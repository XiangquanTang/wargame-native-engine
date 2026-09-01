# Training Reward Channel Contract Audit v1

## 1. Executive Summary

This is a read-only contract audit. It does not implement a reward calculator,
change an Engine rule, change a training environment, add Eight Factor shaping,
or modify replay authority. The only repository change made by this audit is
this document.

The current reward semantics are small and internally consistent:

```text
committed Native transition
  -> Engine terminal/truncation/winner authority
  -> training-wrapper sparse terminal projection
       nonterminal                         0.0
       Native/training truncation          0.0
       terminal draw                       0.0
       terminal winner                     +1.0
       terminal loser                      -1.0
  -> training result / DecisionTransition
```

The scalar is produced by the training wrapper, not by `NativeEngine`. Engine
and scenario terminal rules remain authoritative for whether the committed
post-state is terminated or natively truncated and, when terminated, which
side won. Training-only step/window limits are a second explicit truncation
source and also produce zero reward.

The RED and BLUE MultiSeat rewards are computed once from the same committed
post-state and are zero-sum in every current case. Invalid, stale, preview-
rejected, and execution-failed requests do not create a transition or a reward
record; they are exceptions, not a `-1` penalty. WAIT has no direct cost. The
current reward is therefore dimensionless, deterministic, unclipped, and
sparse terminal supervision in the exact range `[-1.0, 1.0]`.

The policy-input/supervision boundary is intact. Reward, winner, outcome, GREEN,
Eight Factor analysis, return, advantage, and value targets are absent from the
Decision-v1 policy record. Current reward code does not read a GREEN frame. It
does consume the Engine's referee-level terminal winner, so it is privileged
terminal supervision even though there is no direct GREEN-object dependency.

There is no P0 or P1 contract blocker. The missing pieces are P2 contract and
verification infrastructure: no standalone reward schema, canonical policy
identity/hash, component decomposition, or strict-replay reward recomputation;
training-level truncation configuration is not part of a reward-policy record.
The existing formula is nevertheless clear enough to freeze and implement as
`native-training-reward/1.0`.

```text
REWARD_SCHEMA_VERSION_READINESS: READY
REWARD_POLICY_HASH_READINESS: PARTIAL
REWARD_REPLAY_READINESS: READY_AFTER_FIX
MULTISEAT_REWARD_READINESS: READY
EIGHT_FACTOR_REWARD_INTEGRATION_READINESS: READY_AFTER_REWARD_CONTRACT
REWARD_CHANNEL_IMPLEMENTATION_READINESS: READY
```

## 2. Scope / Non-goals

This audit freezes:

- current scalar reward authority and formula;
- transition, side, decision-window, frame, and simulation-time alignment;
- terminal, draw, native truncation, and training truncation behavior;
- invalid/stale action and WAIT behavior;
- the MultiSeat side relation and recommended joint record;
- privileged-supervision and policy-input isolation;
- version, policy hash, record hash, replay, and snapshot requirements;
- the boundary for future Eight Factor shaping, labels, and evaluation;
- the required implementation tests and phased implementation plan.

It does not implement or change:

- a reward calculator, reward record, policy file, or policy hash;
- `NativeTrainingEnv`, `NativeMultiSeatEnv`, or `DecisionTransition`;
- Engine terminal rules, objective scoring, action validation, or World fields;
- Decision, Perspective, capability, catalog, mask, or encoder contracts;
- learner-side return, normalization, clipping, bootstrapping, PPO, or MARL;
- Eight Factor reward/shaping, a central critic, offline dataset writer, or LLM;
- Human, Agent, ControllerView, Viewer, Source, or authoritative Replay APIs.

All behavior described here is Native simplified-rules behavior. It is not an
official or SDK-equivalent reward definition.

## 3. Current Reward Data Flow

### NativeTrainingEnv

```text
Decision-v1 observation/catalog at window N
  -> controlled catalog index resolves to current action_id
  -> opponent receives its detached side observation/actions
  -> RED and BLUE batches enter one Gateway decision window
  -> one Engine advance commits the window
  -> ControllerView for the committed post-state
  -> training termination/truncation resolution
  -> _reward(post_view)
  -> next Decision-v1 observation + reward + flags + info
```

`NativeTrainingEnv._reward` returns zero unless `view.terminated` is true and
`view.winner` is non-null. It then returns `+1.0` for the controlled winner and
`-1.0` for the controlled loser. `max_steps` is evaluated after the committed
step; it marks the returned step truncated but does not alter the scalar from
zero.

### NativeDecisionSession / NativeMultiSeatEnv

```text
same RED/BLUE pre-state snapshot at decision window N
  -> preview both side decisions
  -> submit RED without advance
  -> submit BLUE and commit one joint Engine window
  -> construct both post-state ControllerViews
  -> verify shared frame/time/window identity
  -> _rewards(terminated, truncated, winner)
  -> two side DecisionTransition records with one shared transition interval
```

`NativeMultiSeatEnv.max_decision_windows` is applied outside the native
decision session. When the limit is reached without an Engine terminal/native
truncation, both side rewards are replaced with `0.0`, both final transition
records are marked truncated, and the source is recorded in `info` as
`training_max_decision_windows`.

`SingleSeatTrainingAdapter` does not calculate another reward. It selects the
controlled side's scalar and transition from the MultiSeat result.

## 4. Reward Authority

The current arrangement is mixed only at the input-authority boundary: Engine
and scenario rules determine the terminal facts, while the training wrapper
owns the sparse numeric projection.

| Reward component/fact | Producer | Input | Direct GREEN use | Step/terminal | RED/BLUE relation | Verdict |
|---|---|---|---|---|---|---|
| Nonterminal base scalar | training wrapper (`_reward` / `_rewards`) | committed post-state `terminated=False` | No | step | `0.0 / 0.0` | CLEAR |
| Terminal winner fact | `evaluate_terminal` -> `NativeEngine.result` -> ControllerView | authoritative committed World plus scenario terminal rules | No GREEN frame; authoritative World is used by Engine | terminal | one winner or draw | ENGINE_AUTHORITY |
| Terminal win/loss scalar | training wrapper | Engine `terminated` and `winner` | No | terminal | `+1.0 / -1.0` | CLEAR, ZERO_SUM |
| Terminal draw scalar | training wrapper | Engine `terminated=True`, `winner=None` | No | terminal | `0.0 / 0.0` | CLEAR, ZERO_SUM |
| Native max-ticks truncation | Engine terminal evaluator; wrapper scalar | `max_ticks`, committed tick, `truncated=True` | No | truncation | `0.0 / 0.0` | CLEAR |
| Training max-step/window truncation | training wrapper | explicit wrapper limit and decision counter | No | truncation | `0.0 / 0.0` | CLEAR; provenance not versioned |
| Objective/elimination result | Engine terminal evaluator | objective score or living sides | No | terminal fact | winner/draw feeds the same terminal scalar | NOT A DENSE COMPONENT |
| Invalid/stale/rejected action | encoder/Gateway/preview/Engine validation | submitted identity/parameters/window | No | no committed transition | no reward record | EXCEPTION, NOT PENALTY |
| WAIT/action cost | none | none | No | step | no direct component | ABSENT |
| Eight Factor/advantage | `EightFactorAnalyzer` and replay analysis pipeline | detached GREEN frame | Yes | evaluation frame | symmetric analysis, not reward | OUTSIDE REWARD |

The reward authority must therefore be named precisely:

```text
terminal/outcome authority = NativeEngine + scenario TerminalRules
training-only truncation authority = configured training wrapper
numeric reward policy authority = training reward policy/wrapper
```

The wrapper must never infer winner from health, unit count, objective display,
score text, or SideObservation when Engine supplies a terminal decision.

## 5. Transition Identity

The current `DecisionTransition` already records a useful transition identity:

- `episode_id` and `run_namespace`;
- `decision_index` and side;
- before observation/capability and submitted decision;
- next observation/capability;
- closed fact-frame range;
- simulation-time start/end in milliseconds;
- scenario identity/hash, map identity, and seed;
- reward plus terminated/truncated flags.

The required reward-time contract is:

```text
Decision_t + Catalog_t + joint ActionBatch_t
  -> exactly one committed Engine transition
  -> authoritative post-state
  -> Reward_t and Decision_t+1 derived from that same post-state
```

For a successful step, `Reward_t` is not associated with the pre-state alone,
an intermediate RED submission, a future call, or the next action window. It is
computed after the Engine commit and returned alongside the next observation.
Pure construction order between reward and encoding the already-frozen next
view is not semantically significant; their frame/time/terminal identity must
match.

The v1 reward record must add explicit reward-policy identity and the exact
before/after Decision references. It must reject side, window, frame, time, or
catalog mismatches instead of joining by list position or "latest" state.

## 6. Single-seat Reward

Two single-seat surfaces exist:

1. `NativeTrainingEnv` applies the sparse formula directly for its controlled
   side after the joint Gateway window commits.
2. `SingleSeatTrainingAdapter` projects the controlled side from the already
   computed MultiSeat reward pair.

Both currently produce the same semantics:

| Transition outcome for controlled side | Reward |
|---|---:|
| nonterminal | `0.0` |
| native truncation | `0.0` |
| training truncation | `0.0` |
| terminal draw | `0.0` |
| terminal win | `+1.0` |
| terminal loss | `-1.0` |

Phase 1 should remove the duplicated formula by making both surfaces project a
shared reward record. This is maintainability work, not an existing semantic
disagreement.

## 7. MultiSeat Reward

The current MultiSeat path is aligned correctly:

- RED and BLUE pre-state views are validated to share window, frame, simulation
  time, terminal flags, and immutable snapshot hashes.
- Both previews complete before execution begins.
- RED submission is required not to advance; BLUE submission completes the
  same Gateway window and must advance it.
- Both next observations are built from the committed post-state.
- `_rewards` is invoked once, using that post-state's termination and winner.
- Two side transitions use the same decision index, frame range, and simulation
  time range.

Current relation:

```text
r_red + r_blue == 0.0
```

for nonterminal, truncated, draw, RED-win, and BLUE-win cases. This is a
`ZERO_SUM` base reward contract, not merely side-specific rewards that happen
to match in one scenario.

The recommended canonical MultiSeat form is one immutable
`JointTrainingRewardRecord` containing one transition identity and the two
side rewards. A side environment receives an immutable side projection. This
makes same-transition and zero-sum validation structural and avoids two
independently constructed records drifting.

## 8. Terminal / Truncation

Engine terminal evaluation order is frozen by `TerminalRules` as elimination,
objective, then max ticks.

Current Engine reason codes are:

- `side_eliminated`;
- `mutual_elimination`;
- `objective_score`;
- `objective_score_draw`;
- `max_ticks` for native truncation.

`GameResult.outcome` is `win`, `draw`, `truncated`, or `ongoing`. Winner and
reason are Engine authority. The training wrapper consumes the winner; it does
not parse the reason text or duplicate terminal rules.

The current reward distinction is:

- `terminated=True, truncated=False, winner=side`: terminal +/-1;
- `terminated=True, truncated=False, winner=None`: draw 0;
- `terminated=False, truncated=True`: 0;
- Engine terminal takes precedence over a coincident training step/window
  limit, so a real terminal reward is not replaced by truncation zero.

Learner bootstrapping on truncation is algorithm policy and must remain outside
the reward record. The record supplies exact flags and source; it does not tell
PPO how to bootstrap.

## 9. Invalid Action Behavior

Normal Decision-v1 actions resolve through the frozen ActionCatalog, so invalid
action penalties are not part of the intended learning mechanism.

Current failure semantics are:

- invalid, padded, or out-of-range catalog index: rejected before opponent or
  Engine advance;
- stale Decision/Catalog binding: training snapshot error before submission;
- stale Gateway window/action identity: Gateway rejection;
- preview rejection: no Engine advance and no transition; retry may remain
  possible where the documented preflight contract allows it;
- partial execution failure: environment is failed/reset-required, no committed
  transition/reward record is published.

The frozen reward contract is `NO_COMMITTED_TRANSITION -> NO_REWARD_RECORD`.
It is not `reward=0`, and it is not an implicit negative penalty. A future
invalid-action curriculum must be a separately versioned wrapper policy and
must not weaken final Gateway/Engine rejection.

## 10. Sparse / Dense Components

Current component classification:

| Class | Current component |
|---|---|
| TERMINAL | win `+1`, loss `-1`, draw `0` |
| OBJECTIVE | none; objective score affects Engine terminal authority only |
| DAMAGE | none |
| SURVIVAL | none |
| TIME | none |
| ACTION_COST | none |
| SHAPING | none |
| OTHER | training/native truncation returns zero |

This is a sparse terminal reward. Sparse credit assignment and possible
stalling/long-horizon learning are risks, not correctness bugs. There is no
damage farming, repeated-objective reward, suicide penalty, movement-loop
reward, or direct WAIT cost in the current formula.

WAIT is an ordinary authorized action. A nonterminal WAIT transition returns
zero. If the committed transition containing WAIT also reaches an Engine
terminal, the normal terminal outcome reward applies; the action itself still
has no component.

A zero-action catalog does not cause a penalty or synthetic WAIT. Terminal
entry returned the reward on the transition that produced the terminal state;
calling `step` again is rejected by the environment lifecycle.

## 11. GREEN / Privileged Supervision

Current base reward code does not read:

- a GREEN frame;
- `WorldState` or `OperatorState` directly;
- exact enemy health, cooldown, suppression, lifecycle, or boarding;
- RNG, adjudication evidence, score display, or Eight Factor output.

It reads `terminated`, `truncated`, and `winner` projected from the authoritative
Engine result into the committed ControllerView/training result. Winner is not
a Decision-v1 policy feature, so this is privileged **terminal supervision**.

```text
DIRECT_GREEN_FRAME_USED: NO
PRIVILEGED_SUPERVISION_USED: YES (terminal winner/outcome authority only)
```

Reward is allowed to use privileged supervision. The security boundary is that
privileged inputs and component details never mutate or enter Decision,
Perspective, encoders, ActionCatalog, capabilities, mask, SideObservation, or
World. A reward may legitimately change when a future privileged policy reads
hidden state; that does not violate observation noninterference. The required
test is that calculating it cannot alter the policy observation or mask.

For future centralized-training/decentralized-execution work, a privileged
critic input must be a separate versioned contract. It is neither a policy
observation nor a `TrainingRewardRecord` component container.

## 12. Policy Input Isolation

The Decision-v1 observation encoder explicitly excludes reward, winner,
outcome, EightFactor, GREEN, authority hashes, advantage, and other evaluation
labels. It consumes only the immutable side Decision semantic. The action mask
consumes only the side ActionCatalog.

Current separation is:

```text
POLICY_INPUT
  Decision-v1 numerical observation + current catalog mask

TRAINING_SUPERVISION
  scalar reward + terminated/truncated + optional training/debug info

EVALUATION SIDECAR
  GREEN Eight Factor analysis and replay-derived metrics
```

There is no episode return, reward accumulator, winner, or GREEN field in the
policy record. Episode return must remain a learner/dataset aggregation of the
reward sequence, never an observation feature.

Human, deployed Agent, Decision, ControllerView machine core, Viewer, and LLM
decision renderers must not receive `TrainingRewardRecord` on their normal
execution API. A benchmark/training harness may request it explicitly.

## 13. EightFactor Boundary

`EightFactorAnalyzer` is a stateless, read-only analyzer over detached GREEN
frames. It sorts units, evaluates eight demo-compatible factors, aggregates
both sides, and produces a symmetric GREEN relative-advantage analysis. Its own
module states that the advantage does not participate in hit adjudication,
attrition, termination, or win/loss decisions.

Current role: `EVALUATION METRIC` and replay/live analysis sidecar. Training
package publication verifies online/offline Eight Factor sidecars but does not
use them to calculate scalar reward.

Role assessment:

| Candidate role | Verdict | Reason |
|---|---|---|
| Primary reward | NOT_RECOMMENDED | derived demo-compatible analysis, can be UNKNOWN/partial, uses GREEN, and is not task/Engine outcome authority |
| Potential-based shaping | CANDIDATE_AFTER_SEPARATE_CONTRACT | requires an exact potential, side symmetry, discount convention, decision-frame sampling, UNKNOWN policy, and proof/statement of behavioral effects |
| Auxiliary training label | RECOMMENDED CANDIDATE | suitable for an explicit privileged critic/auxiliary channel without changing base reward |
| Evaluation metric | YES, CURRENT ROLE | deterministic replay-derived comparison with explicit provenance |

Winner correlation alone would not make Eight Factor a reward authority. A
future shaping policy must be opt-in, ablatable, independently versioned (for
example `native-eight-factor-reward-shaping/1.0`), and disabled by default in
the base-reward contract. With shaping disabled, base reward bytes and hashes
must remain unchanged.

Cost is approximately O(number of Operators x eight factors) per analyzed
frame, plus timeline construction. The in-memory analyzer does not need
network, LLM, RNG, or Engine mutation, but replay processing adds I/O. A future
per-decision shaping implementation must select one precise committed frame;
it must not rescan full history on every step.

## 14. Reward Versioning / Hash

Freeze the following non-implemented identities:

```text
reward record schema_version: native-training-reward/1.0
joint record schema_version:  native-training-joint-reward/1.0
reward_policy_id:             native-sparse-terminal-zero-sum
reward_policy_version:        1.0.0
```

The current repository does not yet have a canonical reward policy source or
hash. `REWARD_POLICY_HASH_READINESS` is therefore `PARTIAL`; a snapshot hash or
Decision hash must not be relabeled as a reward-authority hash.

The future canonical policy document must cover:

- dimensionless units and numeric precision;
- nonterminal, win, loss, draw, native truncation, and training truncation;
- zero-sum side relation;
- invalid/rejected action semantics (`NO_RECORD`);
- WAIT and zero-action semantics;
- base component definitions and exact weights;
- shaping disabled/enabled identity;
- whether any normalization/clipping belongs to reward authority;
- stable terminal/truncation source codes.

Its `reward_policy_hash` must be SHA-256 over canonical JSON UTF-8. Changing a
formula, magnitude, side relation, truncation behavior, invalid-action policy,
authority-level scale/clipping, or shaping component requires a policy version
bump. Learner-side return normalization does not change the raw reward policy.

## 15. Snapshot / Replay

### Snapshot / restore

Engine snapshots contain the authoritative World terminal fields and all state
needed for deterministic adjudication. Reward is derived and must not enter the
World snapshot. Restoring a snapshot taken before a transition, submitting the
same side-bound joint action batch with the same exact delta, and applying the
same reward/truncation policy can reproduce the current scalar.

Current status is `PARTIAL` only because no formal R2 reward-record/hash test
or reward policy identity exists. The formula itself has no hidden mutable
accumulator, RNG, wall clock, or I/O dependency.

### Strict replay

Strict Source replay reconstructs and verifies the authoritative World and its
terminal/winner result. Training transitions also record scalar rewards and
closed frame/time ranges. Same-seed repeated episodes are tested to produce
equal reward maps and equal transition records.

However, current publication verification validates that terminal/truncated
flags occur on the correct final transition but does not independently
recalculate each scalar from a versioned reward policy. Training max-step/
max-window truncation also requires the same wrapper limit/configuration, which
is not yet represented by reward-policy metadata in the authoritative replay.

Therefore:

```text
ENGINE_TERMINAL_SOURCE_REPLAYABLE: YES
CURRENT_REWARD_SEQUENCE_RECORDED: YES
CURRENT_REWARD_POLICY_REDERIVATION_VERIFIED: NO
REWARD_REPLAY_READINESS: READY_AFTER_FIX
```

The fix belongs in the derived training-supervision layer. It must not change
World hashes, replay actions, Source frames, RNG, or authoritative manifests.

## 16. Determinism

Current reward calculation is O(1) and deterministic:

- inputs are committed booleans and a stable side identity;
- values are exact constants `-1.0`, `0.0`, and `1.0`;
- no collection ordering affects the result;
- no RNG, wall clock, network, replay I/O, full-history scan, or analyzer call
  occurs in `_reward` or `_rewards`;
- MultiSeat computes one map and projects it to two same-transition records.

Same pre-state, same joint action batch, same exact delta time, same Engine
rules, and same reward policy must always produce the same reward record/hash.
If a future potential uses pre/post values, both values must be explicit
transition inputs; it cannot rely on whichever wrapper call happened last.

## 17. Proposed Non-Implemented Schema

The recommended immutable machine schema is structurally equivalent to:

```text
TrainingRewardRecord {
  schema_version: "native-training-reward/1.0"

  reward_policy {
    policy_id: "native-sparse-terminal-zero-sum"
    policy_version: "1.0.0"
    policy_hash: sha256
  }

  transition_identity {
    episode_id
    run_namespace
    side: RED | BLUE
    decision_window_index
    frame_before
    fact_frame_start
    fact_frame_end              # closed interval
    frame_after
    sim_time_before_ms
    sim_time_after_ms
    decision_before_hash
    action_catalog_hash
    submitted_action_ids: tuple # exact frozen order
    decision_after_hash
  }

  total_reward: finite float
  components: tuple[RewardComponent]
  terminated: bool
  truncated: bool
  terminal_reason_code: stable string | null
  truncation_source: NATIVE | TRAINING_LIMIT | null
  supervision_source: ENGINE_TERMINAL | TRAINING_WRAPPER
  privileged_supervision_used: bool
  semantic_hash: sha256
}

RewardComponent {
  component_id: stable string
  producer_namespace: stable string
  amount: finite float
  privileged_supervision_used: bool
}
```

For base v1, use one stable component
`base:TERMINAL_OUTCOME` whose amount is exactly the total scalar, including
zero. Components are explanatory machine evidence; their deterministic sum
must equal total reward, and changing their authority cannot silently change
the total.

Recommended MultiSeat master record:

```text
JointTrainingRewardRecord {
  schema_version: "native-training-joint-reward/1.0"
  shared_transition_identity   # no side field
  red: TrainingRewardRecord side projection
  blue: TrainingRewardRecord side projection
  relation: ZERO_SUM
  semantic_hash
}
```

The two side projections must share every non-side transition field. `reward`
must be finite; `terminated` and `truncated` cannot both be true. Missing or
mismatched identities fail closed. No UI explanation, Python repr, mutable
object, optimizer state, policy logits, World hash, GREEN payload, or raw
Eight Factor record belongs in this schema.

## 18. Semantic Hash

`HASH_RECOMMENDED`.

Use canonical JSON, UTF-8, and SHA-256. A side reward hash covers:

- reward schema and reward-policy identity;
- exact transition/side identity and stable action IDs;
- total and ordered components;
- terminated/truncated and stable reason/source codes;
- supervision source and privileged-supervision flag.

A joint hash covers the shared identity, relation, and both side record hashes
in fixed RED/BLUE order.

Exclude:

- the hash field itself;
- wall clock, Python object identity/repr, process hash, learner/optimizer state;
- policy logits, advantages, value targets, episode return;
- renderer/UI text and debug exceptions;
- full GREEN/World payloads and authoritative World hash;
- Decision hash inputs beyond the explicit immutable transition references.

Decision semantic hash and reward semantic hash remain different namespaces.
Reward may reference Decision identity; Decision must never include reward or
change its hash because reward policy changed.

## 19. Findings

### P0

NONE.

No reward write-back, Engine-adjudication mutation, GREEN policy observation,
or nondeterministic external reward dependency was found.

### P1

NONE.

No transition misalignment, side ambiguity, different RED/BLUE post-state,
winner duplication, terminated/truncated reward confusion, call-order-dependent
reward state, or nondeterministic scalar was found.

### P2

1. `REWARD_RECORD_SCHEMA_MISSING`: scalar reward is embedded in training result
   and `native-decision-transition/1.0`, not a standalone versioned record.
2. `REWARD_POLICY_ID_VERSION_HASH_MISSING`: no canonical formula source or
   authority hash exists.
3. `REWARD_REPLAY_REDERIVATION_NOT_VERIFIED`: strict replay proves World facts,
   while publication stores but does not independently recalculate reward.
4. `TRAINING_TRUNCATION_POLICY_PROVENANCE_GAP`: max-step/max-window config and
   semantics are not carried by a reward-policy record.
5. `DUPLICATED_SPARSE_REWARD_FORMULA`: single-seat and MultiSeat helpers encode
   equivalent formulas separately.
6. `REWARD_COMPONENT_DECOMPOSITION_MISSING`: base/shaping/source provenance is
   implicit.
7. `SPARSE_TERMINAL_REWARD`: correct but creates credit-assignment and possible
   stalling risks.
8. `INFO_SUPERVISION_CLASSIFICATION_INFORMAL`: winner/outcome and truncation
   source are out-of-band, but there is no versioned info/supervision schema.
9. `NONFINITE_VALIDATION_LATE`: current producers emit constants, but
   `DecisionTransition` accepts a numeric NaN until canonical JSON writing
   rejects it.
10. `EIGHT_FACTOR_SHAPING_CONTRACT_MISSING`: analysis is properly isolated but
    has no opt-in reward role, version, or ablation contract.

### P3

1. Private names `_reward` and `_rewards` do not identify the sparse terminal
   policy or producer namespace.
2. Terminal/truncation source and future reward-component codes are strings
   rather than one stable reward-specific enum contract.

## 20. Contract Blockers / Readiness

Contract blockers: `NONE`.

The formula, transition point, side relation, privileged boundary, and replay
requirements are clear enough to implement without duplicating Engine
terminal logic.

```text
REWARD_SCHEMA_VERSION_READINESS: READY
REWARD_POLICY_HASH_READINESS: PARTIAL
REWARD_REPLAY_READINESS: READY_AFTER_FIX
MULTISEAT_REWARD_READINESS: READY
EIGHT_FACTOR_REWARD_INTEGRATION_READINESS: READY_AFTER_REWARD_CONTRACT
REWARD_CHANNEL_IMPLEMENTATION_READINESS: READY
```

`READY_AFTER_FIX` for replay means adding the derived reward policy/record and a
recomputation verifier. It does not authorize modifying World or authoritative
replay hashes.

## 21. Required Implementation Tests

- **R1** same transition plus same reward policy produces the same record and
  semantic hash.
- **R2** restore a pre-transition World snapshot, replay the same exact action
  and delta, and obtain the same reward.
- **R3** strict replay plus the same reward/truncation configuration reproduces
  the complete ordered reward sequence.
- **R4** RED and BLUE records share one joint transition identity.
- **R5** Engine terminal win/loss/draw values are `+1/-1/0` exactly.
- **R6** native and training truncation produce zero and retain distinct source
  codes.
- **R7** reward consumes the Engine winner/reason authority and never infers a
  winner from health, score, observation, or text.
- **R8** reward calculation mutates no World, Engine, Decision, observation,
  catalog, capability, timer, or RNG.
- **R9** enabling/calculating reward does not change Decision, encoded policy
  observation, action catalog, or mask.
- **R10** finite ordered component amounts sum exactly to total reward.
- **R11** canonical reward policy version/hash is stable and changes on any
  authority-level semantic change.
- **R12** UI/presentation text, exception text, and Python repr are absent from
  records and hashes.
- **R13** episode return equals the ordered scalar reward sum and remains
  outside policy observation.
- **R14** bool, NaN, positive/negative infinity, and nonnumeric rewards fail at
  record construction.
- **R15** current RED/BLUE relation is zero-sum for nonterminal, truncation,
  draw, RED win, and BLUE win; side-swap reverses signs.
- **R16** invalid index, stale action, preview reject, and failed execution
  produce no committed transition/reward record and no implicit penalty.
- **R17** WAIT has no direct component; only a coincident terminal outcome can
  make the transition reward nonzero.
- **R18** zero-action terminal state receives no synthetic action or extra
  reward and cannot be stepped again.
- **R19** MultiSeat rewards use the same pre-state action catalogs and same
  committed post-state; no intermediate RED-state reward is observable.
- **R20** reward flags/references and returned next Decision observation have
  identical transition/frame/time identity.
- **R21** privileged winner/GREEN-derived future supervision never enters
  policy Decision/tensor/mask or mutates next observation.
- **R22** future Eight Factor shaping disabled yields byte-identical base reward
  records and hashes.
- **R23** future shaping enabled appears as a separate component and policy
  version/hash with explicit privileged provenance.
- **R24** component/action ordering and hashing are deterministic across input
  mapping order and processes; Python `hash()` is not used.

## 22. Recommended Implementation Phases

### Reward Phase 1: Base Reward Record / transition binding

- add immutable `TrainingRewardRecord` and `JointTrainingRewardRecord`;
- add canonical `native-sparse-terminal-zero-sum` policy source and hash;
- centralize the current formula without changing values;
- bind exact Decision/Catalog/action/frame/time identities;
- validate finite values, components, side relation, and hashes.

### Reward Phase 2: NativeTrainingEnv integration

- have both single-seat surfaces project the shared record;
- preserve Gym scalar/flags while exposing record only on explicit training
  supervision APIs;
- keep Decision encoder and action mask independent;
- record training truncation source/config.

### Reward Phase 3: MultiSeat reward record

- construct one joint master record after one committed post-state;
- project RED/BLUE side records;
- add zero-sum, same-transition, snapshot, and strict-replay verification.

### Reward Phase 4: Optional Eight Factor shaping

- define a separate opt-in policy/version/hash and exact potential/sampling;
- preserve base-only mode and ablation;
- treat GREEN use as privileged supervision;
- keep analyzer derived, read-only, and non-adjudicative.

### Reward Phase 5: Reward/evaluation logging and offline dataset

- serialize stable `(s, a, r, s')` references and reward records;
- keep GREEN analysis/evaluation sidecars separate;
- record reward/encoder/perspective identities in benchmark/checkpoint metadata;
- verify offline recomputation without changing authoritative replay.

## Verification Evidence

The audit inspected:

- `training/env.py`: single-seat commit, reward, truncation, and info order;
- `training/multi_seat.py`: same-window joint execution, reward pair,
  single-seat projection, and training truncation replacement;
- `training/models.py` and `training/transitions.py`: transition identity,
  serialization, and nonfinite JSON behavior;
- `training/publication.py`: transition/fact alignment, strict replay, analysis
  sidecar, and current absence of reward rederivation;
- `systems/terminal.py`, `core/engine.py`, and `rules/terminal_rules.py`: winner,
  draw, objective, elimination, and max-ticks authority;
- `gateway/session_manager.py` and `gateway/models.py`: post-state ControllerView
  terminal/winner/outcome projection;
- `training/decision_encoder.py`: explicit supervision exclusion;
- `analysis/analyzer.py`, `analysis/green_frame_contract.py`, and
  `analysis/advantage.py`: detached GREEN Eight Factor role and provenance;
- training, MultiSeat, Decision transition, terminal/objective, replay,
  publication, and Eight Factor tests.

Focused existing tests completed during this audit:

```text
training/reward/transition/Decision encoder: 66 passed
terminal/replay/local runner/Eight Factor:     178 passed, 1 skipped
full repository suite:                         1910 passed, 10 skipped
```

A read-only `/tmp`-free process probe evaluated the current MultiSeat helper for
nonterminal, RED win, BLUE win, draw, and truncation. Results were respectively
`(0,0)`, `(+1,-1)`, `(-1,+1)`, `(0,0)`, and `(0,0)`; every pair summed to zero.

The full suite completed with zero failures in 294.56 seconds. Tracked changes
and this new untracked document both passed whitespace-error checks; final
worktree status is reported in the task handoff because it includes the large
pre-existing preserved change set.

## Reward Phase 1–3 Implementation Verification

### Canonical policy and schema

Reward v1 is now implemented from the sole machine source
`config/training/native_sparse_terminal_reward_v1.json`, validated by the
strict JSON schema `config/schemas/native_training_reward_policy.schema.json`.
The policy identity is:

```text
policy_id:      native-sparse-terminal-zero-sum
policy_version: 1.0.0
policy_hash:    5653b7653eb46d3f091d5d0c5c04ab7dd5ce380236a4d2455214ab735a39b0cf
relation:       ZERO_SUM
```

The hash is SHA-256 over canonical JSON (UTF-8, sorted object keys, compact
separators, non-finite values forbidden). It covers all authority semantics in
the policy document and excludes its path, comments, runtime reward values,
World/Decision hashes, wall time, learner state, and rendering. The loader is
read-only, exact-field/fail-closed, and rejects any mapping or shaping semantic
other than the frozen v1 contract. No training path retains a fallback
`winner -> +/-1` formula.

### Immutable record contracts

`training/reward.py` implements these immutable value contracts:

- `native-training-reward/1.0` — `TrainingRewardRecord`;
- `native-training-joint-reward/1.0` — `JointTrainingRewardRecord`;
- `RewardTransitionIdentity`, `RewardComponent`, `RewardPolicyIdentity`, and
  typed reward/outcome/source/truncation enums;
- `TrainingTruncationProvenance`, with a distinct canonical configuration hash
  for `max_steps` or `max_decision_windows`.

Every committed transition has exactly one component,
`base:TERMINAL_OUTCOME`. Nonterminal, draw, native truncation, and training
truncation amounts are zero. RED/BLUE terminal win/loss is exactly `+1/-1` and
every joint record validates a zero sum. `total_reward` must equal its finite
component sum and remain in `{-1.0, 0.0, +1.0}`. Bool, NaN, and infinities fail
at reward-component/record construction. Machine records contain no free-form
reason text or Eight Factor values.

The transition identity binds the real episode/run namespace, side, decision
window, before/after frame and simulation time, closed generated fact-frame
range, before/after Decision hashes, before Catalog hash, submitted catalog
action IDs where those identities exist, a deterministic submitted-action
semantic hash, and a shared joint-action/transition hash. Product/Seat
operations that do not expose catalog action IDs use `submitted_action_ids =
null` plus their explicit canonical operation semantic hash; IDs are never
invented from private Engine objects.

### Training integration and atomicity

`NativeTrainingEnv`, `NativeDecisionSession`, `NativeMultiSeatEnv`, and
`SingleSeatTrainingAdapter` now use the canonical mapper. Their outward Gym or
existing training scalar APIs remain unchanged, while each successful step is
internally backed by a `TrainingRewardRecord`; MultiSeat builds one joint
record and projects both side records. RED and BLUE identities share the same
pre-state interval, joint action hash, committed post-frame, and simulation
time. Engine `GameResult` remains the only winner/terminal-reason authority.

Training limits no longer overwrite only loose scalar fields. They rebuild the
final joint record with an explicit `TRAINING_MAX_STEPS` or
`TRAINING_MAX_DECISION_WINDOWS` source and a separate training-config hash.
Native `max_ticks` remains `NATIVE_MAX_TICKS`. If Engine termination and a
training limit coincide, Engine termination takes precedence. Rejected,
invalid, stale, preview-failed, and pre-commit execution failures create no
transition and no reward record. WAIT has no action cost. A post-terminal step
is rejected and cannot create a second terminal reward.

Episode return is accumulated only from committed record totals. The `info`
compatibility surface now adds one classified `training_supervision` group
containing record/policy/transition hashes and return; it is out-of-band and is
not consumed by the Decision encoder. Existing winner/outcome compatibility
keys remain temporarily available, so `INFO_SUPERVISION_CLASSIFICATION_INFORMAL`
is contained rather than completely removed.

### Replay-derived verification

`training/reward_replay.py` reconstructs ordered joint records from the two
side records embedded in each canonical Decision transition and re-derives the
sequence using the versioned policy plus replay-authoritative terminal result.
Training publication now performs this after strict World replay succeeds and
compares count, exact records, transition identities, values, flags, reasons,
and semantic/sequence hashes. It records derived reward policy metadata,
record count, sequence hash, and `reward_replay_match` in the training summary.
No new authoritative replay member is added: stored expected rewards remain in
the existing transition JSONL, and the World/replay manifest and final World
hash semantics are unchanged.

Snapshot/restore verification starts from an authoritative pre-transition
World snapshot, reapplies the same action/delta, confirms the same Engine
result, and derives an equal Reward/Joint record and hash. Strict replay uses
the initial scenario, ordered actions, and exact deltas to prove World state
first, then independently re-derives the reward sequence rather than trusting
the stored scalar.

### Policy-input and privileged-supervision boundary

Reward building does not accept or mutate World, Engine, ControllerView,
Decision, observation tensor, capability, catalog, mask, RNG, or wall clock.
It consumes detached transition/result evidence. Engine terminal authority is
explicitly marked privileged supervision for terminal outcome records; no
GREEN frame or analyzer payload is read. Decision and encoder regression tests
confirm that reward, winner, terminal reason, components, privileged flags,
and Eight Factor data do not enter the policy record or mask.

Eight Factor remains a read-only evaluation sidecar. The canonical policy has
`shaping.enabled = false`; attempts to load an enabled v1 shaping policy fail
closed. Any future shaping must use a separate policy/version/hash and
component contract, retain base-only ablation, and declare GREEN-analyzer
privilege.

### R1–R24 verification

| Test | Verification |
|---|---|
| R1 | Same identity/policy produces equal side/joint records and hashes. |
| R2 | Pre-transition World snapshot/restore plus identical commit produces the same result and reward/hash. |
| R3 | Strict replay re-derives the complete stored reward sequence. |
| R4 | RED/BLUE share one validated joint transition identity. |
| R5 | RED win, BLUE win, loss, and draw mappings are exact. |
| R6 | Native and training truncation are zero with distinct typed provenance. |
| R7 | Terminal winner/reason comes from `NativeEngine` `GameResult`; no wrapper inference remains. |
| R8 | Reward construction is pure and mutation-free. |
| R9 | Reward reconstruction leaves Decision observation and catalog masks unchanged. |
| R10 | The component sum equals total exactly. |
| R11 | Policy id/version/hash is stable and attached to every record. |
| R12 | Presentation text, exception repr, and UI messages are absent. |
| R13 | Episode return equals the sum of committed record totals. |
| R14 | Bool, NaN, and positive/negative infinity are rejected at the reward boundary. |
| R15 | Zero-sum and winner side-swap are exact. |
| R16 | Invalid/preview-rejected action creates no transition/reward. Existing stale tests preserve the same result. |
| R17 | WAIT has no independent component or cost. |
| R18 | Terminal/empty-action state cannot be stepped for another reward. |
| R19 | MultiSeat records share the committed pre/post interval. |
| R20 | Reward after-frame/time/Decision hash matches the returned next decision point. |
| R21 | Privileged supervision fields remain outside policy input. |
| R22 | Eight Factor absence leaves base records unchanged. |
| R23 | Enabling shaping under v1 is unsupported and fails closed. |
| R24 | Canonical JSON ordering is deterministic and uses no Python `hash()`. |

The base and joint builders are O(1). Sequence verification is O(number of
committed decision windows). No analyzer, replay/history scan, LOS,
pathfinding, capability query, network service, or LLM participates in an
online reward calculation.

### P2 reclassification and next readiness

```text
REWARD_RECORD_SCHEMA_MISSING:                 CLOSED
REWARD_POLICY_ID_VERSION_HASH_MISSING:        CLOSED
REWARD_REPLAY_REDERIVATION_NOT_VERIFIED:      CLOSED
TRAINING_TRUNCATION_POLICY_PROVENANCE_GAP:    CLOSED
DUPLICATED_SPARSE_REWARD_FORMULA:             CLOSED
REWARD_COMPONENT_DECOMPOSITION_MISSING:       CLOSED
NONFINITE_VALIDATION_LATE:                    CLOSED_AT_REWARD_BOUNDARY
SPARSE_TERMINAL_REWARD:                       EXPECTED_BY_DESIGN
INFO_SUPERVISION_CLASSIFICATION_INFORMAL:     CONTAINED
EIGHT_FACTOR_SHAPING_CONTRACT_MISSING:        P2, DEFERRED
```

```text
EIGHT_FACTOR_SHAPING_IMPLEMENTATION_READINESS: READY
AUXILIARY_LABEL_READINESS:                     READY
```

The next phase may define optional Eight Factor shaping or auxiliary labels,
but must not alter `base:TERMINAL_OUTCOME`, reward policy v1, Decision schemas,
or the policy-input boundary.
