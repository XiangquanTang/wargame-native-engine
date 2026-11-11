"""Small rollout boundary: native adjudication, public observations, explicit rewards.

The neural policy never receives GREEN analysis. That analysis is used solely by
this training reward adapter. Old training interfaces remain import-compatible.
"""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass, asdict
import math
import random
from typing import Mapping

from wargame_native_engine.agents.baselines import NoOpAgent
from wargame_native_engine.analysis.native_analyzer import NativeEightFactorAnalyzer
from wargame_native_engine.backends.native import NativeEngineBackend
from wargame_native_engine.compat.source_frame.contract import SOURCE_ACTION_CONTRACT
from wargame_native_engine.compat.source_frame.template import source_frame_template_for_scenario
from wargame_native_engine.data import ScenarioBundle
from wargame_native_engine.operations.controller import SideController
from wargame_native_engine.session.game_session import GameSession
from wargame_native_engine.session.source_environment import SourceCompatibleEnvironment
from wargame_native_engine.training.env import NativeTrainingEnv
from wargame_native_engine.training.spatial_candidates import build_spatial_candidate_provider_config
from wargame_native_engine.training.decision_encoder import OWN_OPERATOR_FEATURES, OBJECTIVE_FEATURES

OWN = {name: i for i, name in enumerate(OWN_OPERATOR_FEATURES)}
OBJ = {name: i for i, name in enumerate(OBJECTIVE_FEATURES)}


@dataclass(frozen=True)
class RewardConfig:
    gamma: float = .99
    win_weight: float = 1.0
    capture_weight: float = 1.0
    distance_weight: float = .1
    eight_factor_weight: float = .03

    preset: str = "legacy-v1"
    damage_weight: float = 0.0
    own_damage_weight: float = 0.0
    time_weight: float = 0.0
    approach_scale: float = 3.0

    pressure_major_rate: float = 3.0
    pressure_minor_rate: float = 1.5
    pressure_major_value: float = 2.0
    pressure_distance_scale: float = 3.0
    pressure_floor: float = .5
    pressure_enemy_multiplier: float = 2.0
    kill_weight: float = 1.0

    @classmethod
    def for_preset(cls, preset="legacy-v1", **overrides):
        values = dict(preset=preset)
        if preset == "progressive-v2":
            values.update(win_weight=10., capture_weight=5., distance_weight=1.,
                          eight_factor_weight=0., damage_weight=1.,
                          own_damage_weight=1., time_weight=.005)
        if preset == "capture-pressure-v3":
            values.update(win_weight=10., capture_weight=5., distance_weight=1.,
                          eight_factor_weight=0., damage_weight=3.,
                          own_damage_weight=1., time_weight=0.)
        values.update({k: v for k, v in overrides.items() if v is not None})
        return cls(**values)

    def metadata(self):
        values = asdict(self)
        if self.preset == "legacy-v1":
            return {k: values[k] for k in ("gamma", "win_weight", "capture_weight",
                                          "distance_weight", "eight_factor_weight")}
        if self.preset == "progressive-v2":
            return {k: v for k, v in values.items()
                    if not k.startswith("pressure_") and k != "kill_weight"}
        return values

    def __post_init__(self):
        if self.preset not in {"legacy-v1", "progressive-v2", "capture-pressure-v3"}:
            raise ValueError("unknown reward preset")
        for name in self.__dataclass_fields__:
            if name == "preset":
                continue
            value = getattr(self, name)
            if isinstance(value, bool) or not math.isfinite(value) or value < 0:
                raise ValueError(f"invalid reward {name}")
        if not 0 < self.pressure_floor <= 1:
            raise ValueError("pressure_floor must be in (0,1]")
        if self.pressure_distance_scale <= 0 or self.pressure_major_value <= 0:
            raise ValueError("pressure distance scale and major value must be positive")
        if self.pressure_enemy_multiplier < 1:
            raise ValueError("pressure_enemy_multiplier must be >= 1")
        if self.approach_scale <= 0:
            raise ValueError("approach_scale must be positive")
        if self.preset == "legacy-v1" and any((self.damage_weight, self.own_damage_weight, self.time_weight)):
            raise ValueError("damage/time rewards require progressive-v2")
        if not 0 < self.gamma <= 1:
            raise ValueError("gamma must be in (0,1]")


REWARD_COMPONENT_NAMES = ('win', 'capture', 'distance', 'eight_factor',
                          'enemy_damage', 'own_damage', 'time', 'objective_pressure', 'enemy_kill')


@dataclass(frozen=True)
class RewardState:
    distance: float
    owned: float
    eight: float
    enemy_loss: float
    own_loss: float
    sim_time_ms: int
    pressure_rate: float = 0.0
    enemy_kills: float = 0.0


class LossLedger:
    """Reward-only actual health loss, bounded by the immutable scenario roster.

    Reserve arrivals and absent rows are not damage. Healing/reappearing units
    cannot earn the same loss twice. Dead units consume only their remaining
    initial health. Initial pre-existing damage is not rewarded.
    """
    def __init__(self, operators):
        self.roster = {u.operator_id: (u.side, float(u.health)) for u in operators}
        self.losses = {key: 0. for key in self.roster}
        self.kill_eligible = {u.operator_id for u in operators
                              if u.health > 0 and getattr(u, 'alive', True)}
        self.killed = set()
        self.capacity = {}
        for side, health in self.roster.values():
            if not math.isfinite(health) or health < 0:
                raise ValueError("invalid scenario health")
            self.capacity[side] = self.capacity.get(side, 0.) + health

    def update(self, operators, controlled_side):
        for key, row in operators.items():
            key = int(key)
            if key not in self.roster:
                raise RuntimeError("reward roster changed: unknown operator")
            side, initial = self.roster[key]
            if row['side'] != side:
                raise RuntimeError("reward roster side changed")
            health = float(row['health'])
            if not math.isfinite(health) or health < 0:
                raise ValueError("invalid authoritative health")
            if not row['alive'] and key in self.kill_eligible:
                self.killed.add(key)
            remaining = health if row['alive'] else 0.
            self.losses[key] = max(self.losses[key], min(initial, max(0., initial-remaining)))
        own = sum(loss for key, loss in self.losses.items() if self.roster[key][0] == controlled_side)
        enemy = sum(loss for key, loss in self.losses.items() if self.roster[key][0] != controlled_side)
        own_cap = self.capacity.get(controlled_side, 0.)
        enemy_cap = sum(v for k, v in self.capacity.items() if k != controlled_side)
        return (enemy/enemy_cap if enemy_cap else 0., own/own_cap if own_cap else 0.)

    def enemy_kill_fraction(self, controlled_side):
        eligible = {key for key in self.kill_eligible if self.roster[key][0] != controlled_side}
        return len(self.killed & eligible)/len(eligible) if eligible else 0.


class ApproachPotential:
    """Static directed-map graph distance to each objective's capture region.

No combat/path-cost/oracle prediction. All objectives remain in the potential;
owned ones have value one. This avoids dropping a target on capture. Actor
positions come exclusively from current side-owned encoded rows.
"""
    def __init__(self, scenario, *, tolerate_missing_objectives=False):
        cells = {c.hex_id: c for c in scenario.map.hexes}
        if not cells:
            raise ValueError("minimal baseline requires declared native hexes")
        self.positions = {h: (float(h % 100), float(h // 100)) for h in cells}
        self.spatial_scale = max(1., max(max(p) for p in self.positions.values()))
        reverse = {h: [] for h in cells}
        for h, cell in cells.items():
            for n in cell.neighbors:
                if n in cells:
                    reverse[n].append(h)
        self.distances = {}
        self.objective_values = {p.objective_id: float(getattr(p, 'value', 1.)) for p in scenario.objectives}
        self.tolerate_missing_objectives = tolerate_missing_objectives
        for point in scenario.objectives:
            if point.x != int(point.x) or point.y != int(point.y):
                if tolerate_missing_objectives:
                    self.distances[point.objective_id] = {}
                    continue
                raise ValueError("objective must have integral native hex coordinates")
            center = int(point.y)*100 + int(point.x)
            if center not in cells:
                if tolerate_missing_objectives:
                    self.distances[point.objective_id] = {}
                    continue
                raise ValueError("objective center is absent from map")
            region = {center, *(n for n in cells[center].neighbors if n in cells)}
            distances = {h: 0 for h in region}
            queue = deque(sorted(region))
            while queue:
                h = queue.popleft()
                for n in reverse[h]:
                    if n not in distances:
                        distances[n] = distances[h] + 1
                        queue.append(n)
            self.distances[point.objective_id] = distances
        self.distance_scale = max(1, max((max(d.values(), default=0) for d in self.distances.values()), default=1))

    def state(self, binding, nonlinear_scale=None):
        obs = binding.observation
        positions = [int(row[OWN['current_hex']])
                     for row, present in zip(obs.own_operator_features, obs.own_operator_mask)
                     if present and row[OWN['alive']] and not row[OWN['destroyed']]
                     and row[OWN['spatially_present']] and not row[OWN['on_board']]
                     and row[OWN['current_hex_present']] and row[OWN['domain']] in (1., 2.)]
        points = [row for row, present in zip(obs.objective_features, obs.objective_mask) if present]
        total = sum(max(0., row[OBJ['value']]) for row in points)
        if not points or total <= 0:
            return 0., 0.
        phi = owned = 0.
        for row in points:
            weight = max(0., row[OBJ['value']])/total
            is_own = row[OBJ['control_relation']] == 1
            owned += weight * is_own
            key = int(row[OBJ['objective_reference']])
            distances = self.distances.get(key, {}) if self.tolerate_missing_objectives else self.distances[key]
            d = min((distances[p] for p in positions if p in distances), default=None)
            progress = 0. if d is None else max(0., 1. - d/self.distance_scale)
            if nonlinear_scale is not None:
                progress = 0. if d is None else math.exp(-d/nonlinear_scale)
            phi += weight * (1. if is_own else progress)
        return float(phi), float(owned)

    def pressure_rate(self, binding, config):
        """Points per 60 simulated seconds, summed over the static objective roster.

        value >= pressure_major_value is major, lower positive value is minor;
        zero-value points carry no pressure. No objective-name heuristics. Missing
        objective rows or unreachable regions get full neutral pressure; only a
        present, typed objective row can grant ownership or approach credit.
        """
        obs = binding.observation
        positions = [int(row[OWN['current_hex']])
                     for row, present in zip(obs.own_operator_features, obs.own_operator_mask)
                     if present and row[OWN['alive']] and not row[OWN['destroyed']]
                     and row[OWN['spatially_present']] and not row[OWN['on_board']]
                     and row[OWN['current_hex_present']] and row[OWN['domain']] in (1., 2.)]
        rows = {int(row[OBJ['objective_reference']]): row
                for row, present in zip(obs.objective_features, obs.objective_mask) if present}
        rate = 0.
        for key, value in self.objective_values.items():
            if not math.isfinite(value) or value < 0:
                raise ValueError('invalid objective pressure value')
            if value == 0:
                continue
            row = rows.get(key)
            if row is not None and row[OBJ['control_relation']] == 1:
                continue
            distances = self.distances.get(key, {})
            d = min((distances[h] for h in positions if h in distances), default=None)
            proximity = math.exp(-d/config.pressure_distance_scale) if row is not None and d is not None else 0.
            factor = 1. - (1. - config.pressure_floor)*proximity
            if row is not None and row[OBJ['control_relation']] == 2:
                factor *= config.pressure_enemy_multiplier
            base = config.pressure_major_rate if value >= config.pressure_major_value else config.pressure_minor_rate
            rate += base*factor
        return rate


def reward_components(before, after, *, base_reward, terminated, config: RewardConfig):
    """Pure reward math. Truncation keeps after potentials; true terminal zeros them."""
    if config.preset in ('progressive-v2', 'capture-pressure-v3'):
        return _progressive_reward_components(before, after, base_reward=base_reward, config=config)
    distance_before, owned_before, eight_before = before
    distance_after, owned_after, eight_after = after
    distance_used = 0. if terminated else distance_after
    eight_used = 0. if terminated else eight_after
    win = float(base_reward)
    capture = owned_after-owned_before
    distance = config.gamma*distance_used-distance_before
    eight = config.gamma*eight_used-eight_before
    values = {
        'win_raw': win, 'capture_raw': capture,
        'distance_raw': distance, 'eight_factor_raw': eight,
        'win': config.win_weight*win,
        'capture': config.capture_weight*capture,
        'distance': config.distance_weight*distance,
        'eight_factor': config.eight_factor_weight*eight,
        'distance_before': distance_before, 'distance_after': distance_after,
        'eight_factor_before': eight_before, 'eight_factor_after': eight_after,
        'owned_fraction_before': owned_before, 'owned_fraction_after': owned_after,
    }
    values.update(enemy_damage_raw=0., own_damage_raw=0., elapsed_seconds=0., enemy_damage=0., own_damage=0., time=0.)
    values.update(objective_pressure=0., enemy_kill=0.)
    values['total'] = sum(values[k] for k in REWARD_COMPONENT_NAMES)
    if not all(math.isfinite(v) for v in values.values()):
        raise ValueError('nonfinite reward component')
    return values


def _progressive_reward_components(before, after, *, base_reward, config):
    """Bounded net-progress deltas, NOT gamma-policy-invariant shaping.

    Retain end-state potentials at both termination and truncation so capture
    does not cancel approach credit. Standing still earns zero approach reward;
    retreat/loss pays back progress (undiscounted cycles sum to zero).
    """
    if not isinstance(before, RewardState) or not isinstance(after, RewardState):
        raise TypeError("progressive-v2 requires RewardState")
    enemy = after.enemy_loss-before.enemy_loss
    own = after.own_loss-before.own_loss
    elapsed = (after.sim_time_ms-before.sim_time_ms)/1000.
    if min(enemy, own, elapsed) < 0:
        raise ValueError("reward ledger/time must be monotonic")
    values = dict(win_raw=float(base_reward), capture_raw=after.owned-before.owned,
                  distance_raw=after.distance-before.distance,
                  eight_factor_raw=after.eight-before.eight,
                  enemy_damage_raw=enemy, own_damage_raw=own, elapsed_seconds=elapsed,
                  distance_before=before.distance, distance_after=after.distance,
                  eight_factor_before=before.eight, eight_factor_after=after.eight,
                  owned_fraction_before=before.owned, owned_fraction_after=after.owned,
                  enemy_loss_before=before.enemy_loss, enemy_loss_after=after.enemy_loss,
                  own_loss_before=before.own_loss, own_loss_after=after.own_loss,
                  sim_time_ms_before=before.sim_time_ms, sim_time_ms_after=after.sim_time_ms)
    for name, weight in [('win',config.win_weight), ('capture',config.capture_weight),
                         ('distance',config.distance_weight), ('eight_factor',config.eight_factor_weight),
                         ('enemy_damage',config.damage_weight), ('own_damage',-config.own_damage_weight)]:
        values[name] = weight*values[name+'_raw']
    values['time'] = -config.time_weight*elapsed
    values.update(objective_pressure=0., enemy_kill=0.)
    if config.preset == 'capture-pressure-v3':
        kills = after.enemy_kills-before.enemy_kills
        if kills < 0 or before.pressure_rate < 0 or after.pressure_rate < 0:
            raise ValueError('pressure/kill ledger must be nonnegative and monotonic')
        # Left endpoint integration: owning at the end does not erase the cost
        # of the preceding unowned interval. Time is authoritative simulation time.
        values.update(objective_pressure=-before.pressure_rate*elapsed/60.,
                      enemy_kill=config.kill_weight*kills, enemy_kill_raw=kills,
                      pressure_rate_before=before.pressure_rate,
                      pressure_rate_after=after.pressure_rate)
    values['total'] = sum(values[k] for k in REWARD_COMPONENT_NAMES)
    if not all(math.isfinite(v) for v in values.values()):
        raise ValueError('nonfinite reward component')
    return values


class MinimalTrainingEnv(NativeTrainingEnv):
    """Same authoritative gateway, plus symmetric spatial-catalog random opponent."""
    def __init__(self, bundle: ScenarioBundle, *, side='red', horizon=1500,
                 decision_ms=1000, candidate_limit=255, opponent='random-legal', reward_config=None):
        if opponent not in {'random-legal','no-op'}:
            raise ValueError('opponent must be random-legal or no-op')
        self.reward_config = reward_config or RewardConfig()
        self.loss_ledger = LossLedger(bundle.scenario.operators)
        self.bundle = bundle
        self.opponent_mode = opponent
        self._opponent_rng = random.Random(0)
        scenario = bundle.scenario
        self.approach = ApproachPotential(scenario, tolerate_missing_objectives=self.reward_config.preset == "capture-pressure-v3")
        own = [u for u in scenario.operators if u.side == side]
        enemy = [u for u in scenario.operators if u.side != side]
        provider = build_spatial_candidate_provider_config({
            'schema_version':'native-spatial-candidate-provider-config/1.0',
            'provider_id':'native-bounded-static-map-spatial-candidates','provider_version':'2.0.0',
            'cell_set_rule':'PUBLIC_LOCAL_OBJECTIVE_VISIBLE_GRID_SHORTLIST',
            'cell_sort_order':'HEX_ID_ASC','semantic_deduplication':'ENGINE_ACTION_ID',
            'movement_modes':['NORMAL'],'max_map_cells':len(scenario.map.hexes),
            'max_planning_requests':128,
            'final_action_capacity':candidate_limit,'shortlist_quotas':[4,2,1,1],
        })
        def factory():
            current = bundle.scenario
            return SourceCompatibleEnvironment(
                GameSession(NativeEngineBackend(current, map_runtime_data=bundle.map_runtime_data)),
                template=source_frame_template_for_scenario(current), source_contract=SOURCE_ACTION_CONTRACT)
        assets = scenario.to_dict()
        map_asset = assets.pop('map')
        super().__init__(factory, controlled_side=side, opponent=NoOpAgent('minimal-'+opponent),
            max_actions=candidate_limit, max_steps=horizon,
            decision_interval_ms=decision_ms, frame_interval_ms=decision_ms,
            training_interface_version='decision-v2',
            max_own_operators=max(1,len(own)), max_observed_enemies=max(1,len(enemy)),
            max_objectives=max(1,len(scenario.objectives)),
            max_weapon_entries=max(1,sum(len(u.weapon_ids) for u in own)),
            max_ammunition_pools=max(1,sum(len(u.weapon_ids)+len(u.raw_attributes.get("ammunition",{})) for u in own)),
            spatial_candidate_provider_config=provider, enable_eight_factor_shaping=False,
            analyzer_factory=NativeEightFactorAnalyzer, retain_analysis_history=False,
            replay_assets={'schema':'native-scenario-split/1.0','map':map_asset,'scenario':assets})

    def reset(self, seed=42):
        self.loss_ledger = LossLedger(self.bundle.scenario.operators)
        self._opponent_rng = random.Random(seed ^ 0x5D34A)
        return super().reset(seed)

    def _opponent_action_ids(self):
        if self.opponent_mode == 'no-op':
            return ()
        session = self._require_session_id()
        controller_id = self._controller_ids[self.opponent_side]
        view = self._gateway.view(session, controller_id)
        if view.terminal:
            return ()
        controller = SideController(self._gateway, session, controller_id)
        self.spatial_candidate_provider.prepare(
            view=view, public_map=self._manager.public_static_map(session),
            indirect_capabilities=self._manager.get_indirect_fire_capabilities(session,self.opponent_side),
            action_capacity=self.action_encoder.max_actions,
            plan_move=controller.plan_move, plan_indirect_fire=controller.plan_indirect_fire)
        view = self._gateway.view(session, controller_id)
        entries = view.action_catalog.entries
        if not entries:
            raise RuntimeError('random opponent has no legal action')
        return (entries[self._opponent_rng.randrange(len(entries))].action_id,)

    def reward_state(self, binding):
        progressive = self.reward_config.preset in ('progressive-v2', 'capture-pressure-v3')
        distance, owned = self.approach.state(binding, self.reward_config.approach_scale if progressive else None)
        # This privileged value stays solely in the reward path, never PolicyInput.
        analysis = self._manager.frame_analysis(self._require_session_id(), binding.observation.frame_index)
        if analysis.provenance.model_id != NativeEightFactorAnalyzer().model_id:
            raise RuntimeError('expected Native eight-factor analysis')
        if len(analysis.side_analyses) != 2 or any(
            s.coverage != 1.0 or s.effective_power is None for s in analysis.side_analyses):
            raise RuntimeError('incomplete Native eight-factor coverage; reward cannot be computed')
        phi = analysis.advantage.normalized_advantage
        if phi is None or not math.isfinite(phi):
            raise RuntimeError('Native eight-factor advantage unavailable')
        eight = phi if self.controlled_side == 'red' else -phi
        if progressive:
            snapshot = self._manager.training_reward_snapshot(self._require_session_id())
            enemy, own = self.loss_ledger.update(snapshot['operators'], self.controlled_side)
            pressure = self.approach.pressure_rate(binding, self.reward_config) if self.reward_config.preset == 'capture-pressure-v3' else 0.
            kills = self.loss_ledger.enemy_kill_fraction(self.controlled_side) if self.reward_config.preset == 'capture-pressure-v3' else 0.
            return RewardState(distance, owned, eight, enemy, own, snapshot['sim_time_ms'], pressure, kills)
        return distance, owned, eight
