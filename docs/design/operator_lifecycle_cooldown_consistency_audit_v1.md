# Native Engine Operator Lifecycle & Cooldown Consistency Audit v1

Status: **PARTIAL**

Audit type: read-only architecture, state-machine, and authoritative-time audit

Scope: current working tree as inspected on 2026-08-26

Rules characterization: Native simplified rules; this report does not claim SDK or official-rule equivalence.

## 1. Executive Summary

NativeEngine has a real millisecond authoritative-time backbone. Source-compatible execution accepts an explicit `delta_time_ms`, advances a detached working world, stages actions from one sealed pre-state, advances movement and timers, and commits atomically. Movement segments, stop/weapon-posture transitions, fatigue recovery, boarding end state, weapon cooldown, suppression countdown, and limited Operator lifecycle all use integer elapsed time rather than wall clock or step count.

That backbone is not yet consumed consistently by every configured time rule. The two historical P1 suspicions are confirmed:

1. `guided_fire_recovery_remaining_ms` is set and enforced, but never decremented. A committed guided shot therefore permanently disables further guided fire.
2. Indirect fire's configured 300-second planned cooldown has no shooter/runtime field, is never set or decremented, and cannot block immediate creation of another mission.

Three additional P1 findings affect state/time correctness:

3. Suppression can stop a movement task while preserving the remaining path, but suppression expiry neither resumes nor clears it; the Operator is then stopped with an unresumable path that also blocks a new MOVE.
4. `OperatorState.to_dict()` conditionally omits several authoritative fields when `current_hex is None`. Boarding sets an embarked passenger's `current_hex` to `None`, so clone/commit/snapshot can silently reset fatigue, weapon cooldown, suppression, and movement-transition data.
5. Indirect mission phase advancement does not consume elapsed-time overshoot across FLYING→ACTIVE, and the ACTIVE same-integer-second guard can discard a sub-second interval.

No P0 nondeterminism or Replay bypass was found. Strict Replay re-executes recorded actions and `delta_time_ms`; it deterministically reproduces both correct and currently incorrect timer semantics. The system is suitable for a future read-only `OperatorLifecycleView` only after the P1 repairs.

Final readiness: **READY_AFTER_P1_FIX**.

## 2. Authoritative Time Model

### 2.1 Source-native execution chain

```text
GameSession / environment
  -> NativeEngine.step(..., delta_time_ms)
  -> NativeEngine._step_ms
       -> clone authoritative WorldState and NativeRNG
       -> NativeEngine._step_source
            -> seal one pre_state snapshot
            -> validate/stage actions against pre_state
            -> apply staged action effects to working world
            -> advance Operator movement/time by delta_time_ms
            -> advance boarding transitions by delta_time_ms
            -> advance indirect mission phases/areas
            -> refresh observation
            -> sort events deterministically
       -> atomic _commit_world
  -> Replay records ordered actions and exact delta_time_ms
```

The authoritative time value is `WorldState.elapsed_time_ms`. `WorldState.tick` is a seconds projection, set as `elapsed_time_ms // 1000`; it is not a unique Engine step identity. The Replay recorder uses an independent step sequence for archive identity.

The legacy non-Source path still advances in fixed one-second steps. This is a separate compatibility path and must not be treated as proof that arbitrary-duration timers are step counters.

### 2.2 Time inputs and hazards

| Question | Finding |
|---|---|
| Unified intended timer input | `delta_time_ms`, integer milliseconds |
| Authoritative accumulated time | `world.elapsed_time_ms` |
| `world.tick` meaning | `elapsed_time_ms // 1000` time projection |
| Per-step decrement found | No authoritative Operator timer intentionally decrements by `1`; indirect mission seconds/remainder logic is a mixed-unit implementation |
| Wall clock in adjudication | None found |
| Wall clock elsewhere | Gateway deadlines, telemetry/archive creation timestamps only; not used for combat or lifecycle adjudication |
| Large-tick support | Correct in Movement and limited lifecycle; incomplete at indirect mission phase boundaries |

`AUTHORITATIVE_TIME_MODEL: PARTIAL`

The core clock and transaction model pass. The status is PARTIAL because Guided recovery does not consume the clock and Indirect mission phase handling can discard elapsed time.

## 3. Operator State Axes

Operator authority is orthogonal; it is not represented by one exhaustive state enum.

```text
OperatorState
├── identity
├── survival/existence
├── deployment/location
├── movement and movement runtime
├── movement mode and stop transition
├── combat resources and weapon posture
├── suppression and fatigue
├── guidance authority/recovery
├── boarding relations/locks
├── concealment posture
├── limited lifecycle
├── current observation contacts (derived/raw compatibility)
└── source compatibility metadata/raw attributes
```

| State axis | Authoritative field(s) | Kind | Owner | Derived/compatibility notes |
|---|---|---|---|---|
| Survival | `alive`, `health`, `blood`, `state` | typed | Engine/destruction commit | `state` duplicates some orthogonal truth and requires invariant discipline |
| Deployment | `deployed`, `current_hex` | typed | World/Engine | On-board passenger is deployed false and has no independent hex |
| Movement | `speed`, `move_path`, segment runtime fields, `stopped` | typed | MovementRuntime | `moving` is derived from runtime fields |
| Movement mode | `movement_mode` | typed enum/value | Movement | NORMAL/MARCHING/CHARGE semantics are separate from stopped/moving |
| Stop transition | `move_to_stop_remaining_ms`, `movement_stop_requested` | typed | MovementRuntime | Millisecond timer |
| Generic transition | `transition_kind`, `transition_remaining_ms` | typed | MovementRuntime/Engine | Used for weapon posture and movement-mode transitions |
| Fatigue | `fatigue_level`, `fatigue_recovery_elapsed_ms` | typed | MovementRuntime | Elapsed accumulator rather than remaining timer |
| Suppression | `suppression_remaining_ms`, partly `state` | typed authority | Combat/MovementRuntime | Raw seconds compatibility value can become stale |
| Boarding | `mounted_on`, `passenger_id`, `movement_locked`; `WorldState.boarding_transitions` | typed | Boarding/Engine | Relation and transition history are separate |
| Concealment | `concealment_posture` | typed posture | Engine/concealment | Mirrored to raw `concealed`; explicit typed field is not serialized |
| Weapon posture | `weapon_locked`, `weapon_unfolded`, generic transition | typed | Engine/MovementRuntime | Capability and fire validation consume posture |
| Weapon cooldown | `weapon_cooldown_remaining_ms` | typed, Operator-global | Combat/MovementRuntime | No per-weapon runtime map |
| Guided recovery | `guided_fire_recovery_remaining_ms` | typed | Guided fire | Set/consumed but not advanced |
| Indirect planned cooldown | none | missing | Intended Indirect authority | Configuration exists without runtime binding |
| Limited lifecycle | `lifecycle_profile_id`, `lifecycle_remaining_ms` | typed | Engine/lifecycle profile | `None` means no limited lifecycle |
| Ammunition | `raw_attributes["ammunition"]` | raw but currently authoritative resource | Fire pipelines | Explicitly keyed by weapon/resource mappings where authority exists |
| Observation contacts | `raw_attributes["see_enemy_bop_ids"]` and detached observations | derived/compatibility | Observation refresh | Must not be treated as a global detected flag |

## 4. Timer / Cooldown Inventory

| Name | Owner | Field/runtime | Unit | Set | Advance | Clear/completion | Capability consumer | Snapshot | Replay | Verdict |
|---|---|---|---:|---|---|---|---|---|---|---|
| Movement segment | MovementRuntime | segment elapsed/traversal fields | ms | MOVE staging | segmented by `delta_time_ms` | arrive/start next segment | MOVE/STOP validation | yes when on map | re-executed | PASS |
| Move-to-stop | MovementRuntime | `move_to_stop_remaining_ms` | ms | STOP/suppression stop request | `delta_time_ms`, clamped | STOPPING_COMPLETED | MOVE/STOP and common legality | yes when on map | re-executed | PASS |
| Generic transition | Engine/MovementRuntime | `transition_remaining_ms` | ms | posture/mode action | `delta_time_ms`, clamped | applies transition and clears kind | common legality/posture | yes when on map | re-executed | PASS |
| Fatigue recovery | MovementRuntime | `fatigue_recovery_elapsed_ms` | ms | stopped recovery accumulation | `delta_time_ms`; multi-boundary loop | level recovery/reset accumulator | movement/readiness | yes when on map | re-executed | PASS |
| Weapon cooldown | Combat/MovementRuntime | `weapon_cooldown_remaining_ms` | ms | committed Direct/Guided fire | `delta_time_ms`, clamped | zero means ready | Direct/Guided/Indirect generic blockers | yes when on map | re-executed | PASS mechanics; authority granularity ambiguous |
| Guided recovery | Guided fire | `guided_fire_recovery_remaining_ms` | ms | committed guided effect plan | **none** | never | Guided capability and request validation | yes | re-executed as permanent lock | FAIL |
| Suppression | Combat/MovementRuntime | `suppression_remaining_ms` | ms | committed suppression result | `delta_time_ms`, clamped | state clears to STOPPED when applicable | MOVE, boarding, concealment, guided; Direct uses modifier | yes when on map | re-executed | FAIL due stranded movement task; timer decrement itself passes |
| Boarding | Engine/Boarding | `BoardingTransition.remaining_ms` | ms | committed BOARD/UNLOAD | `delta_time_ms`, clamped | BOARD unlock / UNLOAD restores passenger | Board/Unload/common locks | yes | re-executed | PARTIAL |
| Indirect plan cooldown | Intended Indirect authority | **no runtime field** | intended s/ms | not set | none | none | none | no | no semantic state | FAIL |
| Indirect flight | Indirect mission | remaining seconds + ms remainder | mixed | mission creation | `delta_time_ms` conversion | FLYING→ACTIVE | impact eligibility | yes | re-executed | PARTIAL: overshoot lost |
| Indirect active area | Indirect mission | active seconds + ms remainder | mixed | impact activation | `delta_time_ms` conversion | ACTIVE→RESOLVED | active-area resolution | yes | re-executed | PARTIAL: same-second guard can drop interval |
| Limited Operator lifecycle | Engine/lifecycle | `lifecycle_remaining_ms` | ms | explicit lifecycle profile binding | nearest-boundary segmentation | destruction reason `LIFECYCLE_EXPIRED` | alive/deployed filtering | yes | re-executed | PASS |

## 5. Guided Fire Recovery

`GUIDED_FIRE_RECOVERY: FAIL`

- **Set:** committed guided fire produces recovery updates for both shooter and guider. Ground/personnel guidance profiles currently supply 75 seconds, converted to 75,000 ms.
- **Commit boundary:** only a successfully validated/resolved/committed Guided attack sets the recovery values. Rejected intent does not.
- **Advance:** no code decrements `guided_fire_recovery_remaining_ms`. Repository-wide read/write tracing found declaration, serialization, validation, capability consumption, and commit assignment, but no `old - delta_time_ms` path.
- **Authority enforcement:** while nonzero, Engine request validation rejects forged/stale Guided fire, and capability generation omits it. This is not merely a UI restriction.
- **Recovery:** impossible under current execution; exact and large tick advances leave the value unchanged.
- **Snapshot/Replay:** the value is preserved and strict replay deterministically reproduces the permanent lock. Deterministic replay does not make the cooldown correct.

Probe observations:

```text
after committed Guided action: shooter=75000ms, guider=75000ms
after advancing 75999ms:       shooter=75000ms, guider=75000ms
after crossing boundary:       shooter=75000ms, guider=75000ms
forged Guided during recovery: rejected; RNG unchanged
```

P1: `GUIDED_FIRE_RECOVERY_TIMER_NOT_ADVANCED`.

## 6. Indirect Fire Planned Cooldown

`INDIRECT_PLANNED_COOLDOWN: FAIL`

- **Authority:** rule text and versioned profiles contain a 300-second planned cooldown. The resource policy names `PLAN_COOLDOWN_300_SECONDS`.
- **Distinct concepts:** this is shooter planning availability, not mission flight duration (150 seconds) and not active-area duration (300 seconds).
- **Runtime binding:** no Operator field or separate shooter cooldown runtime exists. Mission creation consumes ammunition and appends a mission, but does not attach planned cooldown to the shooter.
- **Advance/recovery:** nothing can decrement or clear a runtime that does not exist.
- **Capability:** an immediately repeated capability remains `AVAILABLE` if ammunition and other authority checks pass.
- **Engine enforcement:** forged explicit `FIRE/INDIRECT` does not encounter a planned-cooldown blocker. Two missions can be created consecutively.
- **Replay:** strict replay reproduces the two missions; this is deterministic but violates the configured authority.
- **Mission separation:** the currently missing cooldown cannot be accidentally described as mission state. A repair must bind plan availability to the shooter (or an explicitly keyed shooter runtime), independently of FLYING/ACTIVE/RESOLVED mission phase.

P1: `INDIRECT_FIRE_PLANNED_COOLDOWN_NOT_BOUND_TO_OPERATOR`.

## 7. Weapon Cooldown

`WEAPON_COOLDOWN_MODEL: RULE AMBIGUITY`

The implemented mechanics are coherent: committed Direct/Guided fire sets an Operator's `weapon_cooldown_remaining_ms`; MovementRuntime decrements it by authoritative milliseconds and clamps to zero; Direct/Guided validation and capability generation reject while nonzero; Indirect capability also uses it as a generic blocker; snapshot and replay preserve/recompute it.

The runtime is Operator-global, not keyed by `weapon_id`. Consequently, firing one weapon locks every weapon carried by that Operator. Some prose refers to the fired weapon, while Source compatibility exposes a singular `weapon_cool_time`; no unambiguous frozen authority was found for multi-weapon cooldown granularity. The current behavior is recorded, not declared a defect in this audit.

## 8. Suppression

`SUPPRESSION_TIMER: FAIL`

The typed timer itself is sound:

- Combat effects set `suppression_remaining_ms`.
- MovementRuntime decrements by `delta_time_ms` and clamps to zero.
- MOVE, boarding, concealment, and guided restrictions consume typed suppression; Direct Fire applies the existing suppressed-attacker modifier rather than treating suppression as an absolute prohibition.
- Snapshot/replay retain the typed value for an on-map Operator.

Two consistency defects remain:

1. **P1 – stranded movement task.** Suppression may request stop after the current hex while preserving the remaining path. At suppression expiry the state becomes STOPPED, but the path is neither resumed nor cleared. A fresh MOVE is rejected because a path still exists, while STOP_MOVE is unavailable because no segment is active.
2. **P2 – stale raw projection.** `raw_attributes["suppression_remaining_seconds"]` is written when suppression is applied but is not decremented. Native rule checks use typed milliseconds, so raw does not override authority, but friendly/source compatibility evidence can be stale.

Probe after suppression expiry:

```text
state=STOPPED, suppression_remaining_ms=0
current_hex=102, move_path=[103], moving=False
valid actions omit MOVE and STOP_MOVE
raw suppression_remaining_seconds=150
```

P1: `SUPPRESSION_EXPIRY_LEAVES_UNRESUMABLE_MOVE_TASK`.

P2: `STALE_SUPPRESSION_COMPATIBILITY_PROJECTION`.

## 9. Boarding

`BOARDING_TRANSITION_TIMER: PARTIAL`

The principal 75-second lifecycle passes: BOARD/UNLOAD create immutable transitions, use authoritative millisecond decrement, clamp at zero, enforce movement/action locks, complete deterministically, update passenger/carrier relations, and re-execute in strict replay. Exact-boundary tests pass.

Two issues were found:

1. **P1 – conditional Operator serialization loss.** `OperatorState.to_dict()` serializes movement/fatigue/weapon/suppression/transition fields only when `current_hex is not None`. BOARD intentionally removes the passenger's independent hex. The Engine's normal working-world clone/atomic commit uses `to_dict()/from_dict()`, so boarding can silently reset authoritative passenger timers and fatigue even before an external snapshot is taken.

   Probe:

   ```text
   before BOARD: fatigue=2, recovery_elapsed=10000, weapon_cooldown=100000
   after BOARD:  fatigue=0, recovery_elapsed=0, weapon_cooldown=0
   ```

2. **P2 – overshoot event timestamp.** A single 100,000 ms step completes a 75,000 ms transition correctly, but records `completed_tick`/completion event at the step end (100 seconds), not at the internal 75-second boundary. Final state is deterministic; the transition evidence is temporally coarse.

P1: `ON_BOARD_SERIALIZATION_DROPS_OPERATOR_TIMER_STATE`.

P2: `BOARDING_COMPLETION_TIMESTAMP_USES_STEP_END_ON_OVERSHOOT`.

## 10. Movement / Stop Transitions

`MOVEMENT_TRANSITION_TIME: PARTIAL`

Core movement/stop timing is a positive reference implementation:

- Explicit adjacent paths are advanced by milliseconds.
- A large delta can finish a segment and continue into following segments.
- Stop and generic transition boundaries are consumed exactly.
- Destroyed, boarded, or lifecycle-expired Operators have movement runtime cleaned by authoritative transitions.
- Capability generation and Engine planner validation share the same movement facts.

The PARTIAL rating comes from the suppression handoff described above: movement intentionally retains an unfinished task, but no expiry transition makes that task executable again. This is a cross-axis state-machine defect rather than a failure of segment arithmetic.

## 11. Limited Lifecycle

`LIMITED_LIFECYCLE: PASS`

- Versioned profiles bind `lifecycle_profile_id` and integer `lifecycle_remaining_ms`.
- The Engine segments advancement at the nearest expiry boundary and does not move an Operator past expiry.
- Exact boundary and large-tick crossing use the shared destruction pipeline and deterministic `LIFECYCLE_EXPIRED` evidence.
- Snapshot, restore, world hash, GREEN, valid actions, and strict replay preserve the lifecycle semantics.
- Loitering Munition uses 1,200,000 ms.
- Drone has both lifecycle fields `None`, which means no limited lifecycle; no fake infinite duration or expiry is introduced.

## 12. Concealment

`CONCEALMENT_STATE: PARTIAL`

Concealment is correctly treated as persistent posture, not a timer or lifecycle state. MOVE and active fire expose the roles frozen by existing rules; detection and suppression do not automatically rewrite posture. Engine capability/validation and replay follow the action stream.

The typed `concealment_posture` is mirrored to raw `concealed`, but `OperatorState.to_dict()` does not explicitly serialize the typed posture. Restore reconstructs it from raw compatibility data. Normal Engine setters keep both synchronized, so no current adjudication divergence was reproduced; nevertheless, the authority boundary is effectively typed→raw→typed rather than a strict typed serialization with a one-way raw projection.

P2: `CONCEALMENT_TYPED_POSTURE_SERIALIZES_VIA_RAW_COMPATIBILITY`.

## 13. Snapshot / Restore / Replay

### 13.1 Snapshot/restore

`SNAPSHOT_RESTORE_TIMER_CONSISTENCY: FAIL`

Guided recovery, lifecycle fields, boarding transitions, indirect missions, and on-map movement/cooldown/suppression values are represented in snapshots and restore deterministically. Exact hash equality was observed for representative Guided and Indirect snapshots.

The overall result is FAIL because authoritative Operator fields are conditionally omitted when `current_hex is None`; boarding demonstrates actual state loss through the same serialization boundary used by working-world clone/commit. A correct snapshot cannot be claimed when valid typed state can be reset solely because the Operator is on board/off-map.

### 13.2 Strict Replay

`TIMER_STRICT_REPLAY: PARTIAL`

Replay does not inject final cooldown/lifecycle values. It records the initial scenario, ordered actions, seed/ruleset evidence, and each exact `delta_time_ms`, then calls the Engine again and compares snapshots/events/hashes. Guided permanent recovery and consecutive Indirect mission creation were both reproduced exactly in a strict replay probe.

This is a correct replay mechanism but only a PARTIAL lifecycle result: replay faithfully reproduces the missing Guided countdown, absent Indirect planned cooldown, and indirect phase-boundary loss.

## 14. Capability / valid_actions Coupling

| State | Engine reject/enforce | Native valid actions | Human | Agent | Observation/next frame | Consistent? |
|---|---|---|---|---|---|---|
| Destroyed/not alive | yes | removed | removed | removed | inactive/dead truth | yes |
| Undeployed/on board | yes | passenger not independently actionable | projected from Engine | frozen actions from Engine | relation/state refreshed | yes, except serialization loss |
| Moving | action-specific validation | MOVE/STOP/fire rules consume runtime | projected | projected | position refresh | yes |
| Stopping/transition | common validation blocks incompatible action | filtered | projected | projected | timer visible only through available actions/raw product fields | yes |
| Marching/charge | movement/fire validation consumes mode | filtered | projected | projected | state refreshed | yes |
| Suppressed | typed timer/state enforced | filtered or modifier-driven by action | projected | projected | typed semantics not explicitly normalized | partial: expiry can strand path |
| Concealed | posture/action rules enforced | ENTER_CONCEALMENT hidden when inapplicable | projected | projected | posture persists; detection separate | yes |
| Boarding transition/lock | yes | filtered | projected | projected | relations refreshed | yes |
| Weapon cooldown | yes for fire paths | fire filtered | projected | projected | source product can display typed cooldown | yes, subject to granularity ambiguity |
| Guided recovery | forged request rejected | Guided capability absent | absent | absent | value persists | internally consistent but permanently locked |
| Indirect planned cooldown | **no state/no rejection** | capability remains available | available | available | no cooldown to project | **no** |
| Lifecycle expired | dead authority | no active actions | removed | removed | GREEN/side frames update | yes |

`CAPABILITY_ENGINE_AUTHORITY_CONSISTENCY: FAIL`

Guided recovery is enforced at both authority and projection layers, although it never expires. Indirect planned cooldown is absent at every runtime layer, so a forged or normal repeated action can create another mission immediately. Engine remains the entry point, but the configured cooldown is not part of its authority.

## 15. Typed vs Raw State Consistency

| Semantic | Authoritative field | Raw/compatibility field | Write direction | Read direction for rules | Can raw become stale? | Risk |
|---|---|---|---|---|---|---|
| Concealment | `concealment_posture` | `concealed` | typed setter mirrors raw | restore/import may reconstruct typed from raw | possible if external/raw mutation bypasses setter | P2 mixed authority boundary |
| Suppression | `suppression_remaining_ms` | `suppression_remaining_seconds` | effect writes both initially | Native rules read typed | yes; raw does not decrement | P2 stale evidence |
| Weapon cooldown | typed ms | Source `weapon_cool_time` projection | typed→Source projection | Native rules read typed | projection refreshed; nested old raw may remain | low/P2 presentation risk |
| Ammunition | raw `ammunition` mapping | same | source/config import and committed resource update | fire rules read explicit mapped key | not a typed/raw duplicate | current raw authority by design |
| Observation contacts | detached observation/refresh result | `see_enemy_bop_ids` | observation refresh writes compatibility | visibility/capability uses controlled paths | historical raw can be stale before refresh | derived, not lifecycle authority |
| Boarding | typed relation/transition | possible historical source relation metadata | typed runtime independent | Native rules read typed | raw relation evidence can differ | low if kept compatibility-only |
| Guidance relation | typed profile/affiliation/evidence | legacy source fields | source import may supply explicit supplement only | Native rules read typed | legacy raw remains evidence only | acceptable fail-closed design |

`TYPED_RAW_CONSISTENCY: PARTIAL`

No raw suppression value was found overriding typed Native adjudication. The principal risks are stale compatibility evidence and concealment's reliance on raw serialization for reconstructing the typed posture.

## 16. Config → Runtime Traceability

| Authority/config rule | Runtime binding | Set | Authoritative advance | Consumer | Verdict |
|---|---|---|---|---|---|
| Guided profile recovery 75s | `guided_fire_recovery_remaining_ms` | yes | **missing** | capability + request validation | FAIL |
| Indirect planned cooldown 300s | **none** | no | no | none | FAIL |
| Direct/Guided weapon cooldown | Operator-global weapon timer | yes | MovementRuntime ms decrement | fire validation/capability | PASS mechanics / granularity UNKNOWN |
| Suppression duration | typed suppression timer | yes | MovementRuntime ms decrement | movement/board/conceal/guided; Direct modifier | PARTIAL due post-expiry task and stale raw |
| Boarding duration 75s | World boarding transition | yes | Engine ms decrement | board/unload/common action locks | PASS final state / PARTIAL timestamp and serialization |
| Stop/posture/mode transition duration | Operator transition timers | yes | MovementRuntime boundary loop | movement/common/posture validation | PASS |
| Fatigue recovery duration | elapsed recovery accumulator | yes | MovementRuntime boundary loop | movement/readiness | PASS |
| Loitering lifecycle 1200s | lifecycle profile + remaining ms | yes | outer nearest-boundary loop | survival/capability | PASS |
| Indirect flight 150s | mission seconds + remainder | yes | mission advance | phase/impact | PARTIAL: overshoot loss |
| Indirect active duration 300s | mission seconds + remainder | yes | active advance | area resolution/expiry | PARTIAL: same-second loss |

## 17. Orphan / Dead State Findings

| Field/config | Written at | Read at | Advanced at | Capability consumer | Verdict |
|---|---|---|---|---|---|
| `guided_fire_recovery_remaining_ms` | guided commit | guided capability/validation, serialization | nowhere | yes | P1 permanent lock |
| Indirect `planned_cooldown_seconds` | profile/config only | loader/audit | no runtime | none | P1 config-runtime gap |
| raw suppression seconds | suppression effect | compatibility/presentation only | nowhere | no Native consumer | P2 stale compatibility state |
| preserved `move_path` after suppression stop | movement runtime | planner detects existing task | no resume after expiry | indirectly blocks MOVE | P1 orphaned task |

No other `*_remaining_ms`, cooldown, or recovery field was found both authoritative and wholly consumer-free.

## 18. Timer Boundary Semantics and Event Ordering

| Timer | `remaining > delta` | `remaining == delta` | `remaining < delta` | Event/audit behavior |
|---|---|---|---|---|
| Movement segment/stop/generic transition | decrements | completes at boundary | consumes remainder through subsequent boundaries | deterministic completion events |
| Weapon cooldown | decrements | zero at end of step | clamps zero | no ready event; state is auditable |
| Guided recovery | unchanged | unchanged | unchanged | no completion possible |
| Suppression | decrements | clears at end | clamps/clears | no explicit clear event; stranded path defect |
| Boarding | decrements | completes | completes, but overshoot timestamp is step end | deterministic but temporally coarse |
| Limited lifecycle | decrements | expires exactly | Engine segments at expiry then advances remainder without dead Operator | deterministic destruction event at boundary |
| Indirect FLYING/ACTIVE | decrements in mixed units | transitions | **does not carry all overshoot across phase** | transition timestamp/remaining time can be late |

Not every ready/clear transition needs a dedicated event, but state transitions must remain reconstructable. Cooldown/suppression/guided-ready events are currently absent; this is an observability matter after the authority defects are repaired, not a reason to alter adjudication in this audit.

## 19. Findings

### P0

NONE. No wall-clock adjudication, nondeterministic timer mutation, authoritative-world corruption, or Replay final-state substitution was found.

### P1

1. `GUIDED_FIRE_RECOVERY_TIMER_NOT_ADVANCED` — committed recovery is enforced forever.
2. `INDIRECT_FIRE_PLANNED_COOLDOWN_NOT_BOUND_TO_OPERATOR` — the configured 300-second cooldown has no runtime or authority enforcement.
3. `ON_BOARD_SERIALIZATION_DROPS_OPERATOR_TIMER_STATE` — setting passenger `current_hex=None` causes conditional serialization to reset authoritative fields during clone/commit/snapshot.
4. `SUPPRESSION_EXPIRY_LEAVES_UNRESUMABLE_MOVE_TASK` — expiry leaves a stopped Operator with a path that cannot resume and blocks a new MOVE.
5. `INDIRECT_FIRE_PHASE_BOUNDARY_OVERSHOOT_NOT_ACCOUNTED` — elapsed time can be lost across FLYING→ACTIVE and within the first sub-second ACTIVE interval.

### P2

1. `STALE_SUPPRESSION_COMPATIBILITY_PROJECTION` — raw seconds do not follow typed countdown.
2. `BOARDING_COMPLETION_TIMESTAMP_USES_STEP_END_ON_OVERSHOOT` — end state is correct but completion evidence is late for a large tick.
3. `CONCEALMENT_TYPED_POSTURE_SERIALIZES_VIA_RAW_COMPATIBILITY` — typed posture lacks a first-class serialization field.
4. Timer-ready/clear transitions (weapon cooldown, Guided recovery, suppression) have uneven event evidence; state remains the current source of audit truth.
5. Orthogonal booleans/timers and the general `state` string have no single cross-field invariant validator, increasing inconsistent imported-state risk.

### P3

1. Some Direct-fire structures use names such as `cooldown_ticks` for values whose effective rule unit is seconds before conversion.
2. Indirect missions mix whole seconds with millisecond remainder fields, making boundary reasoning and proof more complex than the Operator millisecond timers.

## 20. Recommended Repair Order

Only minimal repairs are recommended; no generic scheduler or timer-framework rewrite is justified.

1. P1-1: connect Guided recovery to the existing authoritative Operator-time advancement and lock exact/large-boundary behavior with authority-level tests.
2. P1-2: add the smallest versioned shooter planned-cooldown runtime for Indirect fire, with capability and Engine request validation consuming the same field.
3. Remove conditional loss of authoritative Operator fields for on-board/off-map serialization; preserve historical compatibility through explicit defaults/version handling.
4. Freeze and implement the existing suppression rule's post-expiry movement-task outcome (resume or otherwise according to authority; do not guess).
5. Segment Indirect phase advancement so elapsed milliseconds are not discarded across FLYING/ACTIVE boundaries.
6. Handle P2 projections/evidence separately after authoritative behavior is fixed.

## 21. Readiness for OperatorLifecycleView

`LIFECYCLE_VIEW_READINESS: READY_AFTER_P1_FIX`

The engine already has sufficiently orthogonal typed axes and a common millisecond clock to support a read-only semantic view. It is not READY now because a view would faithfully project a permanent Guided lock, nonexistent Indirect plan cooldown, a stranded suppressed movement task, and potentially lost on-board timer state.

After the P1 repairs, a view should derive—not own—the following fields:

- `alive`, `deployed`, `controllable`
- semantic movement state, movement mode, active segment, stop request/remaining time
- generic transition kind/remaining time
- fatigue level/recovery elapsed
- suppressed flag and typed suppression remaining ms
- concealment posture
- boarding state, carrier/passenger relation, transition remaining ms, movement lock
- weapon locked/unfolded, weapon ready, current Operator-global cooldown ms (or later frozen granularity)
- guided-fire ready and guided recovery remaining ms
- indirect-plan ready and planned cooldown remaining ms after authority binding
- limited-lifecycle enabled/profile/remaining ms
- current Engine-generated capabilities and blocker reason codes

The view must not become a second authority, decrement timers, inspect stale raw compatibility values, or manufacture valid actions.

## 22. Tests and Read-only Probes

No repository test was added or modified.

Targeted existing suite:

```text
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src pytest -p no:cacheprovider -q \
  tests/test_guided_capability.py \
  tests/test_guided_fire_validation.py \
  tests/test_guided_fire_resolution.py \
  tests/test_guided_fire_commit.py \
  tests/test_guided_fire_pipeline.py \
  tests/test_indirect_fire_capability.py \
  tests/test_indirect_fire_plan_request.py \
  tests/test_indirect_fire_mission.py \
  tests/test_indirect_fire_action_replay.py \
  tests/test_indirect_fire_active_area.py \
  tests/test_indirect_fire_commit.py \
  tests/test_boarding_contract.py \
  tests/test_boarding_lifecycle.py \
  tests/test_source_movement.py \
  tests/test_joint_tick_semantics.py \
  tests/test_loitering_munition.py \
  tests/test_drone_operator.py \
  tests/test_concealment_action.py \
  tests/test_replay.py \
  tests/test_source_replay_recorder.py \
  tests/test_human_command_capabilities.py \
  tests/test_guided_fire_ui_contract.py \
  tests/test_boarding_ui_contract.py \
  tests/test_boarding_agent.py
```

Result: **332 passed, 0 failed, 65.83s**.

Temporary `/tmp` probes (not retained in the repository) covered:

- Guided set, sub-boundary, exact/overshoot, forged action, snapshot/restore, strict replay.
- Consecutive Indirect mission creation and strict replay.
- Indirect FLYING/ACTIVE large-tick and sub-second boundary behavior.
- Suppression stop/path/expiry capability behavior.
- Boarding timer-state preservation and overshoot event timestamp.
- Lifecycle/Replay behavior through existing Loitering Munition and Drone tests.

## 23. Audit Conclusion

The Native Engine has a credible shared authoritative-time foundation, but its time-driven Operator semantics are not yet closed end to end. Configuration, field existence, UI omission, snapshot persistence, and deterministic Replay were deliberately not accepted as substitutes for Set→Advance→Complete→Engine Validate→Capability→Replay traceability.

`OPERATOR_LIFECYCLE_SEMANTICS_AUDIT_REQUIRES_REPAIR`

Exact P1 set:

- `GUIDED_FIRE_RECOVERY_TIMER_NOT_ADVANCED`
- `INDIRECT_FIRE_PLANNED_COOLDOWN_NOT_BOUND_TO_OPERATOR`
- `ON_BOARD_SERIALIZATION_DROPS_OPERATOR_TIMER_STATE`
- `SUPPRESSION_EXPIRY_LEAVES_UNRESUMABLE_MOVE_TASK`
- `INDIRECT_FIRE_PHASE_BOUNDARY_OVERSHOOT_NOT_ACCOUNTED`

## Repair Round A Verification

Repair scope: authoritative Guided recovery, per-shooter Indirect planned
cooldown, and residual-delta Indirect mission phase advancement.  The original
audit findings above remain the before-state evidence; this section records
the verified current state after the bounded repair.

### GUIDED_FIRE_RECOVERY_TIMER_NOT_ADVANCED

Before:

- A committed Guided attack set both shooter and guider to 75,000 ms.
- Capability and Engine request validation enforced the field.
- No authoritative-time path decremented it, creating a permanent lock.

After:

- The existing `guided_fire_recovery_remaining_ms` field remains the authority.
- Guided effect staging still produces exactly 75,000 ms for both roles.
- `MovementRuntime._advance_duration()` now consumes the field with the same
  integer `delta_time_ms` and clamp-to-zero behavior as weapon cooldown.
- The action step itself consumes its elapsed duration because the established
  Engine order is action commit followed by time advance.  Thus a 1 ms Guided
  step ends at 74,999 ms; 74,999 further milliseconds reaches ready exactly.
- Recovery greater than zero remains enforced by both current capability
  generation and forged-request validation.  A rejected request does not reset
  recovery or consume RNG.
- Snapshot/restore and strict replay reproduce partial, exact, and overshoot
  progression.

Result: **CLOSED**.

### INDIRECT_FIRE_PLANNED_COOLDOWN_NOT_BOUND_TO_OPERATOR

Before:

- Versioned authority declared 300 seconds.
- No shooter runtime was set, advanced, serialized, or consumed.
- One shooter could create consecutive missions immediately.

After:

- `OperatorState.indirect_fire_planned_cooldown_remaining_ms` is the typed,
  per-operator authority with a compatibility default of zero.
- Mission creation obtains the duration only from the matched Indirect profile
  and sets 300,000 ms in the same working-world transaction that consumes
  ammunition and appends the mission.
- `MovementRuntime._advance_duration()` consumes the timer using integer
  authoritative milliseconds and clamps at zero.
- The capability blocker is
  `indirect_fire_planned_cooldown_remaining`; explicit action-stream attempts
  receive `INDIRECT_FIRE_PLANNED_COOLDOWN_ACTIVE`.
- Plan-request revalidation also sees the recomputed blocked capability, so
  trusted/stale request paths cannot bypass cooldown.
- The cooldown is independent of FLYING/ACTIVE/RESOLVED mission phase and is
  scoped to the shooter; another shooter remains available.
- Serialization emits the new field only while nonzero.  Old scenario/operator
  records omit it and restore as ready (`0`), avoiding meaningless zero-field
  changes to historical Direct world hashes.
- Snapshot/restore at 1 ms and strict action-stream replay pass.

Result: **CLOSED**.

### INDIRECT_FIRE_PHASE_BOUNDARY_OVERSHOOT_NOT_ACCOUNTED

Before:

- FLYING consumed the full delta only up to its phase boundary.
- Residual time after FLYING→ACTIVE was discarded.
- A seconds-based `active_started_tick < current_tick` guard could also discard
  the first ACTIVE sub-second interval.

After:

- `indirect_fire_active_delta_ms()` derives, for each deterministically ordered
  mission, how much of this Engine step belongs to ACTIVE:
  - already ACTIVE: the whole delta;
  - PLANNED/FLYING crossing its boundary: `delta - exact flight remainder`;
  - no crossing: zero.
- This per-step derived tuple is not WorldState, Replay metadata, or a second
  clock.  It is passed to the existing ACTIVE advance after impact/area
  initialization, so the current FireMission seconds-plus-ms schema remains
  unchanged.
- One step can now cross FLYING→ACTIVE and, when sufficiently large,
  ACTIVE→RESOLVED.  A 500 ms residual is retained as exactly 500 ms.
- Repeated runs and strict replay produce identical World, RNG, mission, and
  event trajectories.

Result: **CLOSED**.

### Transaction and compatibility verification

- Same-pre-state validation/staging order is unchanged.
- Mission creation, ammunition consumption, and shooter cooldown assignment
  are one working-world mutation under the existing outer atomic transaction.
- Rejected Guided/Indirect requests do not consume attack RNG or resources;
  their elapsed step still advances existing timers as authoritative time.
- Human and Agent continue to consume Engine-derived capability status.  Their
  existing `AVAILABLE` filters automatically remove blocked Indirect entries;
  no UI/Agent timer authority was added.
- FireMission schema, Replay snapshot step identity, `world.tick`, Loitering
  lifecycle, Drone no-lifecycle, DirectFireAdjudicator, GuidedFireResolver, and
  action schemas were not changed.
- Targeted repair/regression suites: 45 passed; Guided/Indirect suite: 220
  passed; related cross-capability suite: 286 passed, 1 skipped.
- Full suite: **1709 passed, 10 skipped, 0 failed in 316.98s**.

### Remaining lifecycle audit P1s

The overall lifecycle audit is not closed.  These intentionally out-of-scope
findings remain:

- `ON_BOARD_SERIALIZATION_DROPS_OPERATOR_TIMER_STATE`
- `SUPPRESSION_EXPIRY_LEAVES_UNRESUMABLE_MOVE_TASK`

`ROUND_A_AUTHORITATIVE_TIMERS_CLOSED`

## Repair Round B Verification

Repair scope: serialization integrity for authoritative `OperatorState` fields
when an Operator has no independent map position.  Boarding rules, timer
advancement, suppression path semantics, Replay schema, and Source projection
were not changed.

### ON_BOARD_SERIALIZATION_DROPS_OPERATOR_TIMER_STATE

Before:

- `OperatorState.to_dict()` placed movement runtime, fatigue, generic
  transition, weapon posture/cooldown, and suppression fields inside a single
  `current_hex is not None` gate.
- `current_hex=None` correctly represented a mounted/off-map spatial state but
  incorrectly made non-spatial typed authority ineligible for serialization.
- The defect affected live adjudication as well as persistence:
  `NativeEngine.step()` creates its detached pre-state/working world through
  `WorldState.to_dict()` → `WorldState.from_dict()`, and `_commit_world()`
  validates another detached serialization round-trip before committing.
- A successful BOARD therefore could set `current_hex=None` and subsequently
  reset non-default passenger runtime to constructor defaults during the same
  commit, a later step clone, snapshot/restore, or strict replay.

Root cause:

- Spatial presence and authoritative-state serialization eligibility were
  coupled in the Operator serializer.  `from_dict()` already had typed defaults
  for every affected field; the loss was solely at the `to_dict()` gate.

Repair:

- All Operators now use one typed runtime-state field contract.
- On-map Operators retain the exact historical wire shape, including the
  existing explicit default-valued runtime fields.
- Operators with `current_hex=None` omit only fields that equal their semantic
  constructor defaults.  Every non-default authoritative runtime value is
  serialized independently of position.
- `current_hex` itself remains omitted while absent.  No `ON_BOARD` branch,
  Boarding serializer, new clock, Replay metadata, or operator-type special
  case was added.
- Existing separately serialized guidance, per-shooter Indirect cooldown,
  Boarding relation, and limited-lifecycle fields remain under their prior
  semantic/default omission rules.

Preserved typed state for an off-map/mounted Operator now includes:

- basic speed, sub-hex progress, path/movement mode, stop request, movement
  action/segment evidence identifiers, and movement blocker;
- fatigue level and recovery elapsed milliseconds;
- generic transition kind and remaining milliseconds;
- weapon lock/unfold posture and weapon cooldown;
- typed suppression remaining milliseconds;
- Guided recovery and Indirect planned cooldown (through their existing
  independent nonzero serialization contracts);
- Boarding relation and movement lock;
- concealment through the existing typed-to-raw compatibility projection;
- generic limited-lifecycle profile and remaining milliseconds.

After:

- A maximal `current_hex=None` Operator round-trip retains every non-default
  authoritative typed field, including suppression and transition fields that
  cannot legally accompany a new BOARD request.
- A default-valued off-map Operator retains the previous omission shape; no
  meaningless `null`/zero fields are emitted.
- A real BOARD commit preserves passenger fatigue, weapon cooldown, Guided
  recovery, and Indirect planned cooldown while their values continue to
  advance by the existing authoritative `delta_time_ms` semantics.
- Snapshot/restore at ON_BOARD state is exact.  The next restored Engine step
  continues each timer rather than reinitializing it.
- A complete BOARD → ON_BOARD → UNLOAD → ACTIVE round-trip preserves state
  except for the expected 150,000 ms of authoritative timer/fatigue advance.
- The passenger remains absent from valid actions while ON_BOARD; capability
  exclusion does not delete its internal authority.
- A newly recorded Boarding run with non-default passenger runtime strictly
  re-executes from scenario plus ordered actions and exact deltas.

### Compatibility and test evidence

- Direct targeted serialization/Boarding suite: **28 passed**.
- Round A timer plus serialization/Boarding suite: **73 passed**.
- Guided, Indirect, Boarding, Human/Agent, Replay, Loitering Munition, Drone,
  and Operator-state related suite: **237 passed**.
- Full suite: **1714 passed, 10 skipped, 0 failed in 322.31s**.
- Existing historical on-map wire/hash semantics remain unchanged.  Existing
  default off-map records also retain their omission shape and restore with
  prior defaults.  Hashes change only for states that previously discarded a
  meaningful non-default off-map value; that is the corrected authoritative
  state rather than serialization noise.
- `git diff --check`: PASS.

Result: **CLOSED**.

### Remaining lifecycle audit P1

The serialization repair does not close the overall lifecycle audit.  The
following intentionally out-of-scope rule-semantic finding remains unchanged:

- `SUPPRESSION_EXPIRY_LEAVES_UNRESUMABLE_MOVE_TASK`

`ROUND_B_OPERATOR_SERIALIZATION_CLOSED`

## Repair Round C Verification

Repair scope: close the suppression-to-movement-task lifecycle gap without
changing suppression duration, movement geometry, same-pre-state adjudication,
action schemas, or raw Source-compatibility suppression evidence.

### Authority evidence and frozen rule

The Rule Gate passed.  The repository contains explicit authority rather than
requiring a new gameplay choice:

- `docs/rules/推演规则_完整纯文本_Final.md` section 1.4(d) states that personnel
  suppressed while moving or charging finish the current hex, stop, and, after
  the 150-second suppression expires, continue the unfinished movement or
  charge command.
- Section 1.3 establishes that an accepted movement command is an ordered path
  whose unexecuted portion is cancelled by an explicit stop-movement command.
- Section 3.1 establishes that suppressed personnel cannot move or fire and
  that suppression lasts 150 seconds.
- `config/source_compat/source_movement_policy_v1.json` independently freezes
  the 150-second suppression duration and center-to-center path-completion
  convention.

The resulting Native simplified rule is therefore **RESUME**, not CANCEL:

1. a moving personnel Operator that becomes suppressed completes its current
   segment/hex;
2. its exact remaining authoritative movement task is explicitly paused;
3. the existing stop transition completes and the Operator remains stopped
   while suppression is active;
4. when both suppression and the stop transition have completed, the exact
   unfinished path resumes automatically;
5. an explicitly submitted `STOP_MOVE` still cancels the remaining task and
   takes precedence over suppression-driven pause;
6. suppression applied to an already stationary Operator creates no movement
   task.

### Root cause

`move_path` is the Engine-owned queue of unexecuted cells, not a presentation
copy of the user's request and not a separate history record.  Before repair,
suppression reused `movement_stop_requested`, then preserved `move_path` while
finishing the current segment.  Suppression expiry cleared the typed timer but
did not restart or cancel that queue.  The planner rejected a new `MOVE`
because the queue still existed, while `STOP_MOVE` was unavailable because no
segment was active.  This produced a live, controllable, non-suppressed
Operator with an implicit and unresumable movement task.

### Repair

- Added the small typed movement-task pause reason `SUPPRESSION`.  It is
  Operator authority, serializes independently of position, restores through
  snapshots/clones, and is omitted when absent to preserve existing wire/hash
  shape.
- Suppression effects bind that reason only for a currently moving personnel
  Operator.  The movement runtime also recognizes an imported/initial moving
  suppressed state at its first authoritative advance.
- Arrival at the current segment preserves the path only for the explicit
  suppression pause.  Explicit `STOP_MOVE` clears both the path and pause
  reason.
- Suppression expiry is an authoritative time boundary.  Once suppression and
  the existing stop transition are both zero, the existing segment-start path
  resumes the next cell and consumes any residual `delta_time_ms` in the same
  Engine step.
- Destruction clears the pause reason through the existing destruction cleanup.
- No suppression-specific movement action, controller loop, UI/Agent schema,
  new event type, or new clock was introduced.  Resume emits the existing
  deterministic `MOVEMENT_STARTED` fact.

### Boundary semantics

- Exact expiry (`remaining == delta`) resumes at that boundary if the stop
  transition is already complete.
- Overshoot consumes time to the expiry boundary, resumes, and applies the
  residual delta to the next segment.  If the stop transition is still active,
  the pause remains until that later boundary.
- Expiry before current-segment arrival does not bypass the required
  finish-current-hex and stop-transition sequence.
- Suppression at the exact segment boundary deterministically records
  suppression, arrival, and stopping before the paused task can resume.
- A stationary suppressed Operator remains pathless and can submit a fresh
  `MOVE` after expiry.

### Capability, Human, and Agent verification

- During the paused/suppressed interval, Engine validation rejects a forged
  conflicting `MOVE` with the existing active-task blocker, and native valid
  actions expose neither `MOVE` nor `STOP_MOVE` for that stale-control state.
- After the original task resumes and completes, a new `MOVE` is accepted.
- Human validation uses `ProductOperationService` capability projection,
  preview, `RealtimeGateway`, and Engine commit; no client-side suppression or
  path rule was added.
- Agent validation uses a detached `SideObservation`, frozen native valid
  actions, `SeatOperation`, session submission, and Engine commit; it does not
  access mutable `WorldState`.

### Snapshot, replay, and regression evidence

- Snapshot/restore with `suppression_remaining_ms=1` and an explicit paused
  path produces the same resumed path, segment progress, final state, and
  capabilities as uninterrupted execution.
- Strict Replay re-executes actual `MOVE` plus Direct Fire suppression with
  exact delta sequences, including both segmented boundaries and one large
  overshooting step.  World hash, GREEN frame, RNG, event trace, final path,
  and suppression state match.
- Explicit-stop cancellation and stationary suppression are covered separately.
- Targeted suppression/movement/interface suite: **85 passed**.
- Core movement/fire/world regression suite: **144 passed**.
- Guided/Indirect/Boarding/Human/Agent/Replay/Loitering/Drone related suite:
  **267 passed**.
- Full suite: **1724 passed, 10 skipped, 0 failed in 254.85s**.

The deliberately out-of-scope P2
`STALE_SUPPRESSION_COMPATIBILITY_PROJECTION` remains unchanged; typed
`suppression_remaining_ms` continues to be the only Native authority.

Result: **CLOSED**.

`ROUND_C_SUPPRESSION_MOVEMENT_CLOSED`

No P0/P1 finding remains in the Operator Lifecycle & Cooldown Consistency
Audit.  `OperatorLifecycleView` readiness is now **READY**.

`OPERATOR_LIFECYCLE_SEMANTICS_CLOSED`

`OPERATOR_LIFECYCLE_VIEW_READINESS_READY`
