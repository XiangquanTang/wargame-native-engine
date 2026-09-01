# Native Learner / PPO Integration Contract Audit v1

Status: `PASS` through Phase 6E spawn-based parallel Native environment
execution; the original Phase 6A audit below is retained as historical rationale.

Audit date: 2026-08-30

Scope: read-only Phase 6A contract audit; no learner, optimizer, network, or
production PPO configuration is implemented by this document.

## 1. Executive Summary

The current training boundary is semantically ready to feed a PPO learner:
`NativeTrainingEnv` defaults to Decision-v1, emits a perspective-safe numerical
observation, binds a fixed-capacity action mask to one immutable catalog, resolves
indices to catalog `action_id` values, commits through the Gateway, and returns a
transition-aligned versioned reward record. The learner must consume this boundary
as-is and must not read `WorldState`, GREEN, legacy `ControllerView.observation`, or
reimplement legality or reward.

There is no PPO, actor, critic, rollout buffer, GAE, optimizer, or checkpoint
implementation in the repository. The project declares no runtime dependency on
PyTorch, Gym/Gymnasium, Stable-Baselines3, Ray, or RLlib. The inspected Python
environment has NumPy, but it is not a declared project dependency; PyTorch and the
listed RL frameworks are absent.

The recommended Phase 6B backend is a small project-native PyTorch PPO core. It is
the shortest path to exact masked-distribution semantics, catalog provenance,
SharedTrainingDiscountConfig enforcement, and fail-closed checkpoint metadata.
SB3 would require Gymnasium plus `sb3-contrib` MaskablePPO, while RLlib adds a much
larger distributed abstraction before a single-seat baseline has been validated.

Three prerequisites remain before implementation should be called production
ready:

1. Freeze and implement a versioned learner observation preprocessor. Encoder v1
   intentionally emits raw Native units and float64; the probe observed values from
   0--4 for most categorical fields but 1,200,000 for a lifecycle timer.
2. Freeze a learner backend and declare it as an optional dependency. This audit
   recommends PyTorch, but does not install it.
3. Define a versioned learner seed authority covering environment, policy sampling,
   framework, worker, and evaluation streams.

The action-mask, reward alignment, gamma, terminated/truncated bootstrap, GAE, and
checkpoint identity contracts themselves are sufficiently specified below.

## 2. Scope and Non-goals

This audit freezes the interface between the already authoritative Native training
environment and a future learner. It does not implement PPO, install packages,
change the Decision encoder, alter reward, run optimization, add self-play, add a
privileged critic, or add an EightFactor auxiliary target.

World and Engine replay remain the rule-authority verification path. A learner
rollout or checkpoint is a derived training artifact and must never participate in
World hashes or Engine adjudication.

## 3. Current Training Interface

The verified policy and supervision flow is:

```text
NativeEngine / Gateway
  -> ControllerView
  -> SideDecisionSemanticProjection
  -> DecisionObservationEncoder
  -> grouped numerical policy observation
  -> learner action index
  -> frozen EncodedDecisionActionSpace + ActionCatalogSnapshot
  -> action_id
  -> Gateway final validation / NativeEngine commit
  -> TrainingRewardRecord / JointTrainingRewardRecord
  -> next Decision-v1 observation
```

`NativeTrainingEnv` defaults to `decision-v1`. Its `_FrozenTrainingDecisionSnapshot`
holds the encoded observation, encoded action space, and exact catalog. Before an
index is resolved it verifies side, decision window, frame, simulation time,
Decision semantic hash, catalog hash, and action encoder binding. External window
drift fails closed and produces no reward record.

`NativeMultiSeatEnv.step_action_indices` resolves RED and BLUE against their own
same-pre-state catalogs, submits both sides, then performs one joint completion.
`SingleSeatTrainingAdapter` currently accepts product `SeatDecision`, not a discrete
catalog index. Consequently the minimal discrete PPO baseline should use
`NativeTrainingEnv` first. A later adapter may project a learner index through
`NativeMultiSeatEnv.step_action_indices`; it must not translate the index into a
new `SeatOperation`.

The legacy `SideObservationEncoder` remains explicit compatibility only. It is not
an allowed Native PPO v1 policy source.

## 4. Learner Backend Inventory

| Backend | Declared dependency | Native masked actions | Catalog/window binding | Multi-seat future | Dependency/implementation cost | Audit verdict |
|---|---:|---|---|---|---|---|
| Project-native PyTorch PPO | No | Exact custom `MaskedCategorical` is straightforward | Can preserve mask, catalog hash, index and action ID directly | Good base for later independent/self-play runners | Medium; one major optional dependency and local PPO code | **Recommended** |
| Stable-Baselines3 | No | Core PPO has no native mask; `sb3-contrib` MaskablePPO is another dependency | Requires a Gymnasium adapter and custom provenance plumbing | Limited; multi-agent is external to SB3 | Medium initially, high once Native contracts are adapted | Not recommended for v1 |
| Ray RLlib | No | Possible through custom RLModule/model distributions | Possible, but hidden behind a larger batch/config system | Strong long-term multi-agent/distributed support | High and premature for one-seat baseline | Defer to vector/self-play phase |
| NumPy/custom autograd | NumPy is present only in the environment, not declared | Fully custom | Fully custom | Poor | Unjustified optimizer/autograd maintenance | Reject |

No existing repository code imports `torch`, `gym`, `gymnasium`, `ray`,
`stable_baselines3`, or `sb3_contrib`. No learner implementation was found under
`src/wargame_native_engine/training/`, tests, or scripts.

### Backend recommendation

Freeze `CUSTOM_PYTORCH_PPO` for Phase 6B and declare PyTorch through a separate
optional learner dependency group. Keep the Engine and encoders free of PyTorch.
Only the preprocessor, model, masked distribution, rollout batch, optimizer, and
checkpoint code should import it.

## 5. Observation Authority

The sole actor input authority is the policy record emitted by
`DecisionObservationEncoder` schema
`native-decision-observation-encoder/1.0`. The record contains snapshot,
objective, own-operator, observed-enemy, capability features, and their masks.
Its metadata hashes are deliberately out-of-band.

The actor and the v1 single-seat critic must not read:

- `WorldState` or a live Engine/session object;
- GREEN or `FrameAnalysis`;
- authoritative opponent lifecycle;
- reward components, winner, outcome, EightFactor potential, return, advantage,
  or value target;
- legacy `ControllerView.observation` as an alternative authority.

The future learner adapter may flatten and cast the grouped policy record, but the
flatten order must be the encoder contract order and must be versioned. A future
Decision field must not enter the network automatically.

## 6. Observation Numeric Scale

Encoder v1 declares `float64`, raw milliseconds and Native units, with only health
ratio explicitly normalized. Its default flat shape is `(8302,)`:

| Group | Row width | Capacity | Notes |
|---|---:|---:|---|
| Snapshot | 14 | 1 | Includes raw `sim_time_ms`, counts and max ticks |
| Objectives | 6 | 32 | Fixed rows plus objective mask |
| Own operators | 67 | 64 | Includes health, hex, progress and raw timers |
| Observed enemies | 13 | 64 | Perspective-safe fields only |
| Capability | 43 | 64 | 14 fixed families, status and instance count |
| Entity masks | n/a | 224 scalars | Objective, own, enemy and capability masks |

The six-scenario lightweight probe used current Decision-v1 records from source
movement, combined arms, direct fire, guided fire, indirect fire, and loitering
munition scenarios. Active-value absolute statistics were:

| Group | min | median | p95 | max |
|---|---:|---:|---:|---:|
| Snapshot | 0 | 1 | 3,600 | 20,000 |
| Objectives | 0 | 1 | 10 | 10 |
| Own operators | 0 | 0 | 7 | 1,200,000 |
| Observed enemies | 0 | 1 | 7 | 30 |
| Capability | 0 | 0 | 4 | 30 |

The largest feature was `own.limited_remaining_ms` at 1,200,000; sampled
`snapshot.sim_time_ms` reached 20,000, health/max-health reached 100, and most
statuses were 0--4. Directly casting this vector into an unscaled MLP is an
avoidable optimization risk.

Phase 6B should therefore introduce
`native-ppo-observation-preprocessor/1.0`, outside the Decision encoder. It must
freeze group/feature order, float32 conversion, non-finite checks, and deterministic
feature-wise scaling. Fixed, semantic feature-wise scaling is preferred for v1
over an uncheckpointed RunningMeanStd. If running normalization is selected, its
complete state and update count must be part of the checkpoint and disabled/frozen
in evaluation. Changing the preprocessor requires a new hash and is a checkpoint
mismatch.

## 7. Action and Mask Authority

`DecisionActionEncoder` schema `native-decision-action-encoder/1.0` has production
capacity 256. Its mask is exactly:

```text
mask[0:catalog.action_count] = True
mask[catalog.action_count:256] = False
```

It fails closed on catalog overflow. The index is local to side, decision window,
frame, simulation time, catalog hash, and action encoder contract. The authorized
identity is `catalog.entries[index].action_id`; the learner must never reconstruct
an Engine action.

The six-scenario probe collected 34 side/window catalogs:

```text
min=3, median=4, p95=48, max=48, configured capacity=256
```

This is not a capacity proof, but it confirms ample headroom in the sampled public
scenarios. Encoder overflow remains fail-closed; learner-side truncation is
forbidden.

## 8. Masked Distribution Contract

The future `MaskedCategorical` contract is:

1. Verify logits shape equals the action encoder capacity and all valid logits are
   finite.
2. Verify mask dtype is boolean and shape equals logits.
3. If the mask is all false, do not construct or sample a distribution. Route only
   through the ended-state path; otherwise fail closed.
4. Set invalid/padded logits to negative infinity before normalization and construct
   one categorical distribution over the masked logits.
5. Require every invalid probability to be exactly zero and valid probabilities to
   sum to one within the selected numeric tolerance.
6. Sample during training from this masked distribution. Store the resulting index,
   masked `old_log_prob`, and the exact rollout mask.
7. Compute entropy over legal actions only. Invalid/padded logits contribute neither
   probability nor entropy nor policy gradient.
8. During PPO update use the stored mask, never a current environment mask.

Using `softmax(logits) * mask` without renormalization is forbidden. Using a finite
sentinel without an exact-zero test is also insufficient. The all-invalid case must
be checked before the framework distribution is invoked to avoid NaN softmax and
entropy.

## 9. Zero-action Contract

Both current Native and Source-compatible action generation explicitly add WAIT as
a fallback when the World is active. `NativeEngine.valid_actions` returns an empty
tuple only when `world.done` is true. Terminal and truncated next observations can
therefore carry an all-false mask, but the learner must not request another action.

PPO v1 narrows the active-step contract to exactly one catalog index. It must not
use `None`, an empty batch, or synthesize WAIT. The Engine-provided WAIT catalog
entry is sampled like any other legal action.

## 10. Rollout Transition Contract

The following is a non-implemented schema. Tensors are detached learner data;
hashes and IDs are provenance, not model features.

```text
LearnerTransition v1 {
  schema_version: "native-ppo-learner-transition/1.0"
  episode_id
  run_namespace
  side
  rollout_sequence

  observation_float32
  action_mask_bool
  observation_encoder {schema, contract_hash}
  observation_preprocessor {id, version, hash}

  decision_before {window, frame, sim_time_ms, semantic_hash}
  action_catalog {schema, encoder_hash, catalog_hash, capacity}
  action_index
  action_id

  old_log_prob
  value_prediction
  reward
  reward_record_hash
  base_reward_amount
  shaping_reward_amount

  terminated
  truncated
  truncation_source
  effective_gamma
  bootstrap_allowed
  trace_continues

  next_observation_float32
  next_decision {window, frame, sim_time_ms, semantic_hash}
  next_value_prediction
}
```

The rollout buffer must not store a live `ControllerView`, World, GREEN frame,
Engine, Gateway, analyzer, or mutable catalog. The existing `DecisionTransition`
is useful environment publication evidence, but is not a PPO batch: it does not
contain the sampled index, rollout mask, log probability, value prediction,
preprocessor identity, or GAE fields.

## 11. Observation / Action Atomicity

At collection time observation, mask and sampled index must come from the same
frozen training snapshot. Store the exact mask and catalog hash before calling
`step`. After commit, verify the reward transition identity's
`decision_before_hash`, `action_catalog_hash`, and submitted `action_id` against the
stored references. A mismatch aborts the rollout; it must not become a zero-reward
sample.

The environment already rejects stale snapshots. PPO update must not have an
environment reference and must never reinterpret an old index against a new
catalog.

## 12. Reward Alignment and Variant

The frozen transition order is:

```text
obs_t + mask_t + action_index_t
  -> action_id_t -> committed Engine transition
  -> RewardRecord_t
  -> obs_t+1
```

The learner consumes the environment scalar and the reward record hash; it never
recomputes base or shaping reward. Reward variant is selected by the environment's
reward/composite policy configuration:

- `BASE_ONLY`: only `base:TERMINAL_OUTCOME`;
- `COMPOSITE_EIGHT_FACTOR`: base plus
  `shaping:EIGHT_FACTOR_POTENTIAL`.

PPO optimization may use the composite scalar. Logging must separately aggregate
base, shaping, and composite return from the authoritative components. Evaluation
quality is not the shaped return.

## 13. Shared Gamma Authority

The sole discount authority is:

```text
schema  = native-training-discount-config/1.0
id      = native-training-discount
version = 1.0.0
gamma   = 0.99
basis   = PER_DECISION_TRANSITION
hash    = f35a748f9d0232506135b7e2438fa64f5bcc601a7aeb1fc631b182f396e4f06e
```

PPO configuration must reference this identity and must not contain another gamma
value. For the production basis, every learner transition uses effective gamma
0.99 irrespective of `delta_time_ms`. The probe configured 10,000 ms decision
windows and observed min/median/p95/max all equal to 10,000 ms; other repository
tests intentionally configure 1,000, 3,000, 10,000 and the single-seat default
20,000 ms, confirming that elapsed simulation time is not globally constant.

If a future `PER_SIMULATION_TIME_REFERENCE` config is selected, collection must
store `effective_gamma(delta_time_ms)` from the same shared authority and both GAE
and EightFactor shaping must use it. The learner must fail closed on identity
mismatch.

## 14. GAE Lambda and Names

`gae_lambda` belongs exclusively to the versioned learner algorithm config. It is
not EightFactor `shaping_lambda=0.03` and is not part of the shared discount
authority. Code, schemas, logs, and CLI flags must use these unambiguous names:

- `eight_factor_phi`
- `shaping_lambda`
- `discount_gamma`
- `ppo_advantage`
- `gae_lambda`
- `ppo_return`
- `value_target`

No current GAE-lambda authority exists. `0.95` is the recommended first baseline
candidate, not a frozen production default.

## 15. Terminated, Truncated, and Rollout-cut Bootstrap

Three masks are required; one ambiguous `done` flag is forbidden.

| Boundary | Value bootstrap in delta | Continue GAE recurrence into reset/next batch | Environment flag |
|---|---:|---:|---|
| Rule terminal (`terminated=True`) | No; use zero next value | No | terminated |
| Native max-ticks truncation | Yes; use returned next observation value | No across reset | truncated |
| Training max-steps/max-decision-windows | Yes; use returned next observation value | No across reset | truncated |
| Fixed rollout cut, episode active | Yes | Stop at batch edge unless trace state is explicitly carried | neither |
| Ordinary transition | Yes | Yes | neither |

The environment returns a next Decision-v1 observation for terminal and truncated
commits. For native truncation its action mask is empty because `world.done`; the
critic can still evaluate the observation. For wrapper training truncation the
underlying World remains active; the returned observation is still the committed
post-state. In neither truncation case may the learner force next value to zero.

If rule termination and a training limit coincide, the environment gives rule
termination precedence. The learner follows the returned exact flags and reward
record.

## 16. GAE and Return Contract

For transition `t`:

```text
bootstrap_mask_t = 0 if terminated_t else 1
trace_mask_t     = 0 if terminated_t or truncated_t else 1

delta_t = reward_t
        + effective_gamma_t * bootstrap_mask_t * V(next_obs_t)
        - V(obs_t)

ppo_advantage_t = delta_t
                + effective_gamma_t * gae_lambda
                * trace_mask_t * ppo_advantage_t+1

value_target_t = ppo_advantage_t + V(obs_t)
```

At a fixed rollout cut the last delta bootstraps `V(next_obs)` but the recurrence
starts with zero beyond the collected batch unless an explicit, identity-bound
trace carry mechanism is implemented. A rollout cut must not be reported to the
environment as truncation.

Advantage normalization, if enabled, applies only to `ppo_advantage` within the
learner batch. It has no relationship to EightFactor normalized advantage and does
not alter rewards or value targets.

## 17. PPO Objective Contract

PPO v1 should use the standard clipped policy surrogate, a value loss, an entropy
bonus over the masked legal distribution, and finite gradient clipping. The exact
formula and all coefficients belong to `LearnerAlgorithmConfig`, not to the
environment, Decision, reward, or shared discount authority.

At minimum the config must freeze learning rate, `gae_lambda`, clip epsilon,
entropy coefficient, value coefficient, max gradient norm, rollout length,
minibatch size, update epochs, advantage normalization, network architecture,
preprocessor identity, device, and seed identity. NaN/Inf observation, logits,
probabilities, loss, value, gradient or gradient norm aborts the update.

## 18. Actor and Critic Input

The recommended v1 network is a shared MLP backbone with separate fixed-capacity
policy and scalar value heads:

```text
preprocessed Decision-v1 vector
  -> shared MLP
     -> 256 action logits -> exact rollout mask -> MaskedCategorical
     -> scalar V(side-safe Decision-v1 state)
```

Both actor and critic use the same side-safe Decision-v1 input in Phase 6B. GREEN,
EightFactor effective force, potential, reward components, winner, and future
trajectory statistics are forbidden. A centralized privileged critic requires a
separate CTDE contract and is deferred.

An MLP with two 256-unit hidden layers and a conventional nonlinearity is a
reasonable first candidate after preprocessing; it is a research default, not an
environment contract. Transformer, GNN, LSTM, history stacking, and auxiliary
heads are out of scope.

## 19. Proposed Learner Algorithm Config

Non-implemented JSON sketch:

```json
{
  "schema_version": "native-ppo-config/1.0",
  "algorithm_id": "native-masked-ppo",
  "algorithm_version": "1.0.0",
  "backend": "CUSTOM_PYTORCH_PPO",
  "discount_config_ref": {
    "config_id": "native-training-discount",
    "config_version": "1.0.0",
    "config_hash": "f35a748f9d0232506135b7e2438fa64f5bcc601a7aeb1fc631b182f396e4f06e"
  },
  "gae_lambda": "RESEARCH_VALUE_REQUIRED",
  "clip_epsilon": "RESEARCH_VALUE_REQUIRED",
  "learning_rate": "RESEARCH_VALUE_REQUIRED",
  "entropy_coefficient": "RESEARCH_VALUE_REQUIRED",
  "value_coefficient": "RESEARCH_VALUE_REQUIRED",
  "max_grad_norm": "RESEARCH_VALUE_REQUIRED",
  "rollout_length": "RESEARCH_VALUE_REQUIRED",
  "minibatch_size": "RESEARCH_VALUE_REQUIRED",
  "update_epochs": "RESEARCH_VALUE_REQUIRED",
  "advantage_normalization": true,
  "observation_preprocessor": {
    "id": "REQUIRED",
    "version": "REQUIRED",
    "hash": "REQUIRED"
  },
  "network": {
    "kind": "MLP_SHARED_BACKBONE",
    "hidden_dimensions": "RESEARCH_VALUE_REQUIRED",
    "activation": "RESEARCH_VALUE_REQUIRED",
    "policy_head_capacity": 256,
    "value_head": "SCALAR"
  },
  "action_masking_policy": "CATALOG_MASK_BEFORE_NORMALIZATION",
  "seed_config_ref": {
    "id": "REQUIRED",
    "version": "REQUIRED",
    "hash": "REQUIRED"
  },
  "reward_normalization": "NONE",
  "device": "RESEARCH_VALUE_REQUIRED",
  "config_hash": "CANONICAL_SHA256"
}
```

Gamma is intentionally absent as a numeric PPO field. The hash covers every field
that can change optimization or sampling, plus referenced authority identities.

## 20. Seeds and Determinism

Current environment reset accepts one exact integer seed and records it in episode
and transition identities. Native random agents use `NativeRNG`; the random product
seat agent derives an episode stream from its agent and episode seed. No authority
currently coordinates policy sampling, PyTorch, NumPy, worker, environment index,
minibatch shuffling, or evaluation seeds.

Phase 6B requires a versioned `TrainingSeedConfig` with a root seed and canonical
stream derivation for at least:

- Engine/scenario episode seed;
- policy sampling generator;
- parameter initialization;
- minibatch shuffling;
- worker and vector-environment index;
- opponent policy stream;
- deterministic evaluation episodes.

Derived streams should use a canonical hash/namespace scheme, never process-local
`hash()`. The checkpoint stores the seed config identity and the mutable framework
RNG states needed for exact resume.

The guaranteed scope should be: same environment seed and same submitted actions
produce the same authoritative trajectory; same CPU backend/config/seed should
reproduce sampling and an update in tested builds. GPU training must not be claimed
bitwise reproducible unless a separately tested deterministic-kernel contract is
enabled.

## 21. Checkpoint Contract

Non-implemented manifest sketch:

```text
native-ppo-checkpoint-manifest/1.0 {
  checkpoint_id, created_update, environment_steps
  engine {version}
  ruleset {id, version, hash}
  perspective_policy {id, version, hash}
  decision_schema_version
  observation_encoder {schema, contract_hash, flat_shape}
  observation_preprocessor {id, version, hash, state_hash?}
  action_encoder {schema, contract_hash, capacity}
  base_reward_policy {id, version, hash}
  composite_reward_policy {id, version, hash, enabled}
  eight_factor_analyzer {schema, model_id, version, hash} | null
  shaping_policy {id, version, hash} | null
  discount_config {id, version, hash}
  ppo_algorithm_config {id, version, hash}
  seed_config {id, version, hash}
  scenario/training partition identity
  code/git identity if available
  model_state_hash
  optimizer_state_hash
  preprocessor_state_hash
  framework_rng_state_hash
}
```

Load and resume fail closed on decision schema, encoder hash/shape, preprocessor,
action capacity, reward variant, analyzer/shaping provenance, discount, learner
config, network shape, or seed identity mismatch. Equal tensor shapes are not
semantic compatibility. Switching base-only to shaped reward, or changing gamma or
`gae_lambda`, requires a new run unless an explicit non-resume transfer-learning
workflow discards incompatible optimizer/rollout state.

## 22. Evaluation Contract

Training selects actions by sampling the exact masked categorical. Default
evaluation selects the masked argmax, with deterministic tie resolution inherited
from fixed index order. A stochastic evaluation mode is allowed only when named and
given a fixed sampling seed.

Primary evaluation metrics are outcome/win rate, draw rate, base episode return,
episode length, and terminal/truncation reason. Also log shaping return and composite
return when shaping is enabled, but never substitute composite return for win rate.
EightFactor evaluation sidecar metrics may be reported separately.

Evaluation freezes model parameters and preprocessing statistics, disables the
optimizer, records checkpoint and environment identities, uses fixed scenario/seed
partitions, and preserves validation separation. A runnable learner smoke proves
only finite execution, not convergence or superiority.

## 23. Single-seat Baseline

Phase 6B should use `NativeTrainingEnv(controlled_side=..., opponent=NoOpAgent(...))`
with Decision-v1 and one sampled catalog index per active step. `NoOpAgent` is the
simplest deterministic fixed opponent already supported by this environment. A
second baseline can use `RandomLegalAgent` with an explicitly derived seed after
the seed contract exists.

The first implementation acceptance is a short CPU smoke rollout and one PPO
update: all selected actions legal, losses and gradients finite, parameters change
when learning rate is nonzero, and environment snapshot/replay evidence remains
unchanged. It must not claim policy improvement.

`SingleSeatTrainingAdapter` can remain the product-operation adapter. It should not
be the initial discrete PPO API because its `step` accepts `SeatDecision` rather
than a catalog index. A later discrete single-seat wrapper must delegate to
`NativeMultiSeatEnv.step_action_indices` and project the joint reward; it must not
run its own action or reward formula.

## 24. Multi-seat, Vectorization, and Ray Boundary

The current multi-seat environment already isolates per-side Decision projections,
catalogs and rewards and adjudicates both sides from one pre-state. That makes it a
sound future environment substrate, but it does not settle the learning protocol.

Independent PPO, parameter sharing, self-play, MAPPO/CTDE, opponent snapshots,
league management, side swap, and non-stationary checkpoint provenance are deferred.
The recommended progression is fixed-opponent single-seat first, synchronous
vectorized single-seat second, then explicit self-play contracts.

Synchronous vectorization is feasible because each environment owns its match,
snapshot, catalog, reward sequence and seed. No mutable catalog, analyzer result,
preprocessor update, or RNG stream may be shared accidentally. Ray is not needed
for the first single-machine baseline; reconsider it only after the local vector
contract and learner checkpoint are stable.

## 25. Publication and Replay Boundary

Engine strict replay remains independent of policy logits, value estimates,
optimizer state and rollout tensors. The existing training publication records
Decision transitions and reward policy/re-derivation metadata. A future compact
`LearnerRolloutRecord` may record run/episode, decision hashes, catalog hash, action
index/action ID, reward hash, checkpoint identity and learner config identity.

It should not place full tensor batches, GREEN frames, optimizer state or policy
logits into ordinary World replay. Those belong in learner artifacts with their own
schema and retention policy.

## 26. Findings

### P0

NONE. No learner exists that can currently read GREEN, bypass ActionCatalog, mutate
World, duplicate reward, or leak EightFactor shaping into the policy tensor.

### P1

- `LEARNER_OBSERVATION_PREPROCESSOR_CONTRACT_MISSING`: raw float64 Decision-v1
  features span at least 0--1,200,000 in the sampled scenarios. Freeze a
  deterministic, versioned learner preprocessor before claiming an MLP baseline is
  training-ready.
- `LEARNER_BACKEND_AUTHORITY_MISSING`: no backend is declared or installed by the
  project. Freeze `CUSTOM_PYTORCH_PPO` (recommended) and add it only in Phase 6B.
- `LEARNER_SEED_AUTHORITY_MISSING`: environment seeds exist, but policy/framework,
  vector-worker, shuffle and evaluation streams have no shared versioned identity.

### P2

- `PPO_IMPLEMENTATION_MISSING` (expected for this audit).
- `PPO_ROLLOUT_BATCH_SCHEMA_MISSING` and `MASKED_DISTRIBUTION_MISSING`.
- `LEARNER_CHECKPOINT_AND_RESUME_MISSING`.
- `DECISION_TRANSITION_IS_NOT_A_PPO_BATCH`: existing records omit exact rollout mask,
  sampled index, old log probability, value and preprocessor identity.
- `SINGLE_SEAT_PRODUCT_ADAPTER_IS_NOT_INDEX_BASED`; the initial learner can use
  `NativeTrainingEnv` instead.
- `VECTOR_ENV_MISSING`, `SELF_PLAY_CONTRACT_MISSING`, `CTDE_CONTRACT_MISSING`, and
  `EIGHT_FACTOR_AUXILIARY_TARGET_NOT_IMPLEMENTED`.
- `LEARNER_EVALUATION_PUBLICATION_MISSING`.

### P3

- Sampled action catalogs are far below capacity, but the probe is not a global
  action-count bound.
- GPU bitwise determinism is deliberately not promised.
- Large-scale convergence and policy quality benchmarks are outside this contract.

## 27. Required User Freeze

Only learner research/implementation choices remain; environment authorities are
already frozen.

| Field | Recommended initial value | Alternatives/tradeoff | Required before |
|---|---|---|---|
| Backend | Custom PyTorch PPO | SB3 lowers some PPO code but adds Gym + contrib mask/provenance adapters; RLlib is heavier but later multi-agent capable | Phase 6B code |
| Observation preprocessor | Versioned deterministic feature-wise scaling + float32 cast | RunningMeanStd adapts automatically but adds mutable checkpoint/eval state; raw input has demonstrated scale risk | First optimization run |
| `gae_lambda` | 0.95 candidate | Lower reduces variance/trace length; higher increases temporal credit and variance | Production PPO config |
| Clip epsilon | 0.2 candidate | Smaller is more conservative | Production PPO config |
| Learning rate | 3e-4 candidate | Must be tuned; zero retained for invariant test | Production PPO config |
| Entropy coefficient | 0.01 candidate | Zero is simpler; too high can dominate small legal sets | Production PPO config |
| Value coefficient | 0.5 candidate | Tune against observed loss scale | Production PPO config |
| Max gradient norm | 0.5 candidate | Larger permits more aggressive updates | Production PPO config |
| Rollout length | 2048 transitions candidate | Shorter improves iteration latency; longer improves batch statistics | Production PPO config |
| Minibatch / epochs | 64 / 10 candidates | Must divide collected batch and be benchmarked | Production PPO config |
| Network | Shared MLP, 2x256 | Smaller is cheaper; recurrent/set models are later contracts | Production PPO config |
| Advantage normalization | Enabled per update/minibatch contract | Disabled is simpler but scale-sensitive | Production PPO config |
| Evaluation selection | Masked argmax | Seeded stochastic evaluation must be named separately | Evaluation config |
| Device | CPU for contract/smoke | CUDA for experiments after deterministic and checkpoint scope is documented | Runner config |

These are recommended starting values, not claims of optimality and not environment
defaults. `discount_gamma=0.99` is not in this table because it is already frozen by
SharedTrainingDiscountConfig. `shaping_lambda=0.03` is unrelated to `gae_lambda`.

## 28. Readiness

```text
NATIVE_PPO_INTERFACE_READINESS: READY_AFTER_FIX
MASKED_POLICY_DISTRIBUTION_READINESS: READY
GAE_BOOTSTRAP_CONTRACT_READINESS: READY
LEARNER_CHECKPOINT_CONTRACT_READINESS: READY
SINGLE_SEAT_PPO_IMPLEMENTATION_READINESS: READY_AFTER_FIX
MULTISEAT_SELFPLAY_READINESS: NOT_READY
```

`READY_AFTER_FIX` refers to backend, preprocessor and seed authority freeze, not to
an Engine, Decision, Reward, or ActionCatalog defect.

## 29. Required Phase 6B Tests

- **L1** masked categorical never samples an invalid action.
- **L2** every invalid/padded action probability is exactly zero.
- **L3** valid action probabilities sum to one.
- **L4** a single valid action has probability one.
- **L5** all-false mask uses ended path or fails closed before distribution.
- **L6** PPO update reuses the exact stored rollout mask.
- **L7** stored index resolves to the same catalog and action ID.
- **L8** an old-window index cannot resolve against a new catalog.
- **L9** effective gamma comes only from SharedTrainingDiscountConfig.
- **L10** discount identity mismatch fails closed.
- **L11** `gae_lambda` and `shaping_lambda` are independent typed fields.
- **L12** terminated transition bootstraps zero.
- **L13** native and training truncations bootstrap returned next value.
- **L14** active rollout cut bootstraps next value without env truncation.
- **L15** GAE matches a deterministic known vector.
- **L16** returns/value targets match a deterministic known vector.
- **L17** learner consumes, and never recomputes, environment reward.
- **L18** base-only and composite reward variants both train through one interface.
- **L19** shaping/EightFactor values never enter the observation tensor.
- **L20** PPO policy, value, entropy and total losses are finite.
- **L21** every gradient and gradient norm is finite.
- **L22** one nonzero-learning-rate update changes parameters.
- **L23** zero learning rate leaves parameters bitwise unchanged.
- **L24** same CPU seed/config reproduces policy sampling.
- **L25** checkpoint save/load reproduces masked action logits and values.
- **L26** checkpoint authority mismatch is rejected.
- **L27** observation encoder/preprocessor hash mismatch is rejected.
- **L28** action capacity/encoder mismatch is rejected.
- **L29** reward/composite policy mismatch is rejected on resume.
- **L30** shared discount mismatch is rejected.
- **L31** evaluation masked argmax is deterministic, including ties.
- **L32** training categorical sampling always yields a legal action.
- **L33** logs separately sum base, shaping and composite returns.
- **L34** win-rate/outcome evaluation is independent of shaped return.
- **L35** single-seat PPO versus fixed NoOp opponent completes a smoke rollout.
- **L36** environment snapshot and strict replay authority remain unchanged.
- **L37** actor/critic path has no World or GREEN import/read.
- **L38** observation preprocessing is deterministic and hash-bound.
- **L39** observation, logits, probability, loss, value and gradients reject NaN/Inf.
- **L40** policy update cannot mutate environment snapshots, catalogs or reward records.

## 30. Recommended Implementation Phases

1. **Phase 6B -- Minimal Masked PPO Core:** optional PyTorch dependency, versioned
   preprocessor and seed config, masked categorical, rollout batch, GAE, losses,
   known-vector/unit tests.
2. **Phase 6C -- NativeTrainingEnv PPO Runner:** fixed NoOp opponent, checkpoint and
   resume manifest, separate base/shaping/composite logs, deterministic evaluation,
   short CPU smoke.
3. **Phase 6D -- Synchronous Vector Rollout:** isolated per-env seed/catalog/reward
   state and deterministic batch ordering. Evaluate Ray only after this phase.
4. **Phase 6E -- Multi-seat Fixed-opponent and Self-play Contracts:** policy snapshot
   identity, side swap, opponent sampling, nonstationarity, league/evaluation split.
5. **Phase 6F -- EightFactor Auxiliary / CTDE:** separate privileged input and target
   schemas; no actor observation leakage.

## 31. Existing Tests and Probes

The audit relied on existing Decision encoder/action mask, training environment,
multi-seat, reward, discount, EightFactor shaping, transition, replay, fixed-agent,
and training publication contracts. No PPO training was run.

Probe summary:

- Default observation flat shape `(8302,)`, encoder dtype `float64`.
- Catalog count over 34 side/windows: min 3, median 4, p95 48, max 48.
- Six public scenarios exercised: source movement, combined arms, direct fire,
  guided fire, indirect fire and loitering munition.
- Probe-configured decision delta: min/median/p95/max 10,000 ms; repository configs
  also demonstrate other decision intervals.
- Active feature maximum: 1,200,000 (`limited_remaining_ms`).
- Training/native truncation and terminal precedence are covered by existing tests;
  the learner bootstrap behavior specified here remains a Phase 6B test obligation.

## 32. Final Contract Statement

The Native PPO interface, masked action distribution semantics, GAE bootstrap rules,
and checkpoint identity requirements are now explicit. Implementation should not
start until the recommended backend, learner observation preprocessor, and learner
seed authority are frozen. No change is required to Engine, Decision-v1, action
catalog, encoder, base/composite reward, Human, Agent, Viewer, Source, or World
replay authority.

```text
NATIVE_PPO_CONTRACT_REQUIRES_REPAIR
MASKED_ACTION_POLICY_CONTRACT_READY
GAE_BOOTSTRAP_CONTRACT_READY
LEARNER_CHECKPOINT_CONTRACT_READY
```

## Phase 6B0 Prerequisite Closure Verification

Implementation date: 2026-08-30

Phase 6B0 closes the three Phase 6A P1 prerequisites without implementing a
policy distribution, rollout collector, GAE, PPO objective, optimizer, actor,
critic, checkpoint serializer, vector environment, or self-play. Engine,
Decision-v1, action authority, discount, reward, shaping, and replay semantics
remain unchanged.

### Backend authority and dependency boundary

The selected backend is now represented by the immutable value object
`LearnerBackendAuthority` and canonical source
`config/training/native_learner_backend_v1.json`:

```text
schema_version: native-learner-backend-authority/1.0
backend_id: custom-pytorch-ppo
backend_version: 1.0.0
backend_kind: CUSTOM_PYTORCH
framework: pytorch
framework_compatibility: >=2.2,<3
future_algorithm: PPO
semantic_hash: 1ab3ead1c69ca582f80803487135a4961ecd373fcd0354ffcbfd3745a6fa5a3c
```

PyTorch is declared only through the optional project extra
`learner = ["torch>=2.2,<3"]`. No SB3, RLlib, Ray, Gym, or Gymnasium dependency
was added. Importing `wargame_native_engine` or its training package does not
import PyTorch. An explicit learner tensor or seed request lazily loads PyTorch
and raises `LearnerDependencyError` with the `.[learner]` installation command
when unavailable. The runtime PyTorch patch version is recorded separately as
`LearnerBackendRuntimeProvenance`; it does not perturb the backend semantic hash.

This identity freezes the tensor/autograd backend, not a production PPO
algorithm configuration. All PPO research hyperparameters remain outside this
authority.

### Learner observation preprocessor v1

The learner boundary is now:

```text
SideDecisionSemanticProjection
  -> DecisionObservationEncoder v1 float64, shape (8302,)
  -> LearnerObservationPreprocessor v1
  -> CPU float32, shape (8302,)
```

The preprocessor canonical source is
`config/training/native_ppo_observation_preprocessor_v1.json`:

```text
schema_version: native-ppo-observation-preprocessor/1.0
preprocessor_id: native-decision-fixed-scaling
preprocessor_version: 1.0.0
strategy: DETERMINISTIC_FIXED_SCALING
input_encoder_hash: a477d7953c4f3e0a52f13aa2bc12ea08d1327fff7a8bdfe8d91c41a9e51ed750
input_shape/dtype: (8302,) / float64
output_shape/dtype/device: (8302,) / float32 / cpu
semantic_hash: 8f0520cd00d03d4346146f22b4cc8a35290d5868b1281419e4a5baa500ef1554
```

The generated transform table contains 147 canonical feature records and covers
each of the 8,302 input indices exactly once. A record identifies group, feature,
start/end, stride, count, transform, constant, source authority, and any formal
input bound. It is generated from the encoder's exported feature layout and
capacities rather than a second handwritten 8,302-index table. Encoder schema,
hash, layout, shape, dtype, transform ordering, constants, and source authority
identities are all covered by the preprocessor hash. Runtime encoder drift fails
closed even if the shape remains unchanged.

The fixed transforms are:

| Semantic group | v1 transform | Authority / reason |
|---|---|---|
| Existing ratios and bounded flags | identity | Decision encoder semantic |
| Enum/status numeric codes | identity | Preserve Decision-v1 mapping; do not reinterpret as physics |
| `sim_time_ms` | divide by 1,000 | Exact SI millisecond-to-second conversion |
| Own lifecycle/cooldown/recovery `_ms` fields | divide by 1,000 | Exact SI unit conversion |
| `limited_remaining_ms` | divide by 1,200,000 | Operator lifecycle authority version/hash and common configured maximum |
| `segment_progress_millionths` | divide by 1,000,000 | Native progress-millionths contract |
| Snapshot entity/action counts | divide by their encoder capacity | Decision encoder config: 32/64/64/256 |
| Capability instance counts | divide by 256 | Decision action capacity upper bound |
| Position, hex, altitude, absolute health, `max_ticks` | identity | No stable scenario-independent global scale authority; no guessed constant |
| Objective/own/enemy/capability masks and padding | identity | Preserve exact zero/one and zero padding |

All input and output numbers are checked for finiteness; bool, NaN, Inf and
float32 overflow fail closed. There is no clipping, `nan_to_num`, batch statistic,
running mean, learned transform, device selection, or mutable normalization state.
The output is explicitly quantized to IEEE float32 on CPU; `.to_torch_cpu()`
crosses the optional dependency boundary with explicit `dtype=torch.float32` and
`device="cpu"`. The deeply frozen source specification prevents post-hash mutation.

### Timer scaling detail

| Feature family | Transform | Reference source |
|---|---|---|
| Snapshot simulation time | `x_ms / 1000` | SI unit conversion |
| Segment, stop transition, operator transition | `x_ms / 1000` | SI unit conversion |
| Fatigue recovery and suppression | `x_ms / 1000` | SI unit conversion |
| Boarding transition | `x_ms / 1000` | SI unit conversion |
| Weapon cooldown, Guided recovery, Indirect cooldown | `x_ms / 1000` | SI unit conversion |
| Limited lifecycle remaining | `x_ms / 1_200_000` | `native-operator-lifecycle-authority/1.0`, hash `8ab28ce...4623` |

Seconds are a deterministic unit conversion, not episode-relative
normalization. `sim_time_ms` is therefore not divided by an episode horizon.
Other formal timer maxima are not duplicated into learner code. The special
limited-lifecycle scale is accepted only while its authority identity and loaded
profile maximum match the canonical config.

### Raw and processed magnitude probe

The Phase 6A scenario set was sampled again through 17 reset/committed-window
Decision observations (source movement, combined arms, direct fire, Guided,
Indirect, and loitering munition). Statistics below include active entity rows and
exclude padding/mask rows:

| Group | Raw min | Raw median | Raw p95 | Raw max | Processed min | Processed median | Processed p95 | Processed max |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| Snapshot | 0 | 1 | 3,600 | 20,000 | 0 | 0.0234375 | 1,000 | 3,600 |
| Objectives | 0 | 1 | 10 | 10 | 0 | 1 | 10 | 10 |
| Own | 0 | 0 | 7 | 1,200,000 | 0 | 0 | 5 | 114 |
| Observed enemy | 0 | 1 | 9 | 30 | 0 | 1 | 9 | 30 |
| Capability | 0 | 0 | 4 | 20 | 0 | 0 | 4 | 20 |

The 1,200,000 timer risk is reduced to 1.0. The remaining processed maximum of
3,600 is `max_ticks`, for which no stable cross-scenario normalization authority
exists. Hex/position and absolute-health values also remain in native units by
design. This closes the demonstrated extreme conditioning defect; it does not
claim the chosen scaling is empirically optimal. A future running normalizer or
map-aware scaler requires a new, checkpointed learner-preprocessor contract.

### Training seed authority v1

The canonical source `config/training/native_training_seed_v1.json` defines:

```text
schema_version: native-training-seed-config/1.0
config_id: native-training-seeds
config_version: 1.0.0
root_seed: 42
derivation_algorithm: SHA256_CANONICAL_JSON_FIRST_63_BITS_V1
config_hash: 0bf9ddec709ab6048855f6bf86e2338fe5f3b8ef15fdca99a98e9fdf092aa1f3
```

Every child seed is the lower 63-bit positive integer represented by the first
eight bytes of SHA-256 over canonical JSON containing root seed, stream namespace,
worker index, environment index, episode index, and stream index. Python `hash()`
is not used. V1 namespaces are fixed in this order:

| Stream | Default derived seed | Ownership |
|---|---:|---|
| `ENVIRONMENT` | 1925888228136961638 | Episode/scenario seed passed explicitly to the existing Engine boundary |
| `POLICY_INITIALIZATION` | 5703913235427534008 | Future network initialization |
| `POLICY_SAMPLING` | 6345138234774943540 | Future masked categorical sampling |
| `MINIBATCH_SHUFFLE` | 7181209399771913262 | Future PPO batch ordering |
| `TORCH_CPU` | 5074599256022357151 | Explicit PyTorch CPU RNG initialization |
| `TORCH_CUDA` | 2477975436582164953 | Explicit optional CUDA RNG initialization |
| `WORKER` | 1467190557339822168 | Worker-level orchestration |
| `EVALUATION` | 5844164746449250228 | Evaluation split from training sampling |

The Engine continues to own `NativeRNG` and the episode seed it receives.
`TrainingSeedConfig` derives the value a future runner will pass; it does not
replace or mutate Engine RNG state. Python learner RNG helpers return isolated
`random.Random` instances. Torch helpers are explicit calls and never run at
module import. Policy sampling, minibatch shuffling, evaluation, worker, and
Engine streams do not share mutable RNGs. CPU sequence reproducibility is in
scope for compatible runtime/version, fixed parameters, observation and mask;
cross-GPU/CUDA/cuDNN bitwise reproducibility is not guaranteed.

### Verification matrix

All Phase 6B0 prerequisite obligations are formal tests in
`tests/test_ppo_prerequisite_closure.py`:

| Test | Result | Contract evidence |
|---|---|---|
| PP1 | PASS | Same semantic input yields identical values and tensor hash |
| PP2 | PASS | Shape remains `(8302,)` |
| PP3 | PASS | Output dtype and Torch boundary are explicit float32 |
| PP4 | PASS | Encoder hash mismatch rejected |
| PP5 | PASS | NaN rejected |
| PP6 | PASS | positive/negative Inf rejected |
| PP7 | PASS | All entity masks remain exact 0/1 |
| PP8 | PASS | Padding remains zero |
| PP9 | PASS | Timers, progress and formal counts match fixed formulas |
| PP10 | PASS | No batch statistics |
| PP11 | PASS | No running mutable state; source spec deeply immutable |
| PP12 | PASS | Preprocessor canonical hash deterministic |
| PP13 | PASS | Scaling change changes hash |
| PP14 | PASS | Input/config ordering does not affect hash |
| PP15 | PASS | Decision encoder schema/hash/layout unchanged |
| PP16 | PASS | No GREEN, EightFactor, reward or winner enters learner input |
| SD1 | PASS | Same config and cached load yield same hash |
| SD2 | PASS | Root seed change changes hash |
| SD3 | PASS | Same namespace/coordinates yield same child seed |
| SD4 | PASS | Different namespaces yield distinct child seeds |
| SD5 | PASS | Worker index changes child seed |
| SD6 | PASS | Environment index changes child seed |
| SD7 | PASS | Derivation source contains no Python `hash()` call |
| SD8 | PASS | Learner derivation/sampling leaves `NativeRNG` untouched |
| SD9 | PASS | Evaluation and policy-sampling streams differ |
| SD10 | PASS | Explicit CPU Torch seed reproduces the sequence |
| SD11 | PASS | bool, negative and out-of-range root seeds rejected |
| SD12 | PASS | Namespace order and canonical hash deterministic |

Backend tests additionally verify strict schema/config identity, optional dependency
placement, lazy import, clear dependency failure, and the separation of runtime
patch provenance from semantic identity.

Regression verification completed with 16 Phase 6B0 targeted tests, 204 related
Decision/training/reward/replay/consumer tests, and the complete repository suite:
`1998 passed, 10 skipped, 0 failed` in 377.69 seconds. Engine import isolation was
also probed in the environment where PyTorch is absent.

### Compatibility and checkpoint join

Phase 6B0 adds no aggregate learner hash. A future PPO configuration and checkpoint
must reference these independent identities:

```text
backend_semantic_hash
preprocessor_semantic_hash
training_seed_config_hash
shared_discount_config_hash
future_ppo_algorithm_config_hash
```

This preserves the reason for each mismatch and avoids a redundant fourth
authority. Resume must fail closed on any required identity mismatch. The
deterministic evaluation selection remains `MASKED_ARGMAX`; no policy network is
implemented here.

The frozen existing identities remain:

```text
Decision observation encoder: a477d7953c4f3e0a52f13aa2bc12ea08d1327fff7a8bdfe8d91c41a9e51ed750
Decision action encoder: aa08ad27d84634f76d607d7890abe953a911b57b4043b311fdc85dd2813b83dc
Shared discount: f35a748f9d0232506135b7e2438fa64f5bcc601a7aeb1fc631b182f396e4f06e
Base reward: 5653b7653eb46d3f091d5d0c5c04ab7dd5ce380236a4d2455214ab735a39b0cf
EightFactor shaping: bc21e92be00f9f9243b436741321f7d326f51a8bb734386a6de880dad37d7e81
```

### P1 closure and readiness override

The Phase 6A findings are reclassified as follows:

```text
LEARNER_OBSERVATION_PREPROCESSOR_CONTRACT_MISSING: CLOSED
LEARNER_BACKEND_AUTHORITY_MISSING: CLOSED
LEARNER_SEED_AUTHORITY_MISSING: CLOSED

MASKED_CATEGORICAL_IMPLEMENTATION_READINESS: READY
GAE_IMPLEMENTATION_READINESS: READY
PPO_CORE_IMPLEMENTATION_READINESS: READY
CHECKPOINT_IMPLEMENTATION_READINESS: READY
SINGLE_SEAT_RUNNER_READINESS: READY
```

Research values for `gae_lambda`, clip epsilon, learning rate, entropy/value
coefficients, max gradient norm, rollout length, minibatch size, update epochs,
and network architecture remain intentionally unfrozen. `discount_gamma=0.99`
continues to come only from SharedTrainingDiscountConfig;
`shaping_lambda=0.03` remains a separate EightFactor policy parameter.

This section supersedes the earlier Phase 6A `READY_AFTER_FIX` result:

```text
CUSTOM_PYTORCH_LEARNER_BACKEND_FROZEN
LEARNER_OBSERVATION_PREPROCESSOR_V1_READY
LEARNER_SEED_AUTHORITY_V1_READY
NATIVE_PPO_PREREQUISITES_CLOSED
MINIMAL_MASKED_PPO_CORE_IMPLEMENTATION_READY
```

## Phase 6B Minimal Masked PPO Core Verification

Implementation date: 2026-08-30

Phase 6B implements only the tensor-level learner core. It does not reset or step
an environment, collect live rollouts, bind a live catalog index to an action ID,
run a fixed opponent, publish episode metrics, train for convergence, vectorize,
or implement self-play.

### PPO algorithm identity

Canonical source: `config/training/native_masked_ppo_v1.json`.

```text
schema: native-ppo-config/1.0
algorithm_id: native-masked-ppo
algorithm_version: 1.0.0
config_hash: d0b08a8295b22b316d8082272fa3763f491961d07934f2ac2c2b84c951fa622d
backend_hash: 1ab3ead1c69ca582f80803487135a4961ecd373fcd0354ffcbfd3745a6fa5a3c
preprocessor_hash: 8f0520cd00d03d4346146f22b4cc8a35290d5868b1281419e4a5baa500ef1554
action_encoder_hash: aa08ad27d84634f76d607d7890abe953a911b57b4043b311fdc85dd2813b83dc
action_capacity: 256
discount_hash: f35a748f9d0232506135b7e2438fa64f5bcc601a7aeb1fc631b182f396e4f06e
seed_hash: 0bf9ddec709ab6048855f6bf86e2338fe5f3b8ef15fdca99a98e9fdf092aa1f3
```

The config contains no numeric `gamma`. Runtime gamma is resolved from the
referenced `SharedTrainingDiscountConfig`; each rollout transition stores its
effective gamma so future time-based discounting does not require a batch-wide
constant. Runtime authority mismatch fails closed.

The reproducible baseline—not a tuned or optimal claim—is:

| Field | V1 value |
|---|---:|
| `gae_lambda` | 0.95 |
| clip epsilon | 0.20 |
| constant learning rate | 0.0003 |
| entropy coefficient | 0.01 |
| value coefficient | 0.50 |
| max gradient norm | 0.50 |
| rollout capacity | 2048 |
| minibatch size | 64; final partial minibatch allowed |
| update epochs | 10 |
| advantage normalization | update-batch, epsilon `1e-8` |
| network | shared MLP 8302 -> 256 -> 256 |
| activation | Tanh |
| policy/value heads | 256 / 1 |
| initialization | orthogonal hidden gain sqrt(2), policy 0.01, value 1.0 |
| optimizer | Adam `(0.9,0.999)`, epsilon `1e-8`, weight decay 0 |
| device / precision | CPU / float32; no AMP |
| evaluation | deterministic masked argmax, lowest legal tie index |

`gae_lambda=0.95`, `discount_gamma=0.99`, and
`shaping_lambda=0.03` are separate typed authorities. Changing GAE lambda changes
the PPO hash but not the EightFactor shaping hash.

### Masked distribution and actor-critic

`MaskedCategorical` requires float32 logits and a same-shape bool mask. It masks
logits before `log_softmax`; invalid probabilities are exact zero and legal
probabilities normalize to one. An all-false row raises `AllActionsMaskedError`.
Sampling requires an explicit `torch.Generator` derived from the
`POLICY_SAMPLING` seed stream. Invalid `log_prob` requests fail closed. Entropy is
computed only from legal normalized terms; one legal action has zero entropy.

`masked_argmax` uses the same fail-closed mask and PyTorch's first-index tie rule.
The actor-critic accepts only finite CPU float32 tensors with final dimension
8302. It has no Dropout, BatchNorm, recurrent, set, privileged, or auxiliary path.
Its action width is the referenced action encoder capacity, not an independent
constant. Initialization uses an explicit `POLICY_INITIALIZATION` generator and
does not depend on import-time global seeding.

The PPO core imports no Engine, World, GREEN/analysis, Gateway, projection,
ActionCatalog query, or reward implementation. It sees only tensors, masks,
detached rollout values, config, discount identity, and seed identity.

### Rollout and GAE

`LearnerTransition` schema `native-ppo-learner-transition/1.0` stores immutable
copied float/mask data plus episode, side, Decision hash, catalog hash, action
encoder hash, action index/action ID, old log probability/value, scalar reward and
RewardRecord hash, base/shaping amounts, terminal/truncation provenance,
transition-specific gamma, next observation/Decision/value, bootstrap/trace flags,
and preprocessor hash. It cannot hold live environment authority objects.

`PPORolloutBatch` schema `native-ppo-rollout-batch/1.0` owns detached cloned tensors:

```text
observations float32 [T,8302]
action_masks bool [T,256]
actions int64 [T]
old_log_probs, old_values, rewards float32 [T]
terminated, truncated bool [T]
effective_gammas, next_values float32 [T]
bootstrap_allowed, trace_continues bool [T]
advantages, value_targets float32 [T]
```

It supports stacking from transitions, deterministic index/minibatch slicing, and
device transfer without mutating source tensors. The stored rollout mask is always
used during update.

GAE is:

```text
delta_t = reward_t
          + gamma_t * bootstrap_allowed_t * next_value_t
          - value_t
advantage_t = delta_t
              + gamma_t * gae_lambda * trace_continues_t * advantage_t+1
value_target_t = advantage_t + value_t
```

Terminated transitions set bootstrap and trace false. Truncation bootstraps the
committed next value but stops recurrence. An active rollout cut also bootstraps
while stopping recurrence without changing environment truncation. Known-vector
tests use different gamma values per transition.

### PPO objective and update

Advantages are normalized once over the update batch using population standard
deviation and explicit epsilon; size-one and zero-variance batches produce finite
zero advantages. Policy loss is the standard minimum of unclipped and clipped
surrogates. Value loss is `0.5*MSE` with no value clipping. Total loss is:

```text
policy_loss
+ value_coefficient * value_loss
- entropy_coefficient * legal_entropy_mean
```

Adam parameters are explicit. Each epoch uses a deterministic permutation from
the `MINIBATCH_SHUFFLE` generator; a final partial minibatch is processed. Update
fails closed on non-finite inputs, losses, gradients, gradient norm, or parameters.
Gradient norm is measured before clipping and clipped to the configured maximum
before the optimizer step. No KL early stopping, value clipping, reward
normalization/clipping, LR schedule, observation re-normalization, or mixed
precision is present.

`PPOUpdateMetrics` returns policy loss, value loss, legal entropy, total loss,
`mean(old_log_prob-new_log_prob)` approximate KL, fraction with
`abs(ratio-1)>clip_epsilon`, pre-clip/clipped gradient norm, optimizer-step count,
and update number. Diagnostics do not alter optimization control flow.

### Checkpoint core

The JSON manifest schema is `native-ppo-checkpoint/1.0`; tensor/binary state stays
separate. An in-memory checkpoint payload contains model and optimizer state,
PPO/backend/preprocessor/action/discount/seed identities, expected training reward
identity, action capacity, global CPU RNG state, independent sampling/shuffle
generator states, update step, and sample step. Restore verifies all semantic
identities before loading state. Base-only versus composite reward mismatch,
config mismatch, encoder/preprocessor/action capacity mismatch, and discount
mismatch fail closed. Tests verify model output and RNG continuation after restore.
A file manager/publication registry remains Phase 6C scope.

### Formal verification and deferred integration

Phase 6B fixture tests close L1-L6, L9-L17, L19-L32, L37-L40 where the obligation
belongs to tensor/core or checkpoint semantics. L7/L8 retain their existing
catalog/stale-window regression but require live rollout binding in Phase 6C.
L18, L33-L35 and full learner-runner L36 are deferred to Phase 6C.

PC1-PC20 pass: extreme logits, zero entropy, invalid-logit noninterference,
detached old log probabilities, both clipping directions, known value loss,
zero entropy/value coefficients at the loss contract, effective gradient clipping,
deterministic minibatches/epoch counts, network/mask validation, checkpoint RNG,
hash determinism, and static actor-path isolation.

Verification totals: 16 Phase 6B core tests passed in 1.69 seconds; 220 related
learner-prerequisite, Decision, training, reward, replay, and consumer tests passed
in 34.95 seconds; the complete repository suite finished with
`2014 passed, 10 skipped, 0 failed` in 385.17 seconds. No long environment
training was run.

The remaining integration items are exactly: `NativeTrainingEnv` rollout
collection; live observation/mask/catalog/action-ID binding; fixed-opponent runner;
base/composite selection; episode base/shaping/composite metrics and win rate;
checkpoint file/runner wiring; deterministic evaluation runner; and short CPU
real-environment smoke. No self-play or long training belongs in Phase 6C.

```text
NATIVE_MASKED_PPO_CORE_V1_IMPLEMENTED
MASKED_CATEGORICAL_CONTRACT_CLOSED
GAE_BOOTSTRAP_IMPLEMENTED
PPO_UPDATE_CORE_VERIFIED
PPO_CHECKPOINT_CORE_READY
NATIVE_PPO_RUNNER_INTEGRATION_READY
```

## Phase 6C Native PPO Runner Verification

Implementation date: 2026-08-30

Phase 6C connects the existing tensor-only PPO core to the real
`NativeTrainingEnv` without adding adjudication, legality, observation, or reward
formulas. The runner is synchronous and single-environment. It trains exactly one
configured side against the existing deterministic `NoOpAgent`; self-play,
multi-learner PPO, vector environments, Ray, CTDE, and EightFactor auxiliary heads
remain outside v1.

### Runner authority and data flow

The canonical production source is
`config/training/native_ppo_runner_v1.json`:

```text
schema: native-ppo-runner-config/1.0
runner_id: native-single-seat-ppo-runner
runner_version: 1.0.0
config_hash: e024f1d0c0db62ec87f042bdc6d284ec224bb777025f2c838025e8d2c3194141
purpose: BASELINE_V1
mode: TRAINED_SIDE_VS_FIXED_OPPONENT
trained_side: RED (configurable RED/BLUE)
fixed_opponent: native-no-op-fixed-opponent-v1
reward_mode: BASE_ONLY (explicitly configurable COMPOSITE_EIGHT_FACTOR)
device: CPU
rollout_model_mode: EVAL_NO_GRAD
```

The production PPO config remains unchanged at rollout length 2048 and ten update
epochs. Real-environment tests derive an explicitly labelled `TEST_SMOKE` config
with rollout length 2, minibatch 2, one epoch, and an 8x8 hidden fixture network;
that config has its own hash and is not a production default.

The live path is:

```text
Native Engine / Gateway
-> detached ControllerView
-> Decision-v1 semantic projection
-> DecisionObservationEncoder float64[8302]
-> fixed LearnerObservationPreprocessor float32[8302]
-> masked ActorCritic
-> action index
-> the same frozen ActionCatalogSnapshot / action_id
-> NativeTrainingEnv.step_bound_action
-> Gateway same-pre-state RED/BLUE submission
-> committed Engine transition
-> authoritative TrainingRewardRecord
-> detached LearnerTransition / PPORolloutBatch
-> GAE / PPO update
```

`TrainingDecisionBinding` exposes only the immutable encoded Decision and encoded
catalog. `step_bound_action` compares the Decision hash, catalog hash, window,
frame, simulation time, and action-encoder hash immediately before submission.
An index from an old window raises `bound_action_snapshot_stale`; it is never
resolved against a new catalog. The selected action ID is subsequently joined to
the Reward TransitionIdentity's exact submitted action IDs and the next Decision
hash. The runner never creates an `EngineAction` and never queries rules.

The fixed opponent is the existing `NoOpAgent`. It receives its own detached side
observation and valid-action list through `SessionManager.agent_step_input`; its
empty submission goes through the same Gateway decision window. Both seats are
therefore based on one pre-state. It cannot read the learner's logits, value,
gradient, or reward components, and it receives no GREEN frame.

### Rollout, reward and bootstrap

`NativePPORunner.collect_rollout` fills at most the referenced PPO rollout length,
resetting with a newly derived episode seed after true termination or truncation.
An active rollout cutoff does not modify the environment's flags: the final next
value is bootstrapped while the GAE trace stops at the batch boundary. Rule
termination has no bootstrap; native or training truncation bootstraps the
committed next Decision value and stops the episode trace.

Each live `LearnerTransition` populates all Phase 6B provenance fields with real
values: episode/side, preprocessed observation and stored mask, Decision/catalog/
encoder hashes, index and action ID, detached log probability/value, RewardRecord
hash and scalar, base and shaping component amounts, terminal/truncation source,
transition-specific shared gamma, next observation/Decision/value, and bootstrap
flags. No live World, Engine, ControllerView, mutable catalog, GREEN frame, or
FrameAnalysis is stored.

The runner reads component amounts from `TrainingRewardRecord.components`; it
does not infer them from the reward float and never recomputes reward. `BASE_ONLY`
requires an environment with shaping disabled. `COMPOSITE_EIGHT_FACTOR` requires
an environment with the production shaping authority. A mismatch fails at runner
construction. Enabling shaping changes only reward supervision: deterministic
paired tests produced identical Decision tensors, processed tensors, masks, and
action-ID sequences.

Episode metrics schema `native-ppo-episode-metrics/1.0` records Engine-derived
winner/outcome, terminal reason, truncation separately, decision count,
simulation time, base/shaping/composite returns, reward identity, action IDs, and
RewardRecord hashes. A truncation is its own `TRUNCATED` outcome bucket and is not
counted as a loss. Metrics contain no tensor, World, or GREEN payload.

### Seeds, checkpoints and resume

Environment episodes use the `ENVIRONMENT` stream with explicit worker, env, and
episode coordinates. Stochastic training actions use a separately reseeded
`POLICY_SAMPLING` generator. Evaluation uses `EVALUATION` seeds and masked argmax;
it neither samples from nor advances the training generator. Engine RNG remains
owned by the Engine episode seed.

Runner checkpoints are directories containing `manifest.json` and `state.pt`.
Both are first written in a sibling temporary directory and atomically exposed by
rename. The manifest schema is `native-ppo-runner-checkpoint/1.0` and binds:

- runner/PPO/backend/preprocessor/action encoder/action capacity/discount/seed;
- Engine version, ruleset ID/version/hash, perspective policy and Decision schema;
- base or composite reward identity and full reward authority;
- shaping policy and analyzer identity when composite reward is enabled;
- model parameter content hash, optimizer hash, RNG-state hash and state-file hash;
- update, sample, episode and environment-step counters.

Restore verifies the file hash and every runtime authority before loading binary
state. Base/composite, analyzer/shaping, preprocessor, action capacity, discount,
seed, ruleset, perspective, Decision, or PPO mismatch fails closed. CPU RNG,
policy sampling and minibatch shuffle states resume at the next value. V1 saves at
a completed rollout/update boundary; it intentionally embeds no environment
snapshot, and resume starts a new episode.

### Evaluation and publication

`evaluate_checkpoint` performs fail-closed restore followed by optimizer-free
`MASKED_ARGMAX` evaluation. The report schema
`native-ppo-evaluation-report/1.0` records a stable evaluation seed-set hash,
action sequences, WIN/LOSS/DRAW/TRUNCATED buckets, win/loss/draw rates over
completed episodes, base/shaping/composite return mean/std, episode-length
mean/std, and reason counts. The primary metric is Engine-authoritative outcome/
win rate; shaped return is auxiliary and cannot establish policy improvement.

When an artifact root is configured, publication reuses the environment's normal
Source replay/analysis package and adds versioned learner artifacts:
`run_manifest.json`, `episode_metrics.jsonl`, `update_metrics.jsonl`,
`run_summary.json`, numbered checkpoint directories, an evaluation report, and a
per-episode RewardRecord JSONL. Learner publication does not become World replay
authority and contains no full tensors or privileged frames.

### Real-environment smoke and verification

The base smoke used the normal deterministic movement scenario. The composite
smoke used the same Native scenario and Engine with a detached Source-frame test
template containing complete v1 EightFactor evidence; this is necessary because
the production `REQUIRE_COMPLETE_NONEMPTY_SIDES / FAIL_CLOSED` policy correctly
rejects incomplete analysis rather than substituting zero shaping.

Both RED and BLUE paths reset, encoded Decision-v1, sampled only legal actions,
resolved exact action IDs, committed through Gateway, returned aligned rewards,
built GAE batches, performed a finite optimizer update, and changed parameters.
Base and composite smokes each used two real transitions. The composite shaping
return was nonzero. A saved checkpoint evaluated through deterministic masked
argmax without changing optimizer/sample counters, parameters, or training RNG.

One lightweight operational probe (not a benchmark or convergence claim) measured:

| Smoke | transitions | rollout seconds | update seconds | eval episode seconds | transitions/s |
|---|---:|---:|---:|---:|---:|
| base-only | 2 | 0.3111 | 0.0299 | 0.2442 | 6.43 |
| composite | 2 | 0.3188 | 0.0093 | 0.2488 | 6.27 |

The published base smoke replay passed strict Engine replay, and every learner
action ID and side RewardRecord hash joined its corresponding committed
transition. Existing reward re-derivation suites remain green; PPO rollout data is
derived learning data and is never trusted as replay authority.

Before an episode metric is accepted, `NativeTrainingEnv.verify_reward_rederivation`
rebuilds every base reward from committed transition/result provenance and, when
enabled, recomputes EightFactor potential shaping from the committed endpoint
frame analyses and bound analyzer/discount/shaping identities. The expected and
re-derived joint sequence hashes and records must match exactly; failure aborts
the runner rather than publishing a metric.

### R1-R40 and L1-L40 closure

R1-R13 pass for reset/preprocessing, exact mask/index/action binding, stale
rejection, reward-next-Decision alignment, typed transition construction,
termination/truncation bootstrap, rollout cutoff, deterministic episode seeds,
and both trained sides. R14-R15 retain the existing same-pre-state Gateway and
side-observation isolation regressions with the selected NoOp opponent. R16-R21
pass for both reward modes, observation isolation, component totals,
Engine-authoritative outcome, and truncation accounting. R22-R24 pass for
deterministic argmax, no update, and evaluation/training RNG isolation. R25-R34
pass for real rollout/update, parameter change, atomic checkpoint/RNG continuation,
and fail-closed runner/PPO/preprocessor/action/discount/reward/analyzer authority.
R35-R38 pass for strict replay, reward/action joins, and deterministic evaluation
sequences. R39-R40 report zero invalid and zero stale attempts on the normal path.

The previously deferred L7, L8, L18, L33, L34, L35 and runner-level L36 are now
closed. L1-L6, L9-L17, L19-L32, and L37-L40 remain regression-green. In
particular, the learner uses stored rollout masks, does not recompute reward,
never exposes shaping in actor input, and does not mutate environment snapshots.

Verification totals: 17 Phase 6C runner tests passed in 8.40 seconds; 330 related
PPO, training environment, MultiSeat, Decision, Gateway/live-control, reward,
replay/publication, Agent, Human, Viewer and Source tests passed in 54.10 seconds;
the complete repository suite finished with `2031 passed, 10 skipped, 0 failed`
in 393.76 seconds on the final code state.

### Findings and next readiness

```text
P0: NONE
P1: NONE

PPO_ROLLOUT_BATCH_SCHEMA_MISSING: CLOSED
LEARNER_CHECKPOINT_AND_RESUME_MISSING: CLOSED
LEARNER_EVALUATION_PUBLICATION_MISSING: CLOSED
SINGLE_SEAT_PRODUCT_ADAPTER_IS_NOT_INDEX_BASED: CONTAINED_BY_NATIVE_TRAINING_ENV_BOUND_INDEX_PATH

VECTOR_ENV_MISSING: P2 / expected Phase 6D
SELF_PLAY_CONTRACT_MISSING: P2 / not ready
CTDE_CONTRACT_MISSING: P2 / deferred
EIGHT_FACTOR_AUXILIARY_TARGET_NOT_IMPLEMENTED: P2 / implementation-ready, deferred
```

The single-seat fixed-opponent path is ready. `NativeMultiSeatEnv` remains
regression-green, but Phase 6C deliberately does not claim that independent PPO,
self-play, league, or CTDE contracts exist. A synchronous vector collector can be
built next from isolated runner/env instances; no mutable catalog, RNG, or
potential cache may be shared.

```text
SYNCHRONOUS_VECTOR_ROLLOUT_READINESS: READY
MULTISEAT_FIXED_OPPONENT_READINESS: READY
SELF_PLAY_CONTRACT_READINESS: NOT_READY
AUXILIARY_EIGHT_FACTOR_READINESS: READY

NATIVE_PPO_RUNNER_V1_IMPLEMENTED
REAL_ENV_PPO_ROLLOUT_VERIFIED
LIVE_ACTION_CATALOG_BINDING_CLOSED
PPO_CHECKPOINT_RUNNER_READY
DETERMINISTIC_PPO_EVALUATION_READY
FIXED_OPPONENT_PPO_BASELINE_READY
NATIVE_ENGINE_END_TO_END_RL_BASELINE_READY
```

## Phase 6D Synchronous Vector Rollout Verification

### Vector authority and scope

Phase 6D adds the production vector-runner authority below without changing the
production PPO, single-runner, encoder, discount, reward, Engine, or World
authorities:

```text
schema: native-ppo-vector-runner-config/1.0
runner_id: native-synchronous-vector-ppo-runner
runner_version: 1.0.0
config_hash: b95c355a38a854b9426c971e07501b4529b801f782d1f32ed30016425e0a7a34
num_envs: 4 (explicit and configurable; never derived from host CPU count)
collection_mode: SYNCHRONOUS
reset_policy: AUTO_RESET_COMPLETED_ENV
rollout_target: PPO_CONFIG / TOTAL_TRANSITIONS_PER_UPDATE
transitions_per_update: 2048
flatten_order: ENV_MAJOR_THEN_COLLECTION_ORDER
error_policy: FAIL_WHOLE_VECTOR
reward_verification: VERIFY_EVERY_COMPLETED_EPISODE
device: CPU
```

`rollout_length` retains its Phase 6B/6C meaning: it is the total number of
committed transitions in one PPO update, not a per-environment horizon. With
four environments, production 2048 therefore requires approximately 512 full
vector steps. If the final step needs fewer than `num_envs` transitions, only a
stable prefix of active environment indices is stepped. No Engine transition is
committed and then discarded.

The configuration binds the exact single-runner, PPO and seed identities. It
also freezes same-scenario/different-derived-seed assignment, uniform trained
side and reward mode, and `ENV_MAJOR_THEN_COLLECTION_ORDER` flattening. Changing
`num_envs` changes the vector config hash but does not change the referenced PPO
config hash.

### Isolation, batched inference and live binding

`NativeSynchronousVectorPPORunner` accepts exactly `num_envs` distinct
`NativeTrainingEnv` instances. Construction rejects duplicate environment or
mutable opponent objects. Each environment owns a separate SessionManager,
Session/Engine/World, ControllerView, Decision/catalog, reward history,
NoOpAgent instance, episode state and Engine seed. Locally generated session ID
strings may coincide across separate managers; isolation identity is the
manager/session pair, not the display string alone. Runtime Engine/ruleset,
perspective, Decision and encoder authorities and scenario IDs must agree across
the vector, otherwise collection fails closed.

One synchronous policy step performs:

```text
active env_i Decision-v1 + frozen catalog_i
-> existing fixed preprocessor per row
-> stack float32 [B, 8302] and bool [B, 256]
-> one existing ActorCritic forward: logits [B, 256], values [B]
-> existing MaskedCategorical per row
-> explicit batch_row -> env_index mapping
-> action_index_i -> observation-time catalog_i -> action_id_i
-> independent NativeTrainingEnv.step_bound_action for each env_i
-> batch value inference over committed non-terminal post states
-> authoritative RewardRecord join
```

There is no vector semantic encoder, union/intersection mask, fake done row,
second network, vector-specific PPO loss, or learner-side legality rule. Every
invalid probability is exactly zero per row, and active all-false rows fail
closed. A stale Decision/catalog snapshot raises the existing bound-action error
and aborts the synchronous vector collection. Normal smokes recorded zero
invalid and zero stale attempts.

The fixed opponent remains `native-no-op-fixed-opponent-v1`. Every environment
holds its own instance, and its action is still obtained through the normal
detached opponent side path and joined to the same pre-state Gateway decision
window. Vectorization is multiple independent games, not a MultiSeat joint game.

### Per-environment RNG and auto-reset

Each environment owns a separate CPU `torch.Generator` for policy sampling. It
is derived from the versioned `POLICY_SAMPLING` namespace plus worker index,
derived env index and episode index. Environment seeds use the separate
`ENVIRONMENT` namespace; deterministic evaluation uses `EVALUATION` and masked
argmax. Resetting or shortening env 0 therefore neither consumes nor reseeds env
1's policy stream. Root seed equality does not create identical replicas because
the env-index coordinate differs.

Completed transitions are recorded before auto-reset. For `terminated=True`,
the endpoint value is exactly zero. For `truncated=True`, the committed post-
state Decision is preprocessed and valued before reset; the new episode's
initial observation can never leak backward into the truncated transition.
Only the completed environment resets while others remain active. Policy
batches contain active rows only, with an explicit row-to-env mapping.

### Per-environment GAE and flattening

Collection internally stores one ordered `VectorTransitionRecord` segment per
environment under schema `native-ppo-vector-rollout/1.0`. Records bind env,
vector-step and collection indices, scenario ID, rollout policy update version,
and the existing immutable `LearnerTransition`. All records in a rollout must
share one policy update version; the policy is not updated mid-collection.

At the rollout boundary, the last active transition in every participating env
stops the recurrence while retaining its own next-value bootstrap. Existing
`compute_gae` is invoked independently for each env segment. Termination and
truncation already stop recurrence within an env, so GAE crosses neither env nor
episode boundaries. Only after GAE does the runner flatten env index ascending,
then collection order, into the existing `PPORolloutBatch`. The existing
`NativeMaskedPPO.update` consumes that batch unchanged: one shared policy and
optimizer, one deterministic minibatch shuffle authority.

### Metrics, publication, checkpoint and evaluation

The collector reports vector steps, exact committed-transition count, per-env
invalid/stale counters and operational time split for preprocessing, policy
forward/value inference, Engine stepping, reward/replay verification and total
rollout. `native-ppo-vector-episode-metrics/1.0` wraps the existing Engine-
authoritative episode metrics with env index, scenario ID and stable completion
metadata. Base, shaping and composite returns remain separate. Outcome/winner
continues to come from Engine result; truncation is never counted as loss.

When artifacts are enabled, each environment publishes its normal replay and
RewardRecord sequence under an env-indexed directory, plus vector episode and
update JSONL. Full tensors, World, GREEN and mutable session state are excluded.
Every completed training/evaluation episode still requires strict reward
re-derivation before its metric is accepted.

Vector checkpoint directories use schema `native-ppo-vector-checkpoint/1.0` and
the existing atomic `manifest.json` + `state.pt` layout. The manifest binds the
vector/base runner, PPO/core, all runtime authorities, uniform reward authority,
seed identity, counters, model hash and per-env generator provenance. Loading
for deterministic evaluation validates all identities and restores the PPO
state. Exact mid-training vector continuation is deliberately not claimed:
live Engine states are not embedded, so `VECTOR_TRAIN_EXACT_RESUME_MISSING`
remains P2. Supported v1 meanings are vector checkpoint save and
`LOAD_CHECKPOINT_FOR_EVALUATION`/new-run policy initialization, not exact live-
episode continuation.

Vector evaluation batches masked argmax over active rows and sorts its semantic
report by evaluation seed/env/episode. It records Engine outcome win/loss/draw/
truncated buckets, return and episode-length mean/std, and terminal reason
counts. It changes no optimizer/model/sample state and consumes no training
sampling RNG. Two-env vector evaluation matched sequential single-runner
evaluation using the exact corresponding env-index seed coordinates, action IDs,
RewardRecord hashes and outcomes. `num_envs=1` also matched the existing single
runner under the same checkpoint, scenario and deterministic seed.

### V1-V40 and D1-D8

V1 passes the single-runner differential. V2-V5 pass distinct environment,
manager/session-pair, opponent and derived-seed isolation, including env-0 reset
independence from env 1. V6-V11 pass `[B,8302]` float32 observations, `[B,256]`
bool per-row masks, explicit subset row mapping, done-row exclusion, exact-zero
invalid probability and legal sampling. V12-V14 pass per-env frozen catalog/
action-ID binding, fail-closed stale submission, and distinct opponent state.

V15-V18 pass terminal-zero bootstrap regression, truncated committed-post-state
bootstrap, reset-value isolation, and per-env rollout-boundary bootstrap. V19-V20
pass zero dropped transitions and exact total-transition targets including a
partial final vector step. V21-V24 pass independent GAE, no cross-env/episode
trace, and stable env-major flattening. V25-V27 pass reuse of the existing PPO
batch/update, shared parameter update and one policy version per rollout.
V28-V32 pass uniform base/composite modes and RED/BLUE vector paths; reward-mode
changes leave observation/mask/action IDs unchanged. V33-V34 pass vector-vs-
sequential deterministic evaluation and stable ordering. V35-V36 record zero
normal-path invalid/stale attempts. V37-V38 pass strict Engine replay, reward
re-derivation and action/reward joins for each smoke env. V39-V40 pass stable
vector config hashing and hash change on `num_envs` change.

D1-D5 pass reproducible vector action sequences, RewardRecord hashes, PPO update
metrics, final parameter content hash and stable env/flatten ordering. D6 passes
reset-timing RNG independence. D7 passes vector/sequential evaluation equality.
D8 passes distinct derived seeds for different env indices.

### Operational probe and bottleneck

The following single-run CPU measurements are smoke/probe data only. They are
not a benchmark, speedup claim, scaling-efficiency claim, convergence result or
hyperparameter recommendation.

| mode | envs | transitions | rollout s | preprocess s | policy s | Engine s | reward/replay s | update s | transitions/s |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| existing single runner | 1 | 16 | 2.4961 | n/a | n/a | n/a | n/a | 0.0488 | 6.41 |
| synchronous vector | 1 | 16 | 2.4798 | 0.8313 | 0.0203 | 1.1266 | 0.0107 | 0.0331 | 6.45 |
| synchronous vector | 2 | 32 | 5.4476 | 1.7635 | 0.1882 | 2.4970 | 0.0232 | 0.0991 | 5.87 |
| synchronous vector | 4 | 64 | 10.1689 | 3.2407 | 0.2768 | 4.7014 | 0.0452 | 0.1528 | 6.29 |

The largest instrumented cost was `ENGINE`; fixed preprocessing was second.
Engine commits remain intentionally serial in this phase, so batched inference
does not imply linear CPU throughput growth. The result supports a later
parallel-environment execution audit; it does not authorize multiprocessing,
Ray or asynchronous semantics in v1.

### Verification, findings and next readiness

Phase 6D targeted vector tests finished with `22 passed, 0 failed`. A related
PPO core/single runner/training environment/MultiSeat/Decision/reward/shaping/
replay/publication/Agent/Human/Viewer/Source/capability set finished with
`408 passed, 0 failed`. The final full-suite result is recorded below after the
repository-wide verification run: `2053 passed, 10 skipped, 0 failed` in
406.40 seconds. The ten skips are the existing unavailable Node.js/read-only
Source-input cases; Phase 6D added no skip. The increase from the 2031 baseline
is exactly the 22 vector tests.

```text
P0: NONE
P1: NONE

VECTOR_ENV_MISSING: CLOSED / SYNCHRONOUS_VECTOR_ENV_IMPLEMENTED
PPO_ROLLOUT_BATCH_SCHEMA_MISSING: CLOSED
SELF_PLAY_CONTRACT_MISSING: P2 / unchanged
CTDE_CONTRACT_MISSING: P2 / unchanged
EIGHT_FACTOR_AUXILIARY_TARGET_NOT_IMPLEMENTED: P2 / ready but deliberately deferred
PARALLEL_ENV_EXECUTION_MISSING: P2 / Engine stepping remains serial
VECTOR_TRAIN_EXACT_RESUME_MISSING: P2 / evaluation/new-run load only

PARALLEL_ENV_EXECUTION_READINESS: READY
MULTISEAT_SELFPLAY_CONTRACT_AUDIT_READINESS: READY
EIGHT_FACTOR_AUXILIARY_IMPLEMENTATION_READINESS: READY
TRAINING_BENCHMARK_EXPERIMENT_READINESS: READY

NATIVE_SYNCHRONOUS_VECTOR_PPO_V1_IMPLEMENTED
VECTOR_ENV_ISOLATION_CONTRACT_CLOSED
BATCHED_MASKED_POLICY_INFERENCE_VERIFIED
PER_ENV_GAE_CONTRACT_CLOSED
VECTOR_PPO_DETERMINISM_VERIFIED
NATIVE_PPO_DATA_COLLECTION_SCALING_READY
TRAINING_BENCHMARK_EXPERIMENT_READY
```

## Phase 6E Parallel Environment Execution Verification

Phase 6E implements `SYNCHRONOUS_MULTIPROCESS_ENVIRONMENT_EXECUTION` with an
explicit Python `spawn` context. It changes environment collection organization,
not PPO mathematics or any Engine, Decision, ActionCatalog, reward, observation or
replay authority. The learner/coordinator remains the sole owner of the shared
ActorCritic, optimizer, PPO update state and learner RNG streams. Each persistent
worker owns one env/session/Engine/World, opponent, Engine RNG, episode lifecycle,
Decision/catalog binding, reward sequence and replay recorder. Worker and env
indices are identical in v1; PIDs are operational metadata only.

### Parallel and protocol authorities

The production parallel config is
`native-ppo-parallel-runner-config/1.0`, id
`native-parallel-ppo-runner`, version `1.0.0`, hash
`f2539a9ab2d284fe8228d87b8412a6857e81468bc182fe542754c3152a3be3ce`.
It fixes four envs/workers, `SPAWN`, `SYNCHRONOUS_MULTIPROCESS`,
`TOTAL_TRANSITIONS_PER_UPDATE`, stable env-index aggregation,
`FAIL_WHOLE_ROLLOUT`, and no automatic restart. The 30-second response timeout is
operational safety provenance and is intentionally excluded from the learning
semantic hash; changing it leaves the config hash unchanged.

The IPC authority is `native-ppo-parallel-worker-protocol/1.0`, id
`native-ppo-parallel-worker-protocol`, version `1.0.0`, hash
`fa0094258b5b41b1df0001071f073d36ae66a697d3e84d7a884f8b56c0463c32`.
One dedicated duplex Pipe is used per worker. Typed messages are
`WorkerInitRequest`, `WorkerReady`, `WorkerDecisionEnvelope`,
`CoordinatorActionCommand`, `WorkerTransitionResult`, `WorkerResetRequest`,
`WorkerResetResult`, `WorkerShutdown`, `WorkerShutdownAck`, and `WorkerError`.
Every request is tied to protocol/run/worker/env/request/episode coordinates.
Results may arrive in any OS completion order but are validated and aggregated in
stable env-index order.

The live binding token hashes env, episode, Decision window/frame/sim-time,
Decision hash, catalog hash and action-encoder hash. The coordinator sends only
that token plus an action index. The worker resolves the index against the exact
observation-time frozen catalog and commits through `NativeTrainingEnv`'s normal
Gateway/Engine path. Tokens are one-shot. Wrong worker/env, request, episode,
token, masked/out-of-range index, stale command and duplicate command all fail
closed. No World, Engine, ControllerView, live catalog, GREEN or FrameAnalysis is
an actor IPC field.

### Worker authority and ownership handshake

Before the first action, each worker constructs its environment inside the child,
validates trained side, the frozen no-op opponent, action encoder, shared discount,
reward mode, and—when composite shaping is enabled—Analyzer and shaping hashes.
`WorkerReady` returns runtime Engine/ruleset/Perspective/Decision authority,
reward authorities, seed hash, preprocessor hash, action encoder hash, scenario
identity, PID/session identity and the first detached Decision. The coordinator
requires all workers to report identical semantic authorities and distinct PIDs
and session identities before collection starts.

Preprocessing runs inside each worker with the existing
`native-ppo-observation-preprocessor/1.0` authority, hash
`8f0520cd00d03d4346146f22b4cc8a35290d5868b1281419e4a5baa500ef1554`.
The transported learner observation is finite float32 `[8302]`; its mask is bool
`[256]`. The coordinator sorts active envs, stacks `[B,8302]` and `[B,256]`, and
executes one shared batched ActorCritic forward. Sampling is then per-row using
the env's independent `POLICY_SAMPLING` generator in stable env-index order.
Worker completion timing therefore cannot alter sampling.

The worker module does not construct or receive a policy, PPO core or optimizer.
Under pytest/CLI spawn, Python may re-import a parent entry module which has
already imported the optional torch package, so `WORKER_TORCH_IMPORT` may report
`YES`; this is runtime module visibility, not policy ownership. A fresh Engine or
training-authority import still does not require or import torch.

### Barrier, reset, bootstrap and GAE

Each synchronous round first freezes all active worker Decisions, performs one
batched inference, dispatches commands, then waits at a barrier for every active
worker. Engine commits execute concurrently in independent processes. The
coordinator never updates the policy before the complete total-transition rollout
has been collected, so every transition in a rollout has one policy version.
Partial final rounds select the lowest active env indices and never over-step or
drop a committed transition.

Normal results carry the next detached Decision. Terminated transitions use a
zero next value. Truncated transitions carry the committed post-truncation
observation marked separately; the coordinator evaluates that value before an
explicit reset handshake. Reset observations cannot leak backward into truncated
bootstrap. A worker returns the complete terminal/truncated record and episode
verification first; it resets only after `WorkerResetRequest`. Environment seeds
continue from explicit per-env episode indices.

The coordinator keeps env-index trajectory segments, applies the existing GAE
authority per segment, breaks traces at terminal/truncation and rollout boundaries,
then flattens in the existing stable env-major order. There is no cross-env or
cross-episode recurrence and no parallel-specific PPO update. The existing single
model/optimizer consumes the ordinary `PPORolloutBatch`.

### Reward, replay, failure and checkpoint boundaries

Base-only and composite modes were both exercised. Workers return the scalar
reward and Base/EightFactor component amounts from the formal RewardRecord; the
coordinator never recomputes reward. Composite GREEN analysis remains worker-local
privileged supervision. The same frame analyzed in spawned workers retains model
hash `77e0113c896c813033528d43bc6c132e296cb91eacf9a153d436cb9fe20bc577`.
Completed smoke episodes pass reward re-derivation, Engine strict replay and
action-ID/reward-record joins.

Any worker exception, EOF, protocol error or timeout fails the whole synchronous
rollout. There is no in-rollout automatic restart. Normal, exception and timeout
paths request bounded shutdown, join every child, then terminate any remaining
child; tests observe zero orphan/zombie Native PPO processes.

Learner checkpoints still contain the model, optimizer, learner RNG and semantic
config/counter state, never live worker Engines. They are written only at a
quiescent rollout/update boundary. Existing compatible checkpoints can initialize
a new parallel run or deterministic evaluation. Exact continuation of live worker
episodes is deliberately not claimed.

### PE1-PE48

PE1 passes spawn startup/readiness. PE2 passes all-worker authority equality. PE3
rejects a mismatched worker authority. PE4-PE7 pass distinct process/session/World,
opponent and Engine-RNG ownership. PE8 passes worker vs single/vector preprocessor
equality. PE9 passes `[B,8302]`/`[B,256]` batched inference. PE10-PE11 pass per-env
sampling isolation and completion-order independence. PE12-PE16 pass exact token,
wrong/duplicate/stale rejection and worker-catalog action-ID resolution. PE17
retains the fixed opponent same-pre-state path. PE18 observes actual worker
critical-path overlap.

PE19-PE23 pass normal, terminal, truncated committed-post-state bootstrap,
reset-value isolation and per-env reset. PE24-PE25 pass partial-round no-drop and
exact total transition target. PE26-PE27 pass per-env GAE and stable env-major
flattening. PE28-PE30 pass existing PPO update reuse, one policy/optimizer, and one
policy version per rollout. PE31-PE34 pass Base/Composite and RED/BLUE paths.
PE35-PE36 pass parallel1/vector1 and parallel2/vector2 semantic differentials.
PE37 passes artificial-delay scheduling determinism. PE38 passes parallel/sequential
evaluation equality. PE39-PE41 pass Engine replay, reward replay and action join.
PE42-PE45 pass worker exception/timeout fail-whole-run and zero child leakage.
PE46 passes stable config/protocol hashes. PE47 passes spawn-safe top-level factory
construction. PE48 verifies detached IPC schemas contain no actor World/GREEN/live
authority fields.

### PD1-PD10

PD1-PD5 pass repeated-process action sequence, reward sequence, flattened rollout,
PPO metrics and final parameter content hash equality. PD6 passes artificial
worker-delay noninterference. PD7 passes distinct env-index seeds. PD8 passes
deterministic parallel evaluation. PD9 passes parallel/synchronous differential.
PD10 confirms PID changes do not enter semantic hashes.

### Operational performance and payload probe

The table combines the Phase 6D single/synchronous smoke with the Phase 6E
parallel smoke. These figures are operational observations on one run, not a
benchmark, scaling guarantee, convergence claim or hyperparameter result.

| mode | envs | transitions | rollout s | Engine critical s | preprocess sum s | policy s | IPC s | reward/replay sum s | update s | transitions/s |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| existing single | 1 | 16 | 2.4961 | n/a | n/a | n/a | n/a | n/a | 0.0488 | 6.41 |
| synchronous vector | 1 | 16 | 2.4798 | 1.1266 | 0.8313 | 0.0203 | n/a | 0.0107 | 0.0331 | 6.45 |
| synchronous vector | 2 | 32 | 5.4476 | 2.4970 | 1.7635 | 0.1882 | n/a | 0.0232 | 0.0991 | 5.87 |
| synchronous vector | 4 | 64 | 10.1689 | 4.7014 | 3.2407 | 0.2768 | n/a | 0.0452 | 0.1528 | 6.29 |
| spawn parallel | 1 | 16 | 2.1489 | 1.1476 | 0.4745 | 0.0097 | 0.0137 | 0.0100 | 0.0633 | 7.45 |
| spawn parallel | 2 | 32 | 2.5278 | 1.3037 | 1.0425 | 0.1112 | 0.0166 | 0.0214 | 0.1059 | 12.66 |
| spawn parallel | 4 | 64 | 3.1084 | 1.6184 | 2.2982 | 0.0364 | 0.0218 | 0.0489 | 0.1730 | 20.59 |

For parallel4, worker Engine CPU time summed to 6.0140 seconds while the observed
Engine critical path was 1.6184 seconds; all 16 vector rounds overlapped. This is
direct evidence of real process concurrency. Engine CPU remains the largest
instrumented critical-path component; worker preprocessing is second, while
policy and measured IPC are small. Compared with the supplied 6.41 transitions/s
single smoke, the 20.59 transitions/s result is about 3.21x operational throughput,
but it remains `SMOKE_OPERATIONAL_ONLY`.

At protocol-5 pickle sizing, observation transport was about 74,742 bytes per
transition, masks about 271 bytes, commands about 299 bytes and transition results
about 76,124 bytes. Shared memory is not needed for the current benchmark smoke;
`SHARED_MEMORY_OBSERVATION_TRANSPORT` remains a future optimization if larger
workloads show IPC becoming material.

### Phase 6E closure and readiness

Phase 6E targeted tests: `22 passed, 0 failed`. The related PPO/vector/single
runner, prerequisite, TrainingEnv/MultiSeat, Decision, reward, EightFactor,
Gateway and replay set: `272 passed, 0 failed`. The final full-suite result is
`2075 passed, 10 skipped, 0 failed` in 472.18 seconds. All ten skips are the
existing unavailable Node.js/read-only Source-input cases; Phase 6E added no
skip. The increase from the 2053 baseline is exactly the 22 parallel tests.

```text
P0: NONE
P1: NONE

PARALLEL_ENV_EXECUTION_MISSING: CLOSED
SYNCHRONOUS_MULTIPROCESS_ENVIRONMENT_EXECUTION: IMPLEMENTED
VECTOR_TRAIN_EXACT_RESUME_MISSING: P2 / unchanged
PARALLEL_TRAIN_EXACT_RESUME_MISSING: P2
WORKER_FAULT_TOLERANT_RESTART_MISSING: P2
SHARED_MEMORY_IPC_OPTIMIZATION_MISSING: P2 / performance-only
SELF_PLAY_CONTRACT_MISSING: P2 / unchanged
CTDE_CONTRACT_MISSING: P2 / unchanged
EIGHT_FACTOR_AUXILIARY_TARGET_NOT_IMPLEMENTED: P2 / ready but deferred

TRAINING_BENCHMARK_EXPERIMENT_READINESS: READY
SELF_PLAY_CONTRACT_AUDIT_READINESS: READY
EIGHT_FACTOR_AUXILIARY_IMPLEMENTATION_READINESS: READY
DISTRIBUTED_RAY_EXECUTION_READINESS: NOT_NEEDED_FOR_CURRENT_BENCHMARK

NATIVE_PARALLEL_PPO_ENV_EXECUTION_V1_IMPLEMENTED
MULTIPROCESS_ENV_ISOLATION_CONTRACT_CLOSED
PARALLEL_LIVE_ACTION_BINDING_VERIFIED
PARALLEL_ENGINE_EXECUTION_VERIFIED
PARALLEL_PPO_DETERMINISM_VERIFIED
NATIVE_PPO_ROLLOUT_THROUGHPUT_SCALING_READY
TRAINING_BENCHMARK_EXPERIMENT_READY
```

## Phase 7 Base PPO vs EightFactor-Shaped PPO Training Benchmark v1

Phase 7 freezes the research protocol as
`native-ppo-training-benchmark/1.0`, id
`native-base-vs-eight-factor-ppo-pilot`, version `1.0.0`, semantic hash
`e0248945ebed679da24b307f4389df22631b18be3e7d0b31e4bc5940479a404b`.
It compares only `BASE_ONLY` with `COMPOSITE_EIGHT_FACTOR`; PPO, network,
discount, seeds, trained side, fixed opponent, scenario, transition budget and
evaluation grid are paired controls. The run matrix is stable scenario-major,
seed-major and treatment-minor ordering. Run ids are deterministic hashes, not
wall-clock names.

### Scenario and supervision freeze

The pilot suite contains `demo-direct-fire-basic` (scenario hash
`bcc4d11469480f7590af898465790a258bdeef75b3c87b733e3511b43cd4cb9b`)
and `public-joint-fire-small` (scenario hash
`37fdccd6ad801c25c3d28648bc19402561be8d2b0dbcef20f109fda2899000f5`).
Both are small combat-stage MOVE/FIRE scenarios with bounded 20/10-tick
episodes. The opponent remains the deterministic
`native-no-op-fixed-opponent-v1`; this is a learner-pipeline/shaping-signal
baseline, not a strong-opponent benchmark.

The public scenario templates do not contain every field required by the
fail-closed EightFactor GREEN contract. Phase 7 does not treat missing fields as
zero and does not relax analyzer coverage. Instead the benchmark explicitly
versions an analysis-only template policy,
`explicit-complete-eight-factor-supervision/1.0.0`, hash
`bc5a7ab3964652b96456804485db64e0018a5274afcfbd78958b3e03045807fd`.
It supplies fixed `value`, mobility, visibility-list and status facts, forbids
future information, and is excluded from Decision-v1/policy tensors. This
synthetic privileged-supervision assumption is a mandatory limitation on any
research conclusion.

### Pilot protocol

The pilot uses two paired training seed coordinates, RED only, four spawn
workers per run, exactly 2,048 committed environment transitions (one production
PPO rollout/update), evaluation at transitions 0 and 2,048, and eight fixed
evaluation episodes per checkpoint. The evaluation seed-set hash is
`9bcbdbfab95281caaadb4160bfb24afed6ee149d3dfd6f1272203cb732d90aa6`.
All eight runs execute sequentially; each run retains internal four-worker
parallelism. Total planned training is 16,384 transitions. At the Phase 6E
20.59 transitions/s reference this is 795.7 seconds of estimated training time,
excluding evaluation, PPO updates and artifact/replay work. This is an estimate,
not a benchmark result.

Checkpoint selection is `LAST_CHECKPOINT_ONLY`. The primary metric is Engine
outcome win rate. AUC is normalized trapezoidal evaluation win-rate versus
committed transitions. Reward re-derivation remains every completed episode;
Engine strict replay verifies the first completed episode of every run. Raw
evaluation points, per-run summaries and paired differences are published as
JSON plus deterministic `learning_curve.csv`, `final_runs.csv` and
`paired_comparison.csv`.

### Preflight repairs and new baseline

Real transition-zero preflight found two Phase 6E evaluation defects before any
training result was accepted. First, a truncated committed post-state can
legitimately have an empty action catalog: its observation is required for value
bootstrap but it must never be sampled. The IPC protocol now permits an all-false
mask only when `post_truncation_observation=true`; an active envelope remains
fail-closed. Second, evaluation previously assigned replacement episodes in
worker completion order. Parallel evaluation now runs deterministic waves, so
each env consumes exactly the frozen episode-index grid and scheduling cannot
change the evaluation seed set. This changes the parallel protocol hash to
`fa0094258b5b41b1df0001071f073d36ae66a697d3e84d7a884f8b56c0463c32`
and the production parallel config hash to
`f2539a9ab2d284fe8228d87b8412a6857e81468bc182fe542754c3152a3be3ce`.
No pre-repair probe is mixed with the pilot.

### B1-B30 verification map

B1-B3 cover deterministic config/matrix and treatment-only diff. B4-B6 cover
stream-indexed paired initialization, common evaluation seeds and real
transition-zero equality. B7-B8 cover exact transition budget/cadence. B9-B10
cover zero Base shaping and active Composite shaping. B11-B13 cover identical
PPO/scenario/opponent controls. B14-B18 cover zero invalid/stale, finite metrics,
LAST selection, Engine outcome authority and separate truncation. B19-B22 cover
atomic completion, corrupt-run rejection, completed-run skip and explicit
failure. B23-B27 cover raw points, equal-weight scenario aggregation, paired
deltas, known-vector AUC and stable CSV ordering. B28 separates operational
metrics, B29 preserves Engine replay authority and B30 rejects PILOT/FULL
identity mixing.

The dry-run command is:

```text
python -m scripts.run_ppo_training_benchmark plan
```

Generated research artifacts are written under the caller-selected ignored
`local_data/` directory or `/tmp`; they are not source/replay authority and are
not committed.

### Pilot execution result

The repaired-baseline pilot completed all eight planned runs with zero failed
runs, invalid actions, stale actions, non-finite metrics or replay mismatches.
Each run consumed exactly 2,048 committed transitions and performed one PPO
update. All paired initial parameter hashes, deterministic transition-zero
action sequences, outcomes and Base returns matched. Eight atomic completion
markers and final learner checkpoints passed content/RNG/identity restore
validation. The implementation verdict is `PASS`; the research result is
`MIXED` and remains `PILOT_ONLY_DESCRIPTIVE`.

| scenario | seed | Base final win rate | Shaping final win rate | paired delta | Base AUC | Shaping AUC | AUC delta |
|---|---:|---:|---:|---:|---:|---:|---:|
| demo-direct-fire-basic | 0 | 1.00 | 1.00 | 0.00 | 1.00 | 1.00 | 0.00 |
| demo-direct-fire-basic | 1 | 0.00 | 0.00 | 0.00 | 0.00 | 0.00 | 0.00 |
| public-joint-fire-small | 0 | 1.00 | 1.00 | 0.00 | 1.00 | 1.00 | 0.00 |
| public-joint-fire-small | 1 | 0.00 | 1.00 | +1.00 | 0.00 | 0.50 | +0.50 |

Scenario-level means are 0.50 vs 0.50 final win rate/AUC on direct-fire and
0.50 vs 1.00 final win rate, 0.50 vs 0.75 AUC on joint-fire. The equal-weight
macro means are Base 0.50 final win rate and 0.50 AUC versus Shaping 0.75 and
0.625. Three of four paired replicates are ties; one joint-fire replicate favors
shaping. This is not enough evidence for a statistical claim, a cross-scenario
gain, or lower variance in the general case.

The final evaluation shaping return was negative for both direct-fire shaping
runs (`-0.01234` and `-0.00368`) while their win rates matched Base. The one
paired outcome improvement had final evaluation shaping return zero. Training
shaping signals were non-zero in every Composite run, but this two-point pilot
does not establish correlation between shaping return and outcome. No reward
hacking was observed; it also cannot be excluded from two seeds/one update.

Operationally, mean Base training time was 118.79 seconds at 17.30
transitions/s. Mean Composite time was 139.84 seconds at 14.66 transitions/s,
about 17.7% greater wall time and 15.3% lower throughput in this pilot. These
figures include normal reward/analyzer and per-episode artifact/replay work and
remain operational measurements, not a standalone performance benchmark.

The plotting-ready artifact root used for this execution was
`/tmp/native_ppo_phase7_pilot_v1`. It contains `benchmark_protocol.json`,
`benchmark_result.json`, per-run manifests/JSONL/checkpoints/evaluation reports,
and deterministic `learning_curve.csv`, `final_runs.csv`, and
`paired_comparison.csv`. The CSV SHA-256 values are respectively
`91f4782a900421889f6e42904e45f8003b9ba39493be26d05d3908b00d75cffd`,
`3cb4da97d22c4635a5524f35dd054747c6bfa38f2a9c6a999ad71de924f9d97d`,
and `16d413ec60f4f8228c91a00d60271f8ced2c0ae10b1ca5d7b2c4bdaac085f361`.

The recommended FULL protocol is five or more paired seeds, at least 50,000
transitions per run as an initial freeze candidate, evaluation every 10,000
transitions including transition zero, and at least 30 fixed evaluation episodes
per checkpoint. A pre-FULL timing dry run should confirm total cost. The same two
scenarios may be retained for continuity, but the synthetic analysis-only GREEN
template and NoOp-opponent limitations must remain prominent. FULL is not
started by Phase 7 v1.

Phase 7 added 14 tests over the 2,075-test Phase 6E baseline and changed no
existing skip. Targeted benchmark/parallel verification is `36 passed`; the
related PPO, runner, reward, EightFactor, TrainingEnv, MultiSeat and replay set
is `236 passed`; the repaired full-suite baseline is `2089 passed, 10 skipped,
0 failed` in 476.90 seconds.

```text
PPO_TRAINING_BENCHMARK_V1_IMPLEMENTED
BASE_VS_EIGHT_FACTOR_SHAPING_EXPERIMENT_VERIFIED
PAIRED_SEED_EXPERIMENT_CONTRACT_CLOSED
LEARNING_CURVE_PUBLICATION_READY
FULL_PPO_TRAINING_EXPERIMENT_READY

PILOT_RESEARCH_RESULT: MIXED
FULL_PPO_BASE_VS_SHAPING_EXPERIMENT_READINESS: READY
SELF_PLAY_RESEARCH_READINESS: READY_AFTER_CONTRACT_AUDIT
AUXILIARY_EIGHT_FACTOR_RESEARCH_READINESS: READY
```

## Phase 7B Full Base PPO vs EightFactor-Shaped PPO Experiment v1

Phase 7B freezes the confirmatory fixed-opponent protocol as
`native-ppo-training-benchmark/1.0`, id
`native-base-vs-eight-factor-ppo-full`, version `1.0.1`, semantic hash
`3ac63af1c6774146129f05d77474cc8fcc68ab9782fbce2270629c8f00a07f72`.
The only treatment difference is reward mode and its reward/shaping identity.
Engine, rules, perspective, Decision-v1, observation encoder, learner
preprocessor, action encoder, discount, PPO, network initialization, opponent,
trained RED side, scenarios, seed coordinates, four-worker spawn runner,
transition budget and evaluation protocol remain paired controls.

The matrix has two frozen scenarios, five paired training seed indices and two
treatments: 20 sequential runs and exactly 1,000,000 committed training
transitions. Each run targets exactly 50,000 transitions. Because 50,000 is not
divisible by the production 2,048-transition rollout, each 10,000-transition
evaluation interval is collected as `2048 + 2048 + 2048 + 2048 + 1808`.
Consequently every run performs 25 PPO updates, and neither 49,152 nor 51,200 is
reported as 50,000. Evaluation is frozen at transitions 0, 10,000, 20,000,
30,000, 40,000 and 50,000 with 30 distinct EVALUATION-namespace episodes per
checkpoint. The evaluation seed-set hash is
`14b751179fec3b54d8e37acb342e2788ea988c68601529b1925fcab002fae67e`.
The primary checkpoint rule remains `LAST_CHECKPOINT_ONLY`.

The FULL analysis publishes raw 120 checkpoint rows, per-seed final win rate,
normalized trapezoidal win-rate AUC, T50/T75, final reward components, episode
length and operational cost. Scenario statistics report mean, sample standard
deviation, median, min, max and raw seed values. Paired final/AUC deltas include
a deterministic 10,000-resample percentile bootstrap interval using analysis
seed 20260901. Scenario macro aggregation is equal-weight. Primary learning
curves are unsmoothed; the variability band contract is mean plus/minus sample
standard deviation. EightFactor return correlations are explanatory only and
cannot replace Engine outcome.

Artifact retention is formally frozen as
`FIRST_ENGINE_REPLAY_VERIFIED_ONLY`: reward re-derivation still executes for
every completed episode and Engine strict replay still verifies the first
completed episode per run, while non-primary episode packages are removed only
after verification. The final learner checkpoint, all raw evaluation rows,
update/episode aggregates, retained worker session facts, the retained replay
evidence and completion hashes remain. Pilot byte scaling predicts about 8.1 GB;
the launch gate uses a conservative 10 GB estimate (roughly 24% margin) without
weakening the runtime correctness checks. Source provenance records Git HEAD, dirty status,
the binary tracked diff hash and SHA-256 identities for relevant source/config
files; no commit or stash is required.

The Pilot-derived dry-run gate estimates 28,900.1 seconds of Base training,
34,109.9 seconds of Shaping training, 1,800 seconds of evaluation and a 10%
orchestration margin: 71,291.0 seconds (19.80 hours) total. Conservative
estimated disk is 10.0 GB. Both are within the frozen 24-hour/20-GB launch thresholds. This is a
cost estimate, not a benchmark result. The existing NoOp opponent and synthetic
complete GREEN supervision template remain important external-validity
limitations: this is a fixed-opponent shaping study, not a competitive
self-play-strength claim.

F1-F5 cover identity, 20-run pairing and treatment-only difference. F6-F8 cover
paired initialization, transition-zero equality and the distinct 30-episode
evaluation set. F9-F13 cover exact partial rollout boundaries, the six-point
grid, LAST selection and raw-row retention. F14-F18 cover AUC, T50/T75,
equal-weight macro aggregation and deterministic bootstrap intervals. F19-F24
cover incomplete pairs, atomic completion, strict completed-run resume and dirty
source provenance. F25-F29 cover Base/Composite reward isolation and fail-closed
invalid, stale, non-finite and replay-mismatch results. F30 covers deterministic
FULL artifact ordering.

The first 1.0.0 launch was fail-closed during its first run after 44,096
transitions and produced no COMPLETE run. A rollout whose final synchronous
round completed every active episode left all workers inactive at the next
rollout boundary; the next collector correctly refused to invent a decision but
reported `no active worker decision`. The lifecycle repair resets such inactive
workers only at the start of the next rollout, after the prior terminal or
truncated transitions and bootstrap facts are already sealed. A real
two-worker/max-step-one cross-rollout test covers this exact boundary. Version
1.0.1 adds `AUTO_RESET_INACTIVE_AT_ROLLOUT_START_V1` to the FULL semantic
identity. The failed 1.0.0 artifacts remain isolated and are never aggregated
with 1.0.1.
