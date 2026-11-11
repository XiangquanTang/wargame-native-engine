"""Small entity/candidate PPO. Environment owns action legality and execution.

Only detached side-projected records and public map geometry enter this module.
Identifiers are join keys, never numeric model features. Padding is discarded.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from copy import deepcopy
import math
import random
from typing import Mapping, Sequence

from .decision_encoder import (
    SNAPSHOT_FEATURES, OWN_OPERATOR_FEATURES, OBSERVED_ENEMY_FEATURES,
    OBJECTIVE_FEATURES, INVENTORY_UNIT_FEATURES, WEAPON_INVENTORY_FEATURES,
    ACTION_CANDIDATE_FEATURES,
)
from .learner_backend import require_pytorch

torch = require_pytorch()
nn = torch.nn
SCHEMA = "native-minimal-entity-ppo/1.0"
CAPTURE_SCHEMA = "native-minimal-entity-ppo/1.1-capture-objective"
ENCODER_SCHEMAS = (SCHEMA, CAPTURE_SCHEMA)
GLOBAL_WIDTH, OWN_WIDTH, ENEMY_WIDTH, OBJECTIVE_WIDTH, CANDIDATE_WIDTH = 8, 18, 10, 7, 70


def _finite(value: float) -> float:
    result = float(value)
    if not math.isfinite(result):
        raise ValueError("non-finite policy input")
    return result


def _log(value: float, scale: float = 100.0) -> float:
    return math.log1p(max(0.0, _finite(value))) / math.log1p(scale)


def _time(ms: float) -> float:
    return _log(float(ms) / 1000.0, 3600.0)


def _rows(values, mask, names):
    if len(values) != len(mask):
        raise ValueError("row/mask length mismatch")
    return [dict(zip(names, row)) for row, valid in zip(values, mask) if valid]


@dataclass(frozen=True)
class PolicyInput:
    globals: tuple[float, ...]
    own: tuple[tuple[float, ...], ...]
    enemies: tuple[tuple[float, ...], ...]
    objectives: tuple[tuple[float, ...], ...]
    candidates: tuple[tuple[float, ...], ...]
    mask: tuple[bool, ...]
    action_ids: tuple[str, ...]
    action_indices: tuple[int, ...]

    def __post_init__(self):
        # Detach caller-owned lists; rollout observations remain immutable.
        object.__setattr__(self, "globals", tuple(_finite(x) for x in self.globals))
        if len(self.globals) != GLOBAL_WIDTH:
            raise ValueError("global feature width mismatch")
        for name, width in (("own", OWN_WIDTH), ("enemies", ENEMY_WIDTH),
                            ("objectives", OBJECTIVE_WIDTH), ("candidates", CANDIDATE_WIDTH)):
            rows = tuple(tuple(_finite(x) for x in row) for row in getattr(self, name))
            if any(len(row) != width for row in rows):
                raise ValueError(f"{name} feature width mismatch")
            object.__setattr__(self, name, rows)
        for name in ("mask", "action_ids", "action_indices"):
            object.__setattr__(self, name, tuple(getattr(self, name)))
        n = len(self.candidates)
        if any(len(getattr(self, key)) != n for key in ("mask", "action_ids", "action_indices")):
            raise ValueError("candidate identity/mask mismatch")
        if any(type(x) is not bool for x in self.mask):
            raise ValueError("mask must contain bools")
        if len(set(self.action_ids)) != n or len(set(self.action_indices)) != n:
            raise ValueError("duplicate candidate identities")
        if any(type(i) is not int or i < 0 for i in self.action_indices):
            raise ValueError("invalid catalog indices")


class CompactEncoder:
    """18 own, 10 observed-enemy, 7 objective, 70 per-candidate features.

    `hex_positions` is static public geometry, not current world/hidden units.
    `spatial_scale` is a positive map extent in the same coordinate units.
    """
    def __init__(self, hex_positions: Mapping[int, Sequence[float]], spatial_scale: float,
                 weapon_profiles: Mapping[int, Mapping[str, object]] | None = None, *, schema: str = SCHEMA):
        if schema not in ENCODER_SCHEMAS:
            raise ValueError("unsupported compact encoder schema")
        self.schema = schema
        self.weapon_profiles = {int(k): deepcopy(dict(v)) for k,v in (weapon_profiles or {}).items()}
        self.hex_positions = {int(k): tuple(_finite(x) for x in v[:2])
                              for k, v in hex_positions.items()}
        if any(len(v) != 2 for v in self.hex_positions.values()):
            raise ValueError("hex center needs x/y")
        self.spatial_scale = _finite(spatial_scale)
        if self.spatial_scale <= 0:
            raise ValueError("spatial_scale must be positive")

    def _hex(self, value):
        key = int(value)
        if key not in self.hex_positions:
            raise ValueError(f"public geometry missing hex {key}")
        return self.hex_positions[key]

    def encode(self, binding) -> PolicyInput:
        o, space = binding.observation, binding.action_space
        if getattr(o, "catalog_hash", None) != space.catalog_hash:
            raise ValueError("observation/catalog mismatch")
        s = dict(zip(SNAPSHOT_FEATURES, o.snapshot_features))
        own = _rows(o.own_operator_features, o.own_operator_mask, OWN_OPERATOR_FEATURES)
        enemies = _rows(o.observed_enemy_features, o.observed_enemy_mask, OBSERVED_ENEMY_FEATURES)
        objectives = _rows(o.objective_features, o.objective_mask, OBJECTIVE_FEATURES)
        inv = {int(r["operator_reference"]): r for r in _rows(
            o.inventory_unit_features, o.inventory_unit_mask, INVENTORY_UNIT_FEATURES)}
        weapons = _rows(o.weapon_inventory_features, o.weapon_inventory_mask, WEAPON_INVENTORY_FEATURES)
        weapon_lookup = {(int(r["owner_reference"]), int(r["weapon_reference"])): r for r in weapons}
        scale = self.spatial_scale
        own_feats, own_xy = {}, {}
        for r in own:
            key = int(r["operator_reference"])
            known = bool(r["current_hex_present"])
            xy = self._hex(r["current_hex"]) if known else (0.0, 0.0)
            own_xy[key] = xy if known else None
            inventory = inv.get(key, {})
            # Ammo sum counts unique pool references; shared weapons not double-counted.
            pools = {}
            for w in weapons:
                if int(w["owner_reference"]) == key and w["ammunition_present"]:
                    pool_key = ("pool", w["ammunition_pool_reference"]) if w["ammunition_pool_present"] else ("weapon", w["weapon_reference"])
                    pools[pool_key] = w["ammunition"]
            own_feats[key] = (
                xy[0]/scale, xy[1]/scale, r["health_ratio"], r["alive"],
                r["spatially_present"], r["on_board"], r["has_active_segment"],
                r["segment_progress_millionths"]/1_000_000.0,
                _time(r["transition_remaining_ms"]), _time(r["weapon_cooldown_remaining_ms"]),
                _time(r["suppression_remaining_ms"]), float(r["concealment_posture"] == 2),
                r["fatigue_level"]/5.0, _time(r["stop_transition_remaining_ms"]),
                float(inventory.get("locked_weapon_present", 0)), _log(sum(pools.values())),
                r["domain"]/3.0, float(known),
            )
        enemy_feats = {}
        for r in enemies:
            enemy_feats[int(r["operator_reference"])] = (
                r["x"]/scale, r["y"]/scale, r["altitude"]/scale, r["alive"],
                r["domain"]/3.0, r["source_type_code_present"],
                r["source_type_code"]/20.0, r["source_subtype_code_present"],
                r["source_subtype_code"]/20.0, r["motion"]/5.0,
            )
        objective_feats = tuple((
            r["x"]/scale, r["y"]/scale, r["radius"]/scale, _log(r["value"]),
            float(r["control_relation"] == 0), float(r["control_relation"] == 1),
            float(r["control_relation"] == 2),
        ) for r in objectives)
        objective_lookup = {int(r["objective_reference"]): r for r in objectives}
        if self.schema == CAPTURE_SCHEMA and len(objective_lookup) != len(objectives):
            raise ValueError("duplicate objective references")
        goals = [(r["x"], r["y"]) for r in objectives if r["control_relation"] != 1]
        def goal_distance(xy):
            return 0.0 if xy is None or not goals else min(math.dist(xy, g) for g in goals)/scale
        candidates, ids, indices, mask = [], [], [], []
        for index, (row, present) in enumerate(zip(o.action_candidate_features, o.action_candidate_mask)):
            if not present:
                continue
            if index >= len(space.action_ids):
                raise ValueError("candidate row lacks frozen catalog identity")
            a = dict(zip(ACTION_CANDIDATE_FEATURES, row))
            if int(a["catalog_index"]) != index:
                raise ValueError("candidate order does not match catalog")
            action_type = int(a["action_type"])
            if not 1 <= action_type <= 16:
                raise ValueError("unsupported compact action type")
            actor = int(a["actor_reference"]) if a["actor_present"] else None
            if actor is not None and actor not in own_feats:
                raise ValueError("candidate actor is absent from side observation")
            actor_features = own_feats.get(actor, (0.0,)*OWN_WIDTH)
            actor_xy = own_xy.get(actor)
            target = int(a["target_reference"]) if a["target_present"] else None
            capture_objective = None
            if self.schema == CAPTURE_SCHEMA and action_type == 11:
                # CAPTURE targets are objectives, never operator identifiers.
                # Fail closed rather than aliasing a same-numbered visible unit.
                capture_objective = objective_lookup.get(target)
                if capture_objective is None:
                    raise ValueError("CAPTURE target objective absent from side observation")
                r = capture_objective
                target_features = (r["x"]/scale, r["y"]/scale, r["radius"]/scale,
                                   _log(r["value"]), float(r["control_relation"] == 0),
                                   float(r["control_relation"] == 1),
                                   float(r["control_relation"] == 2), 0.0, 0.0, 0.0)
            else:
                # Preserve all historical non-CAPTURE and old-checkpoint features.
                target_features = enemy_feats.get(target)
                if target_features is None and target in own_feats:
                    f = own_feats[target]
                    target_features = (f[0], f[1], 0.0, f[3], f[16], 0, 0, 0, 0, 0)
                target_features = target_features or (0.0,)*ENEMY_WIDTH
            w = weapon_lookup.get((int(a["weapon_owner_reference"]), int(a["weapon_reference"]))) if a["weapon_present"] else None
            if a["weapon_present"] and w is None:
                raise ValueError("candidate weapon absent from side inventory")
            if w is None:
                weapon_features = (0.0,)*14
            else:
                owner_inv = inv.get(int(w["owner_reference"]), {})
                profile = self.weapon_profiles.get(int(w["weapon_reference"]))
                maximum_range = None if profile is None else profile.get("maximum_range")
                range_present = isinstance(maximum_range,(int,float)) and not isinstance(maximum_range,bool)
                if range_present and (_finite(maximum_range) < 0):
                    raise ValueError("negative public weapon range")
                domains = () if profile is None else tuple(profile.get("target_domains", ()))
                weapon_features = (
                    w["ammunition_present"], _log(w["ammunition"]),
                    w["ammunition_cost_present"], _log(w["ammunition_cost"]),
                    w["direct_mode"], w["guided_mode"], w["indirect_mode"],
                    float(bool(owner_inv.get("locked_weapon_present")) and owner_inv.get("locked_weapon_reference") == w["weapon_reference"]),
                    float(profile is not None), float(range_present),
                    float(maximum_range)/scale if range_present else 0.0,
                    float("PERSONNEL" in domains), float("VEHICLE" in domains), float("AIR" in domains),
                )
            xy = None
            if capture_objective is not None:
                xy = (capture_objective["x"], capture_objective["y"])
            elif a["target_position_present"]:
                xy = (a["target_position_x"], a["target_position_y"])
            elif a["path_endpoint_present"]:
                xy = self._hex(a["path_endpoint"])
            elif a["target_hex_present"]:
                xy = self._hex(a["target_hex"])
            elif target in enemy_feats:
                xy = (target_features[0]*scale, target_features[1]*scale)
            elif target in own_xy:
                xy = own_xy[target]
            dx, dy = ((xy[0]-actor_xy[0])/scale, (xy[1]-actor_xy[1])/scale) if xy is not None and actor_xy is not None else (0.0, 0.0)
            geometry = (
                a["actor_present"], a["target_present"], a["weapon_present"], float(xy is not None),
                dx, dy, math.hypot(dx, dy), goal_distance(xy), goal_distance(actor_xy),
                _log(a["path_length"]), a["movement_mode"]/4.0, a["fire_mode"]/3.0,
            )
            features = tuple(float(action_type == k) for k in range(1,17)) + actor_features + target_features + weapon_features + geometry
            candidates.append(features)
            ids.append(space.action_ids[index]); indices.append(index); mask.append(bool(space.mask[index]))
        if len(candidates) != len(space.action_ids):
            raise ValueError("missing catalog candidate rows")
        globals_ = (float(s["stage"] == 1), float(s["stage"] == 2), float(s["stage"] == 3),
                    _time(s["sim_time_ms"]), _log(len(own)), _log(len(enemies)),
                    _log(len(objectives)), _log(len(candidates)))
        return PolicyInput(globals_, tuple(own_feats.values()), tuple(enemy_feats.values()),
                           objective_feats, tuple(candidates), tuple(mask), tuple(ids), tuple(indices))


@dataclass(frozen=True)
class PPOConfig:
    hidden_size: int = 64
    entity_size: int = 16
    learning_rate: float = 3e-4
    gamma: float = 0.99
    gae_lambda: float = 0.95
    clip_epsilon: float = 0.2
    value_coefficient: float = 0.5
    entropy_coefficient: float = 0.01
    max_grad_norm: float = 0.5
    epochs: int = 4
    minibatch_size: int = 32

    def __post_init__(self):
        for k in ("hidden_size", "entity_size", "epochs", "minibatch_size"):
            if type(getattr(self,k)) is not int or getattr(self,k) <= 0:
                raise ValueError(f"{k} must be positive integer")
        for k in ("learning_rate", "gamma", "gae_lambda", "clip_epsilon", "value_coefficient", "entropy_coefficient", "max_grad_norm"):
            v = _finite(getattr(self,k))
            if v < 0 or (k in {"learning_rate", "max_grad_norm"} and v == 0):
                raise ValueError(f"invalid {k}")
        if not 0 < self.gamma <= 1 or not 0 <= self.gae_lambda <= 1 or not 0 < self.clip_epsilon < 1:
            raise ValueError("invalid PPO discount/lambda/clip")


class EntityCandidateActorCritic(nn.Module):
    def __init__(self, config: PPOConfig):
        super().__init__()
        e, h = config.entity_size, config.hidden_size
        def entity(width):
            return nn.Sequential(nn.Linear(width, e), nn.Tanh(), nn.Linear(e,e), nn.Tanh())
        self.own_encoder, self.enemy_encoder, self.objective_encoder = entity(OWN_WIDTH), entity(ENEMY_WIDTH), entity(OBJECTIVE_WIDTH)
        context = GLOBAL_WIDTH + 3*e
        self.policy = nn.Sequential(nn.Linear(context+CANDIDATE_WIDTH,h), nn.Tanh(), nn.Linear(h,1))
        self.critic = nn.Sequential(nn.Linear(context,h), nn.Tanh(), nn.Linear(h,1))

    def forward(self, observation: PolicyInput):
        device = next(self.parameters()).device
        def pool(rows, encoder):
            if not rows:
                return torch.zeros(encoder[0].out_features, device=device)
            return encoder(torch.tensor(rows, dtype=torch.float32, device=device)).mean(dim=0)
        context = torch.cat((torch.tensor(observation.globals, dtype=torch.float32, device=device),
                             pool(observation.own,self.own_encoder), pool(observation.enemies,self.enemy_encoder),
                             pool(observation.objectives,self.objective_encoder)))
        value = self.critic(context).squeeze(-1)
        if observation.candidates:
            candidates = torch.tensor(observation.candidates, dtype=torch.float32, device=device)
            logits = self.policy(torch.cat((context.expand(len(candidates),-1), candidates),dim=-1)).squeeze(-1)
        else:
            logits = torch.empty((0,), dtype=torch.float32, device=device)
        if not torch.isfinite(value).all() or not torch.isfinite(logits).all():
            raise ValueError("non-finite policy output")
        return logits, value


def masked_distribution(logits, mask):
    valid = torch.tensor(mask, dtype=torch.bool, device=logits.device)
    if logits.ndim != 1 or valid.shape != logits.shape or not bool(valid.any()):
        raise ValueError("no legal actions or mask shape mismatch")
    return torch.distributions.Categorical(logits=logits.masked_fill(~valid, -torch.inf))


@dataclass(frozen=True)
class PolicyAction:
    action_index: int
    local_index: int
    log_prob: float
    value: float


@dataclass(frozen=True)
class PPOTransition:
    observation: PolicyInput
    action_index: int
    old_log_prob: float
    value: float
    reward: float
    next_value: float
    bootstrap_allowed: bool
    trace_continues: bool

    def __post_init__(self):
        if type(self.action_index) is not int or not 0 <= self.action_index < len(self.observation.mask) or not self.observation.mask[self.action_index]:
            raise ValueError("transition selects illegal action")
        for k in ("old_log_prob", "value", "reward", "next_value"):
            _finite(getattr(self,k))
        if type(self.bootstrap_allowed) is not bool or type(self.trace_continues) is not bool:
            raise ValueError("bootstrap/trace flags must be bool")
        if self.trace_continues and not self.bootstrap_allowed:
            raise ValueError("trace cannot continue after terminal")


def compute_gae(transitions: Sequence[PPOTransition], gamma: float, gae_lambda: float):
    advantages = [0.0]*len(transitions)
    following = 0.0
    for i in range(len(transitions)-1,-1,-1):
        t = transitions[i]
        delta = t.reward + gamma*float(t.bootstrap_allowed)*t.next_value - t.value
        following = delta + gamma*gae_lambda*float(t.trace_continues)*following
        advantages[i] = following
    return advantages, [a+t.value for a,t in zip(advantages,transitions)]


class MinimalPPO:
    def __init__(self, config: PPOConfig | None = None, *, seed: int = 42, device: str = "cpu", encoder_schema: str = SCHEMA):
        if encoder_schema not in ENCODER_SCHEMAS:
            raise ValueError("unsupported compact encoder schema")
        self.encoder_schema = encoder_schema
        self.config = config or PPOConfig()
        self.device = torch.device(device)
        if self.device.type not in {"cpu", "cuda"}:
            raise ValueError("supported devices: cpu, cuda")
        if self.device.type == "cuda" and not torch.cuda.is_available():
            raise ValueError("CUDA requested but unavailable")
        # Fork RNG for reproducible initialization without changing host RNG.
        with torch.random.fork_rng(devices=[]):
            torch.manual_seed(seed)
            self.model = EntityCandidateActorCritic(self.config).to(self.device)
        self.optimizer = torch.optim.Adam(self.model.parameters(), lr=self.config.learning_rate)
        self.generator = torch.Generator(device="cpu").manual_seed(seed)
        self.shuffle = random.Random(seed)
        self.update_count = 0

    @property
    def parameter_count(self):
        return sum(p.numel() for p in self.model.parameters())

    def act(self, observation: PolicyInput, *, deterministic: bool = False) -> PolicyAction:
        self.model.eval()
        with torch.no_grad():
            logits, value = self.model(observation)
            dist = masked_distribution(logits, observation.mask)
            index = int(torch.argmax(dist.probs).item()) if deterministic else int(torch.multinomial(dist.probs.cpu(),1,generator=self.generator).item())
            return PolicyAction(observation.action_indices[index], index, float(dist.log_prob(torch.tensor(index,device=self.device))), float(value))

    def value(self, observation: PolicyInput) -> float:
        self.model.eval()
        with torch.no_grad():
            return float(self.model(observation)[1])

    def _parameter_snapshot(self):
        return tuple(p.detach().clone() for p in self.model.parameters())

    def _parameter_l2_delta(self, before):
        with torch.no_grad():
            return float(torch.stack([
                (p.detach().double()-old.double()).square().sum()
                for p, old in zip(self.model.parameters(), before)
            ]).sum().sqrt())

    def _teacher_accuracy(self, samples):
        # Training-set top-1 agreement, not held-out playing strength.
        was_training = self.model.training
        self.model.eval()
        try:
            with torch.no_grad():
                correct = sum(
                    int(masked_distribution(self.model(obs)[0], obs.mask).probs.argmax()) == index
                    for obs, index in samples
                )
            return correct / len(samples)
        finally:
            self.model.train(was_training)

    def update(self, transitions: Sequence[PPOTransition]) -> dict[str,float|int]:
        if not transitions:
            raise ValueError("empty PPO batch")
        parameters_before = self._parameter_snapshot()
        advantages, targets = compute_gae(transitions,self.config.gamma,self.config.gae_lambda)
        device = self.device
        a = torch.tensor(advantages,dtype=torch.float32,device=device)
        # A one-element or constant batch retains its advantage signal.
        std = a.std(unbiased=False)
        if len(a)>1 and float(std)>1e-8:
            a = (a-a.mean())/(std+1e-8)
        target = torch.tensor(targets,dtype=torch.float32,device=device)
        totals = {k:0.0 for k in ("policy_loss","value_loss","entropy","loss","grad_norm","approx_kl","clip_fraction")}
        updates = 0
        self.model.train()
        for _ in range(self.config.epochs):
            order = list(range(len(transitions))); self.shuffle.shuffle(order)
            for start in range(0,len(order),self.config.minibatch_size):
                ids = order[start:start+self.config.minibatch_size]
                new_logs, values, entropies = [], [], []
                for i in ids:
                    t = transitions[i]
                    logits,value = self.model(t.observation)
                    dist = masked_distribution(logits,t.observation.mask)
                    new_logs.append(dist.log_prob(torch.tensor(t.action_index,device=device)))
                    values.append(value); entropies.append(dist.entropy())
                logs = torch.stack(new_logs)
                old = torch.tensor([transitions[i].old_log_prob for i in ids],device=device)
                ratio = torch.exp(logs-old)
                advantage = a[ids]
                policy_loss = -torch.minimum(ratio*advantage,ratio.clamp(1-self.config.clip_epsilon,1+self.config.clip_epsilon)*advantage).mean()
                value_loss = .5*(torch.stack(values)-target[ids]).square().mean()
                entropy = torch.stack(entropies).mean()
                loss = policy_loss+self.config.value_coefficient*value_loss-self.config.entropy_coefficient*entropy
                if not bool(torch.isfinite(loss)):
                    raise ValueError("non-finite PPO loss")
                self.optimizer.zero_grad(set_to_none=True)
                loss.backward()
                grad_norm = torch.nn.utils.clip_grad_norm_(self.model.parameters(),self.config.max_grad_norm,error_if_nonfinite=True)
                self.optimizer.step()
                values_now = {"policy_loss":policy_loss,"value_loss":value_loss,"entropy":entropy,"loss":loss,
                              "grad_norm":grad_norm,"approx_kl":(old-logs).mean(),
                              "clip_fraction":((ratio-1).abs()>self.config.clip_epsilon).float().mean()}
                for key,val in values_now.items(): totals[key]+=float(val.detach())
                updates+=1
        self.update_count+=1
        return {**{k:v/updates for k,v in totals.items()},"optimizer_steps":updates,"update":self.update_count,
                "transitions":len(transitions),"advantage_mean":sum(advantages)/len(advantages),
                "parameter_l2_delta":self._parameter_l2_delta(parameters_before)}

    def behavior_clone(self, samples, *, epochs: int = 4, minibatch_size: int | None = None):
        """Supervised warm start from exact legal teacher catalog selections.

        These samples are not PPO rollouts and supply no invented behavior
        probabilities or advantages. The critic head receives no BC loss.
        """
        samples = tuple(samples)
        if not samples or type(epochs) is not int or epochs <= 0:
            raise ValueError("BC needs samples and positive epochs")
        batch_size = self.config.minibatch_size if minibatch_size is None else minibatch_size
        if type(batch_size) is not int or batch_size <= 0:
            raise ValueError("BC minibatch size must be positive")
        for observation, index in samples:
            if not isinstance(observation, PolicyInput) or type(index) is not int or not 0 <= index < len(observation.mask) or not observation.mask[index]:
                raise ValueError("BC teacher selected an illegal candidate")
        parameters_before = self._parameter_snapshot()
        accuracy_before = self._teacher_accuracy(samples)
        self.model.train()
        loss_sum, steps = 0.0, 0
        for _ in range(epochs):
            order = list(range(len(samples))); self.shuffle.shuffle(order)
            for start in range(0,len(order),batch_size):
                losses=[]
                for i in order[start:start+batch_size]:
                    observation,index = samples[i]
                    logits,_ = self.model(observation)
                    dist = masked_distribution(logits,observation.mask)
                    losses.append(-dist.log_prob(torch.tensor(index,device=self.device)))
                loss = torch.stack(losses).mean()
                if not bool(torch.isfinite(loss)):
                    raise ValueError("non-finite BC loss")
                self.optimizer.zero_grad(set_to_none=True)
                loss.backward()
                torch.nn.utils.clip_grad_norm_(self.model.parameters(),self.config.max_grad_norm,error_if_nonfinite=True)
                self.optimizer.step()
                loss_sum += float(loss.detach()); steps += 1
        return {"loss":loss_sum/steps,"samples":len(samples),"epochs":epochs,"optimizer_steps":steps,
                "teacher_accuracy_before":accuracy_before,
                "teacher_accuracy_after":self._teacher_accuracy(samples),
                "parameter_l2_delta":self._parameter_l2_delta(parameters_before)}

    def state_dict(self):
        return {**({"encoder_schema": self.encoder_schema} if self.encoder_schema != SCHEMA else {}),
                "schema":SCHEMA,"config":asdict(self.config),"model":self.model.state_dict(),
                "optimizer":self.optimizer.state_dict(),"generator_state":self.generator.get_state(),
                "shuffle_state":self.shuffle.getstate(),"update_count":self.update_count}

    def load_state_dict(self, state):
        if (state.get("schema") != SCHEMA or state.get("config") != asdict(self.config)
                or state.get("encoder_schema", SCHEMA) != self.encoder_schema):
            raise ValueError("incompatible minimal PPO checkpoint")
        self.model.load_state_dict(state["model"],strict=True)
        self.optimizer.load_state_dict(state["optimizer"])
        self.generator.set_state(state["generator_state"].cpu())
        self.shuffle.setstate(state["shuffle_state"])
        self.update_count=int(state["update_count"])
