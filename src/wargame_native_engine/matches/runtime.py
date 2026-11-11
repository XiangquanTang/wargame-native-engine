"""Symmetric native match host; no optimizer, training rewards or oracle policy input.

Controllers use detached side bindings. This host alone owns the gateway and
publishes the privileged native result/analysis after a match. A window accepts
at most one action per side, then advances the normal native simulation clock.
"""
from __future__ import annotations
from wargame_native_engine.training.candidate_limits import MAX_NEW_CANDIDATES, validate_new_candidate_limit

from copy import deepcopy
from dataclasses import replace
import shutil
import inspect
from wargame_native_engine.io.serialization import canonical_hash
from pathlib import Path
import tempfile

from wargame_native_engine.analysis.native_analyzer import NativeEightFactorAnalyzer
from wargame_native_engine.backends.native import NativeEngineBackend
from wargame_native_engine.compat.source_frame.contract import SOURCE_ACTION_CONTRACT
from wargame_native_engine.compat.source_frame.template import source_frame_template_for_scenario
from wargame_native_engine.gateway import CommandGateway, SessionManager
from wargame_native_engine.operations.controller import SideController
from wargame_native_engine.session.game_session import GameSession
from wargame_native_engine.session.source_environment import SourceCompatibleEnvironment
from wargame_native_engine.training.decision_encoder import (
    DecisionActionEncoder, DecisionEncoderV2Config, DecisionObservationEncoderV2,
)
from wargame_native_engine.training.env import TrainingDecisionBinding
from wargame_native_engine.training.minimal_runtime import ApproachPotential
from wargame_native_engine.training.spatial_candidates import (
    BoundedSpatialCandidateProvider, build_spatial_candidate_provider_config,
)


class _SeatManager:
    """Narrow trusted-teacher facade with no global weapon catalogue escape.

    v25 provides side-aware catalogues; v24 does not. In both versions expose
    profiles only for CURRENT own inventory, never future/hidden inventory IDs.
    """
    def __init__(self, seat, manager):
        self._seat, self._manager = seat, manager

    def get_weapon_catalog(self, session_id, *, side=None):
        if side is not None and side != self._seat.controlled_side:
            raise ValueError('weapon catalogue belongs to another seat')
        method = self._manager.get_weapon_catalog
        if 'side' in inspect.signature(method).parameters:
            catalog = method(session_id, side=self._seat.controlled_side)
        else:
            catalog = method(session_id)
        own_ids = {int(weapon['weapon_id'])
                   for row in self._seat._controlled_view().own_weapon_inventory
                   for weapon in row['weapons']}
        weapons = [deepcopy(w) for w in catalog['weapons'] if int(w['weapon_id']) in own_ids]
        result = {'schema_version': catalog.get('schema_version'), 'scope': 'current-own-inventory',
                  'weapons': weapons}
        result['catalog_hash'] = canonical_hash(result)
        return result

    def public_static_map(self, session_id):
        return self._manager.public_static_map(session_id)

    def get_indirect_fire_capabilities(self, session_id, side):
        if side != self._seat.controlled_side:
            raise ValueError('capabilities belong to another seat')
        return self._manager.get_indirect_fire_capabilities(session_id, side)

    def agent_reset_context(self, session_id, side, agent_id):
        if side != self._seat.controlled_side:
            raise ValueError('reset context belongs to another seat')
        return self._manager.agent_reset_context(session_id, side, agent_id)


class MatchSeat:
    """Trusted built-in adapter facade; do not pass this host object to plugins.

    ``current_decision_binding`` and ``_controlled_view`` are strictly seat
    projections. ``_manager`` exists only for reuse of the built-in rule teacher;
    arbitrary plugins must receive detached controller requests instead.
    """
    def __init__(self, runtime, side):
        self._runtime = runtime
        self.controlled_side = side
        self._manager = _SeatManager(self, runtime._manager)
        self.approach = runtime.approach
        self._controller = SideController(runtime._gateway, runtime._session_id,
                                          runtime._controller_ids[side])
        self._provider = BoundedSpatialCandidateProvider(runtime.provider_config)
        self._prepared_window = None
        self._action_encoder = DecisionActionEncoder(runtime.candidate_limit)

    def _require_session_id(self):
        self._runtime._require_open()
        return self._runtime._session_id

    def _controlled_view(self):
        self._runtime._require_open()
        return self._controller.observe()

    def prepare(self):
        view = self._controlled_view()
        if not self._runtime.ended and not view.terminal and self._prepared_window != view.step_index:
            self._provider.prepare(
                view=view,
                public_map=self._manager.public_static_map(self._require_session_id()),
                indirect_capabilities=self._manager.get_indirect_fire_capabilities(
                    self._require_session_id(), self.controlled_side),
                action_capacity=self._runtime.candidate_limit,
                plan_move=self._controller.plan_move,
                plan_indirect_fire=self._controller.plan_indirect_fire,
            )
            self._prepared_window = view.step_index
        return self.current_decision_binding

    @property
    def current_decision_binding(self):
        view = self._controlled_view()
        decision, catalog = view.decision_semantic, view.action_catalog
        if decision is None or catalog is None:
            raise RuntimeError('match requires decision projection and legal action catalog')
        inventory = deepcopy(tuple(view.own_weapon_inventory))
        # Recompute solely from visible rows, so boarding, deaths, and delayed
        # arrivals cannot leave stale entity masks or leak hidden enemy counts.
        weapons = sum(len(row['weapons']) for row in inventory)
        pools = sum(len(row['ammunition_pools']) + len(row['weapons']) for row in inventory)
        config = DecisionEncoderV2Config(
            max_own_operators=max(1, len(decision.own_operators), len(inventory)),
            max_observed_enemies=max(1, len(decision.observed_enemies)),
            max_objectives=max(1, len(decision.side_context.objectives)),
            max_actions=self._runtime.candidate_limit,
            max_weapon_entries=max(1, weapons), max_ammunition_pools=max(1, pools),
        )
        observation = DecisionObservationEncoderV2(config).encode(decision, inventory, catalog)
        actions = self._action_encoder.encode(catalog)
        return TrainingDecisionBinding(
            observation, actions, inventory, self._require_session_id(),
            self._runtime._binding_scope, self._runtime.provider_config.content_hash,
        )

    def plan_move(self, unit_id, destination_hex, *, request_id, movement_mode='NORMAL'):
        self._runtime._require_active()
        proposal = self._controller.plan_move(unit_id, destination_hex,
                                             request_id=request_id, movement_mode=movement_mode)
        # Force explicit capacity/encoding validation before exposing a plan.
        self.current_decision_binding
        return proposal

    def resolve(self, index):
        binding = self.current_decision_binding
        if index is None:
            return ()
        if type(index) is not int:
            raise TypeError('match action must be an exact int index or None')
        if not 0 <= index < len(binding.action_space.mask) or not binding.action_space.mask[index]:
            raise ValueError('match action index outside current legal catalog')
        return (binding.action_space.actions[index]['action_id'],)


class MatchRuntime:
    """Single-use deterministic decision-window match with symmetric budgets.

    ``max_steps`` is a runner stop, not an invented engine victory. Wall-clock
    controller latency never changes simulation time in this synchronous mode.
    """
    def __init__(self, bundle, *, seed=42, decision_ms=1000, candidate_limit=MAX_NEW_CANDIDATES, max_steps=1500):
        validate_new_candidate_limit(candidate_limit)
        if type(seed) is not int:
            raise TypeError('seed must be an exact int')
        for name, value in [('decision_ms', decision_ms), ('candidate_limit', candidate_limit), ('max_steps', max_steps)]:
            if type(value) is not int or value <= 0:
                raise ValueError(name + ' must be a positive exact int')
        self.bundle, self.seed = bundle, seed
        self.decision_ms, self.candidate_limit, self.max_steps = decision_ms, candidate_limit, max_steps
        self.approach = ApproachPotential(bundle.scenario)
        self.provider_config = build_spatial_candidate_provider_config({
            'schema_version':'native-spatial-candidate-provider-config/1.0',
            'provider_id':'native-bounded-static-map-spatial-candidates','provider_version':'2.0.0',
            'cell_set_rule':'PUBLIC_LOCAL_OBJECTIVE_VISIBLE_GRID_SHORTLIST',
            'cell_sort_order':'HEX_ID_ASC','semantic_deduplication':'ENGINE_ACTION_ID',
            'movement_modes':['NORMAL'],'max_map_cells':len(bundle.scenario.map.hexes),
            'max_planning_requests':128,'final_action_capacity':candidate_limit,'shortlist_quotas':[4,2,1,1],
        })
        def factory():
            current = bundle.scenario
            return SourceCompatibleEnvironment(
                GameSession(NativeEngineBackend(current, map_runtime_data=bundle.map_runtime_data)),
                template=source_frame_template_for_scenario(current), source_contract=SOURCE_ACTION_CONTRACT)
        assets = bundle.scenario.to_dict()
        map_asset = assets.pop('map')
        self._storage = tempfile.TemporaryDirectory(prefix='wargame-native-match.')
        self._manager = SessionManager(factory, storage_root=Path(self._storage.name),
            analyzer_factory=NativeEightFactorAnalyzer, retain_analysis_history=False,
            replay_assets={'schema':'native-scenario-split/1.0','map':map_asset,'scenario':assets})
        self._gateway = CommandGateway(self._manager, decision_interval_ms=decision_ms, frame_interval_ms=decision_ms)
        self._controller_ids = {'red':'native-match-red', 'blue':'native-match-blue'}
        self._session_id = None
        self._binding_scope = f'match:{seed}'
        self._seats = {}
        self._closed = False
        self._failed = False
        self._published = False
        self.ended = False
        self.steps = 0
        self.last_execution = {}

    def reset(self):
        self._require_open()
        if self._session_id is not None:
            raise RuntimeError('match runtime is single-use')
        descriptor = self._gateway.create_session(
            red_controller_id=self._controller_ids['red'], blue_controller_id=self._controller_ids['blue'],
            seed=self.seed, record=True)
        self._session_id = descriptor.session_id
        self.ended = descriptor.terminal
        self._seats = {side: MatchSeat(self, side) for side in ('red', 'blue')}
        return {side: seat.prepare() for side, seat in self._seats.items()}

    def seat(self, side):
        self._require_open()
        if side not in ('red', 'blue'):
            raise ValueError('side must be red or blue')
        if self._session_id is None:
            raise RuntimeError('match must be reset first')
        return self._seats[side]

    def step(self, red_index=None, blue_index=None):
        self._require_active()
        # Resolve and validate BOTH selections before submitting either; no
        # invalid blue decision can leave a half-submitted red window.
        self.last_execution = {}
        choices = {side: self.seat(side).resolve(index)
                   for side, index in (('red', red_index), ('blue', blue_index))}
        views = {side: self.seat(side)._controlled_view() for side in ('red','blue')}
        window = views['red'].step_index
        if window != views['blue'].step_index:
            raise RuntimeError('match sides disagree about decision window')
        try:
            for side in ('red', 'blue'):
                self._gateway.submit_batch(self._session_id, self._controller_ids[side],
                    request_id=f'match:{window}:{side}', expected_window=window,
                    ordered_action_ids=choices[side])
            advance = self._gateway.last_advance(self._session_id, self._controller_ids['red'])
            if advance is None or advance.decision_window_index != window:
                raise RuntimeError('match window advance evidence missing')
            self.last_execution = {side: deepcopy(self._gateway.last_action_results(
                self._session_id, self._controller_ids[side])) for side in ('red', 'blue')}
            self.steps += 1
            view = self.seat('red')._controlled_view()
            self.ended = view.terminal or self.steps >= self.max_steps
            if not self.ended:
                for seat in self._seats.values():
                    seat.prepare()
        except BaseException:
            self._failed = True
            raise
        return self.outcome()

    def outcome(self):
        """Observer-only result; NEVER include in a controller request."""
        self._require_open()
        if self._session_id is None:
            raise RuntimeError('match must be reset first')
        view = self.seat('red')._controlled_view()
        result = self._manager.native_result(self._session_id)
        return {'steps':self.steps, 'sim_time_ms':view.sim_time_ms,
                'terminated':view.terminated, 'truncated':view.truncated or (self.ended and not view.terminal),
                'runner_limit':self.steps >= self.max_steps and not view.terminal,
                'winner':result.winner, 'reason':result.reason,
                'ended':self.ended}

    def publish(self, path):
        self._require_open()
        if self._session_id is None or self._failed or self._published:
            raise RuntimeError('match cannot be published in current lifecycle')
        view = self.seat('red')._controlled_view()
        # Stream observer-only native analysis separately; compact archives
        # deliberately omit the heavyweight debug sidecar.
        from wargame_native_engine.analysis.jsonl import analysis_frames_to_jsonl
        analysis_tmp = Path(self._storage.name) / 'match-analysis.jsonl'
        with analysis_tmp.open('w', encoding='utf-8') as stream:
            for index in range(view.frame_index + 1):
                frame = self._manager.frame_analysis(self._session_id, index)
                stream.write(analysis_frames_to_jsonl((frame,)))
        publication = self._gateway.publish_session(self._session_id, self._controller_ids['red'],
            str(path), truncated=not view.terminal, overwrite=False, replay_mode='compact')
        analysis_path = publication.output_dir / 'frame_analysis.native.jsonl'
        with analysis_tmp.open('rb') as source, analysis_path.open('xb') as target:
            shutil.copyfileobj(source, target)
        self.ended = True
        self._published = True
        return replace(publication, analysis_path=analysis_path)

    def _require_open(self):
        if self._closed:
            raise RuntimeError('match is closed')

    def _require_active(self):
        self._require_open()
        if self._session_id is None or self.ended or self._failed or self._published:
            raise RuntimeError('match is not active')

    def close(self):
        if self._closed:
            return
        try:
            if self._session_id is not None:
                self._gateway.close_session(self._session_id)
        finally:
            self._storage.cleanup()
            self._closed = True
