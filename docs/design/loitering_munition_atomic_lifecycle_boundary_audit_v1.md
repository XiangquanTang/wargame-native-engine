# Loitering Munition v1 Atomic / Lifecycle Boundary Audit

## Scope and result

This is a read-only architecture and adjudication audit of the implemented
predeployed Loitering Munition v1.  It does not add a launcher, mission,
autonomous behavior, interception rule, action type, or combat rule.  The
behavior remains Native simplified adjudication.

Overall verdict after bounded repair: **PASS**.

The same-pre-state, staged resolution, working-world transaction, lifecycle
segmentation, target-effect aggregation, RNG, post-death capability cleanup,
and millisecond snapshot/restore behavior are deterministic.  The audit found
two P1 boundary defects:

1. simultaneous combat destruction and self-consumption emit two conflicting
   `UNIT_DESTROYED` facts for the same loitering operator; and
2. classic Strict Replay cannot represent a step ending within the same integer
   tick as its start because snapshot filenames use `world.tick` and overwrite
   one another.

Both findings have now been repaired without changing same-pre-state
adjudication, the weapon 76 contract, lifecycle duration, or action semantics.
The original findings remain described below as forensic context; the final
status and test evidence are recorded in **Repair Verification**.

## 1. Current semantics

The frozen v1 contract remains:

- Source identity `type=3/subtype=7` resolves to canonical
  `LOITERING_MUNITION`, domain `AIR`.
- The operator starts in `WorldState`, uses ordinary MOVE/Visibility/SHOOT, and
  weapon 76 targets observable PERSONNEL/VEHICLE within two hexes.
- `lifecycle_remaining_ms` starts at 1,200,000 and advances only through the
  authoritative integer `delta_time_ms` axis.
- A formally resolved attack uses `SELF_AS_MUNITION` and consumes the attacker
  on `ATTACK_RESOLUTION_COMMITTED`, independently of loss, suppression, or
  `NO_EFFECT`.
- Validation rejection does not consume the operator or attack RNG.
- Attack consumption and expiry are represented by `UNIT_DESTROYED` with
  distinct reason codes `LOITERING_MUNITION_CONSUMED` and
  `LIFECYCLE_EXPIRED`.

Authority is defined by
`config/rulesets/loitering_munition_attack_policy_v1.json` and
`config/rulesets/operator_lifecycle_profiles_v1.json`.  The target effect and
self-consumption integration is in
`NativeEngine._apply_source_fire_intents()`.

## 2. Same-pre-state behavior

Status: **PASS**.

The real source-compatible step order is:

```text
clone authoritative WorldState and NativeRNG
-> normalize a sealed ordered action batch
-> clone tick-start pre_state
-> common validation against pre_state
-> stage every fire request/outcome against pre_state
-> aggregate and apply effects on detached working WorldState
-> advance movement/time/lifecycle
-> refresh observation and evaluate terminal state
-> atomically copy the complete working state into authoritative WorldState
```

Evidence:

- `NativeEngine._step_ms()` clones WorldState, RNG, counters, events, and valid
  action cache before dispatch and restores every reference on failure.
- `NativeEngine._step_source()` creates one `pre_state` before validating or
  staging any submitted action.
- `_stage_source_shoot()` reads attacker, target, observation, range, and state
  from that `pre_state`.
- `_apply_source_fire_intents()` aggregates target losses after all resolutions
  exist.
- `_commit_world()` is the sole authoritative commit point.

Consequently, a shooter destroyed by another same-tick effect still executes a
fire intent which was legal at tick start.  This matches the documented
`destroyed_actor_executes_submitted_action` and `aggregate_then_apply`
semantics.

## 3. Attack vs expiry

Status: **PASS** for the implemented and documented ordering.

Actions are validated, staged, and applied at the start of the step.  Only then
does `_advance_source_operator_time(delta_time_ms)` advance movement and finite
lifetime.  This is not a time-slice-before-action model.

Observed boundary results:

| Case | remaining | delta | result |
|---|---:|---:|---|
| B1 | 6000 ms | 5000 ms | attack accepted; consumed |
| B2 | 5000 ms | 5000 ms | attack accepted first; consumed; no expiry fact |
| B3 | 4000 ms | 5000 ms | attack accepted first; consumed; no expiry fact |

Thus a valid start-of-step SHOOT wins the action/expiry boundary.  Once attack
consumption makes the operator dead, lifecycle expiry skips it and does not add
`LIFECYCLE_EXPIRED`.

## 4. Attack vs attacker destruction

Status after repair: **PASS**.

An audit-only scenario-local AIR-target profile was used to exercise the joint
pipeline without asserting a new production anti-air rule.  Both actions were
accepted against the same pre-state, both resolutions completed, and the final
world and RNG were deterministic.  Strict Replay also reproduced the result.

Before repair, the fact stream was incorrect at the destruction boundary.
When enemy fire
reduces the loitering operator from one blood to zero, the aggregate target loop
first sets it destroyed and appends a combat `UNIT_DESTROYED`.  The later
self-consumption loop calls `_destroy_source_operator()` again and appends a
second `UNIT_DESTROYED`:

```text
UNIT_DESTROYED actor=1 target=1
  reason=LOITERING_MUNITION_CONSUMED
  target_blood_before=0 target_blood_after=0 losses=0

UNIT_DESTROYED actor=2 target=1
  reason=<combat event has no explicit reason_code>
  target_blood_after=0
```

The staged loitering attack is not lost, health never becomes negative, and the
second cleanup produces no additional state delta.  Nevertheless, two
conflicting death facts for one operator violated destruction/event
idempotency. The unified fire commit now stages both causes, performs one
destruction transition, and publishes one fact containing both complete cause
records in deterministic action/event order.

## 5. Attack vs target destruction

Status: **PASS**.

With a loitering attack and a second friendly direct-fire attack against one
one-blood target:

- both actions were accepted from the same pre-state;
- both adjudication IDs were retained;
- requested losses were aggregated before mutation;
- blood was clamped at zero;
- exactly one target `UNIT_DESTROYED` was emitted; and
- the event contained two `aggregate_adjudication_ids`.

One same-tick effect cannot silently invalidate the other staged effect.
Action sequence remains the authoritative RNG/evidence order; permuting an
ordered action stream is a different input, not nondeterminism.

## 6. Duplicate own action

Status: **PASS** for preventing multiple resolutions.

`DecisionWindow.submit()` rejects a second selected action for the same actor
with `duplicate_actor_action`.  `NativeEngine._step_source()` independently
enforces the invariant for direct ordered-list callers: the first action is
accepted and a later action for the same actor is rejected with
`DUPLICATE_ACTOR_ACTION` before staging.

Submitting the same loitering SHOOT twice produced:

```text
decision 1: accepted
decision 2: DUPLICATE_ACTOR_ACTION
combat_result_looked_up count: 1
LOITERING_MUNITION_CONSUMED count: 1
```

Input order determines which one action survives, but the exact ordered input
is recorded and replayed.  Two attacks cannot consume one loitering operator in
the same batch.

## 7. Movement vs expiry

Status: **PASS**.

`_advance_source_operator_time()` slices at the nearest finite-lifecycle
boundary and invokes ordinary `MovementRuntime.advance()` only for that slice.
For an 8000 ms adjacent AIR segment:

| remaining | observed result |
|---:|---|
| 9000 ms | ARRIVED at 8000; expired at 9000; final hex 102 |
| 8000 ms | ARRIVED and expired at 8000; final hex 102 |
| 7000 ms | no ARRIVED; expired at 7000; final hex remains 101 |

At expiry, segment target, fractional position, path, speed, and movement IDs
are cleared by the shared destruction transition.  A large outer step does not
advance the dead operator past its expiry instant.

## 8. WAIT and illegal attack near expiry

Status: **PASS**.

An explicit WAIT at a 1000 ms remaining boundary emitted WAIT at time zero,
then `UNIT_DESTROYED/LIFECYCLE_EXPIRED` at time 1000.  RNG did not change.

An out-of-observation SHOOT at the same boundary produced:

```text
DIRECT_FIRE_REQUESTED at 0
ACTION_REJECTED code=TARGET_NOT_OBSERVED at 0
UNIT_DESTROYED reason=LIFECYCLE_EXPIRED at 1000
```

It did not produce a combat lookup, consume the operator as ammunition, or
advance attack RNG.

## 9. SELF_AS_MUNITION atomicity

Status: **PASS** for state/RNG transactionality.

Target aggregation and self-consumption execute on the same detached
working-world inside `_apply_source_fire_intents()`.  There is no callback,
second `step`, or controller-owned mutation.  An injected exception exactly at
the self-destruction call, after target effects had been calculated/applied to
the working copy, restored:

- the complete authoritative snapshot;
- NativeRNG;
- previous event state; and
- action/event sequences.

No intermediate target-damaged/attacker-alive or attacker-dead/target-unchanged
authoritative state was exposed.  The duplicate-death finding is a fact-level
idempotency defect, not a partial-commit defect.

## 10. Destruction idempotency

Status after repair: **PASS** at state and event/fact level.

`_destroy_source_operator()` consists of deterministic assignments and is
state-idempotent when invoked twice.  It does not report whether the operator
was already destroyed, however, and event emission is owned independently by
its callers.  `_apply_source_fire_intents()` does not suppress or merge
self-consumption when the same aggregate fire batch already destroyed the
attacker as a target.

The repair is inside the unified fire aggregate commit. It computes one
destruction fact per operator after target and self-consumption causes are
known, applies cleanup once, and preserves both causes. No cause-precedence
rule was invented: a multi-cause fact records `primary_reason_code=null`, while
its ordered `cause_codes` and nested original event evidence remain complete.
Single-cause events retain their established payload.

## 11. Event ordering

Status after repair: **PASS**.

Events are sorted by:

```text
event_time_ms
action_sequence
event_sequence
event priority
roll index
side / actor / target / event_id
```

This is deterministic and replay-stable.  In the simultaneous attacker-death
probe, the self-consumption event belongs to action sequence zero and therefore
sorts before the combat-destruction event belonging to action sequence one,
even though aggregate target mutation was applied before the self-consumption
loop. Stable evidence ordering is not an initiative rule. After repair, it
orders causes within one destruction fact rather than conflicting destruction
facts.

Normal target aggregation emits losses/suppression/destruction before the
self-consumption fact for the same loitering action.  Lifecycle expiry occurs
later at its exact `event_time_ms` and is skipped after attack consumption.

## 12. RNG determinism

Status: **PASS**.

- Formal attacks consume NativeRNG during deterministic staging order.
- Invalid attacks fail before combat lookup and do not consume attack RNG.
- Same-target aggregation does not reroll.
- Self-consumption and lifecycle expiry consume no RNG.
- Attack plus attacker destruction, target destruction, and expiry probes
  reproduced identical RNG hashes for identical initial state, seed, ordered
  actions, and delta.

The R5 duplicate-death defect itself is deterministic and Strict Replay
reproduces it exactly; replayability does not make that event semantics valid.

## 13. Post-death capability and observation cleanup

Status: **PASS**.

After attack consumption or lifecycle expiry:

- `alive=false`, blood/health are zero, and state is `destroyed`;
- active movement runtime is cleared;
- `valid_actions()` contains no action for the operator;
- Human capability projection therefore exposes no controllable operation;
- a new Agent frozen-valid-action set cannot contain the operator;
- own-side observation retains the dead friendly fact but not a valid action;
- enemy visible contacts do not expose it as an active visible enemy; and
- GREEN retains the authoritative destroyed state.

A stale or forged later action reaches Engine common validation and is rejected
as `ACTOR_DESTROYED`.

## 14. Snapshot / restore boundaries

Status: **PASS** for WorldState/RNG restore.

The following snapshots restored and continued identically:

- `lifecycle_remaining_ms=1` immediately before expiry;
- mid-segment at 4000/8000 ms with 5000 ms lifetime remaining;
- before a formal attack; and
- after attack consumption.

Continued world hash, RNG, events, movement completion, expiry time, and final
hex matched.  The one-millisecond boundary emitted one expiry event after
restore; no off-by-one or double expiry occurred.

## 15. Strict Replay boundary cases

Status after repair: **PASS**.

At replay-safe step boundaries, all six requested paths passed strict
action/world/RNG/event re-execution, and independently repeated GREEN projection
produced the same hash:

| Case | strict | World hash | GREEN hash |
|---|---|---|---|
| R1 attack/self-consumption | PASS | `fb369fc5bb3ba24c7db07e65441bef5192499416e3bcd9baa2e971c99d12f183` | `99f297999fc156257dea9e4e4c8c4fefc35d8a5107e6504d37a781a9dc9aa315` |
| R2 pure expiry | PASS | `74a10b2f4e426eb7b8b3654148a33d03eab473d7407705517626188c79e55737` | `62654b4576c35cc040f681774583f96ca4cae012166f27f0f5aae9a4ce4e81d1` |
| R3 movement cut off at 7000 ms | PASS | `0b55062ea778bbd6c5c793efb2dbdad1be323bd4256a093899ad5b30a110afc7` | `be729966d6b4079a9c10ff92b5c38b4ff4b02e607967b8d11a06b524f15d5315` |
| R4 illegal attack/exact 1000 ms expiry | PASS | `be18953928238ed46a0f92b84e857d48e0cc5936cf60acea5c74dcbf44a61fc2` | `47521815a2191a1d6e8c57bf2258a8e625b9a877ad97a7a6edd212d291558b89` |
| R5 attack vs attacker destruction | PASS after single-fact repair | pre-repair hash superseded | pre-repair hash superseded |
| R6 attack vs aggregate target destruction | PASS | `87332476971dfaff5c196e77b4101a213c38a492c28cebdbecfcffa1e5605b66` | `b3195fdc68545e9b1a6da66e5a66fa91f0f150cbf995b5cc53fce76432da96e2` |

Before repair, the classic `ReplayWriter.record_snapshot()` path used
`snapshots/tick-{world.tick:08d}.json`.  Since source-compatible tick is
`elapsed_time_ms // 1000`, an initial snapshot at time zero and a snapshot after
a 1 ms step both use `tick-00000000.json`.  The latter overwrites the former,
and Strict Replay then fails with:

```text
replay snapshot/step count mismatch
```

The step stream already recorded exact `delta_time_ms`; the collision was in
snapshot identity/storage. New archives now use a replay-local monotonic
`snapshots/step-{sequence:08d}.json` identity. Reader ordering is explicit and
continues to accept unmixed legacy `tick-XXXXXXXX.json` archives.

## 16. Findings and risk

### P0

None.  No nondeterministic WorldState, partial atomic commit, RNG divergence, or
failure to re-execute ordinary replay boundaries was observed.

### P1 (repaired)

1. **Duplicate destruction facts on simultaneous combat destruction and
   self-consumption.**  Final state is stable, but the same operator receives a
   combat `UNIT_DESTROYED` and a zero-loss
   `LOITERING_MUNITION_CONSUMED` `UNIT_DESTROYED` in one tick.
2. **Subsecond classic Replay snapshot collision.**  Multiple committed states
   sharing integer `world.tick` overwrite the same snapshot filename, so exact
   millisecond lifecycle steps below one second cannot pass Strict Replay.

Both P1 findings are closed by the bounded repairs described below.

### P2

1. Classic strict replay verifies action/world/RNG/event trajectories; GREEN
   determinism for the six audit probes was checked by independent repeated
   source projection rather than stored inside the classic replay archive.

The two earlier evidence-expression gaps are now covered by committed
regressions and explicit ordered multi-cause evidence. No unrelated P2 was
changed.

### P3

None material beyond the P2 evidence-expression items.

## Repair Verification

### P1-1: destruction fact deduplication

Before repair, target-effect application and loitering self-consumption each
mutated/emitted independently. A simultaneously combat-destroyed loitering
operator therefore produced two `UNIT_DESTROYED` events even though its final
state was deterministic.

After repair, `_apply_source_fire_intents()` stages destruction causes by
operator. At the unified fire-commit boundary it:

1. sorts causes by action sequence, event sequence, cause code, actor, and
   target;
2. invokes the existing destruction transition once;
3. emits at most one `UNIT_DESTROYED` per operator for the commit; and
4. retains each original cause event, its adjudication ID, and its evidence in
   the single fact.

For a multi-cause fact, `primary_reason_code` is `null`. The repository has no
authority for choosing combat over self-consumption or the reverse, and no
adjudication behavior depends on a primary reason. Therefore no rule blocker
remains: both causes are retained without inventing precedence. Single-cause
events keep their established payload.

Combat effects are still staged from the same tick-start pre-state. The repair
does not cancel a fire intent merely because another same-tick effect destroys
its shooter. Combat destruction occurs before lifecycle time advancement, and
the lifecycle loop ignores already-dead operators, so combat plus exact-boundary
expiry also produces only one destruction fact.

### P1-2: replay snapshot identity

Before repair, `world.tick` supplied the snapshot filename. Because it is an
integer-second time projection, distinct states at 0 ms, 1 ms, and 999 ms could
overwrite `tick-00000000.json`.

After repair, `ReplayWriter` owns a replay-local monotonic sequence and writes:

```text
snapshots/step-00000000.json
snapshots/step-00000001.json
...
```

The sequence is archive metadata only. It is not written into WorldState and
does not affect world hash, Engine time, action order, or RNG. `ReplayReader`
orders the explicit numeric step layout and still accepts unmixed legacy
`tick-XXXXXXXX.json` archives. Mixed layouts fail closed. This storage-only
change does not require a replay semantics version bump.

The following exact elapsed-time sequences now retain every state and pass
Strict Replay:

- `0 -> 1` ms;
- `0 -> 1 -> 2 -> 999` ms;
- `0 -> 500 -> 1000 -> 1500` ms;
- `0 -> 1 -> 999 -> 1000 -> 1001 -> 5000` ms; and
- a one-millisecond lifecycle expiry with initial and expiry snapshots.

Repeated recording with the same scenario, seed, actions, and deltas produces
the same snapshot names, snapshot contents, action/event/step streams, final
world hash, and Strict Replay result.

### Boundary R1-R8

| Boundary | Result | Regression evidence |
|---|---|---|
| R1 attack + self-consumption | PASS | `test_weapon76_attack_strict_replay` |
| R2 pure expiry | PASS | `test_lifecycle_expiry_strict_replay` |
| R3 movement near expiry | PASS | `test_movement_cut_off_by_lifecycle_boundary_strict_replay` |
| R4 illegal attack near expiry | PASS | `test_illegal_attack_at_expiry_boundary_strict_replay` |
| R5 attack vs attacker destruction | PASS | simultaneous single-fact test plus subsecond Strict Replay |
| R6 attack vs target destruction | PASS | `test_simultaneous_target_destruction_aggregates_and_strict_replays` |
| R7 subsecond lifecycle expiry | PASS | `test_one_millisecond_lifecycle_expiry_replay_keeps_both_snapshots` |
| R8 subsecond simultaneous destruction | PASS | `test_simultaneous_destruction_strict_replay_at_subsecond_step` |

Verification on 2026-08-26:

- focused loitering/joint/direct/replay/source-recorder set: `113 passed`;
- historical Direct/Guided/Indirect hash regressions: `7 passed`;
- full repository suite: `1687 passed, 10 skipped, 0 failed` in 311.26 s;
- `git diff --check`: PASS.

## 17. Final verdict

```text
SAME_PRE_STATE: PASS
ATTACK_VS_ATTACKER_DESTRUCTION: PASS
ATTACK_VS_EXPIRY: PASS
DUPLICATE_OPERATOR_ACTION: PASS
ATTACK_VS_TARGET_DESTRUCTION: PASS
MOVEMENT_VS_EXPIRY: PASS
WAIT_VS_EXPIRY: PASS
ILLEGAL_ATTACK_NEAR_EXPIRY: PASS
SELF_AS_MUNITION_ATOMICITY: PASS
RNG_DETERMINISM: PASS
EVENT_ORDERING: PASS
DESTRUCTION_IDEMPOTENCY: PASS
POST_DEATH_CAPABILITY_CLEANUP: PASS
SNAPSHOT_RESTORE: PASS
STRICT_REPLAY: PASS
OVERALL: PASS
```

`LOITERING_MUNITION_V1_BOUNDARY_CLOSED`
