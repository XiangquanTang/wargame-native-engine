# Concealment / Exposure State Audit v1

Status: **AUDIT / CONTRACT PROPOSAL — NOT IMPLEMENTED**
Scope: Native simplified rules only
Date: 2026-08-25

This document separates three things which must not be conflated:

1. **rule evidence** in the curated repository knowledge base;
2. **current executable behavior** in `wargame_native_engine`;
3. **missing authority contracts** required before adding concealment transitions.

No statement in this document promotes an unresolved Source compatibility field
to Native authority. In particular, Source `keep`, unit names, subtypes, terrain
names, and historical state deltas must not be used to infer concealment.

## 1. 规则知识库证据

### 1.1 通用隐蔽姿态

The primary human-readable evidence is
`docs/rules/推演规则_完整纯文本_Final.md`:

| Evidence | Confirmed machine meaning |
|---|---|
| lines 83–91 | Entering concealment takes 75 seconds. Other commands are unavailable during the transition. |
| line 85 | Tank fire during an enter-concealment transition immediately interrupts the transition. |
| line 86 | Movement or firing by a concealed unit removes concealment immediately, without another timed transition. |
| line 87 | A guider performing guided fire remains concealed; the guided shooter exits concealment. |
| lines 88–90 | A suppressed unit cannot start concealment; suppression interrupts an active concealment transition; suppression does not remove an already completed concealment posture. |
| line 91 | Concealment, normal movement, charge movement and half-speed movement are mutually exclusive modes. |
| line 225 | Same-hex close combat interrupts an enter-concealment transition, but does not remove already completed concealment. |
| line 235 | Loading/unloading cannot be combined with movement, fire or entering concealment. |

`config/adjudication/source_tables_v1/native_adjudication_tables_final.json`
confirms `concealment_transition_seconds = 75`. This JSON remains the sole
machine adjudication-table source; prose explains semantics but is not itself a
runtime table.

### 1.2 Observation and terrain

The complete rules document lines 103–121 and
`docs/rules/Native兵棋裁决知识库_Final.md` lines 183–217 establish:

- LOS is a center-to-center hex ray with intermediate elevation blocking;
- settlement/forest adds one elevation level when it is an intermediate cell;
- adjacent hexes are always in LOS;
- observation distance comes from an observer-domain/target-domain matrix;
- target concealment halves observation distance;
- a target in settlement/forest independently halves observation distance;
- vehicle concealment is ignored when the vehicle is lower than its observer;
- LOS evidence and distance/modifier evidence are separate outputs.

The machine table repeats the concealment observation multiplier (`0.5`) and
the higher-observer vehicle exception. Terrain does **not** automatically mean
that a unit has entered the voluntary concealed posture; it is an independent
observation modifier.

### 1.3 Fire effects

The complete rules document lines 140–150 establishes that direct fire requires
the attacker to observe its target. It also specifies target-state and terrain
modifiers. The machine table contains a discrete `concealed = -2` correction for
vehicle, personnel and indirect-fire result tables. The implementation may use
only the curated table values; this audit does not introduce continuous damage
or inferred corrections.

Guided-fire evidence at lines 210–218 explicitly separates acquisition from
launch: the guider must observe the target; the shooter need not have LOS. The
guider stays concealed while guiding, while the shooter exits concealment when
it fires.

The prose indirect-fire rules describe no/hex/target observation levels.
However, the current versioned Native simplified policy in
`config/adjudication/indirect_fire_observation_policy_v1.json` is explicitly
`UNOBSERVED` and does not require an observer. That current executable policy
must not be silently replaced by the broader prose rule.

### 1.4 Fortification concealment is a separate contract

The complete rules document lines 292–314 describes combat fortifications and
personnel concealment fortifications. These rules include total non-observation,
different firing permissions, capacity, timed entry/exit and forced exposure.
They are **not equivalent** to the generic concealment posture and are not part
of concealment v1. A later fortification contract must model them independently.

### 1.5 Unresolved source evidence

The machine table labels Source `keep` only as a candidate field for
suppression/concealment and says it must be mapped explicitly. Therefore:

- `keep` → concealment: **UNKNOWN / FORBIDDEN TO INFER**;
- name/subtype → concealment: **UNKNOWN / FORBIDDEN TO INFER**;
- low ground or forest → voluntary posture: **FORBIDDEN TO INFER**;
- historical state changes without an action → concealment transition:
  **FORBIDDEN TO INFER**.

## 2. 当前代码实现

### 2.1 Current fields

`src/wargame_native_engine/core/operator.py::OperatorState` has no typed
`concealment`, `hidden`, `camouflaged`, `stealth`, `observed` or `detected`
field. The only currently executable posture input is:

```text
operator.raw_attributes["concealed"]: bool
```

It is serialized as raw operator data and therefore participates in snapshots
and world hashes. It is not backed by a versioned authority mapping, transition
timer, operation, action or event. Current scenarios can set it explicitly;
Source compatibility conversion must not infer it from `keep`.

The repository has no general `visibility_modifier` field on an operator.
Terrain and concealment modifiers are calculated by the visibility evaluator.

### 2.2 Detection relation

The current Source-compatible refresh path is:

```text
NativeEngine._refresh_source_observation()
  -> observers sorted by operator_id
  -> targets sorted by operator_id
  -> evaluate_visibility(...)
  -> observer.raw_attributes["see_enemy_bop_ids"] = visible target ids
NativeEngine._visible_enemies(side)
  -> union of friendly observer lists
NativeEngine.observe(side)
  -> detached SideObservation
```

Thus detection is a **current observer-to-target relation**, with a side view
formed as a union. It is not a global `target.detected = true` state. The cache
is recomputed on refresh; there is no contact memory, confidence or forgetting
timer.

`NativeEngine.observe()` does not include visible-enemy raw attributes. A RED or
BLUE consumer therefore cannot use the projection to recover the enemy's hidden
posture, raw relationship IDs or GREEN evidence.

### 2.3 Visibility evaluator inputs

`src/wargame_native_engine/visibility/observation.py::evaluate_visibility`
receives:

| Input | Current source |
|---|---|
| map/hex geometry | `HexMapProfile` |
| LOS and elevation/terrain blocking | `evaluate_line_of_sight` or supplied runtime LOS evidence |
| distance | map hex distance |
| observer and target domains | operator domains |
| observation profile | `ObservationProfiles` |
| target concealment | `bool(target.raw_attributes.get("concealed", False))` |
| target terrain | current target hex terrain |
| drone-role exception | observer attributes/profile inputs |

It returns LOS evidence, base/adjusted limits, modifier evidence, observable and
reason. Forest/settlement affects both intermediate LOS blocking and target-cell
observation range where applicable.

`src/wargame_native_engine/systems/visibility.py` is the legacy simple-distance
path. The Source-compatible engine path above is the relevant implementation;
the legacy native-default ruleset explicitly keeps occlusion, elevation and
concealment disabled. These two modes must be named rather than described as one
universal visibility implementation.

### 2.4 Current transition gap

Repository search finds reads of `raw_attributes["concealed"]`, but no runtime
write that starts, completes or breaks concealment. There is no:

- concealment capability or operation;
- typed transition and 75-second timer;
- concealment lifecycle event;
- MOVE/FIRE automatic exposure update;
- suppression/close-combat transition interruption.

Consequently, current behavior implements the observation and fire effects of
an **already declared** concealment flag, not the concealment state machine in
the rule evidence.

## 3. 隐蔽状态模型分析

### 3.1 Option A — `UnitState = VISIBLE | CONCEALED`

Advantages: simple UI and serialization.

Defects: `VISIBLE` is observer-relative, while concealment is an actor posture;
concealment is orthogonal to movement, suppression, boarding, weapon state and
damage; a single enum would create invalid cross-products or overwrite existing
state meanings. **Not recommended.**

### 3.2 Option B — typed posture plus detection relation

Recommended model:

```text
Authoritative operator fact:
  concealment_posture = EXPOSED | CONCEALED

Authoritative timed transition:
  ENTER_CONCEALMENT(remaining_ms=75000, status=ACTIVE|COMPLETED|CANCELLED)

Derived observation fact:
  observer_id -> detected target ids + VisibilityEvidence + observation_version
```

This retains one authoritative `WorldState`, models the voluntary posture needed
by visibility and fire modifiers, and keeps observation side-relative. A frozen
transition value is preferable to overloading movement or boarding transitions.

Legacy `raw_attributes.concealed` should be accepted only through an explicit,
versioned compatibility mapping. It must not remain the long-term mutation
surface.

### 3.3 Option C — entirely derived observation

Advantages: no additional authoritative field.

Defects: cannot represent a voluntary 75-second operation, fire/move breaking
the posture, or target-state fire modifiers independently of a particular
observer. It also conflates forest/settlement with voluntary concealment.
**Insufficient.**

### 3.4 Recommendation

Adopt Option B after a separate authority-contract phase. The posture is a
world fact; detection remains per-observer derived evidence. Do not call the
opposite of concealment `VISIBLE`; call it `EXPOSED`, because an exposed unit may
still be outside LOS or observation range.

## 4. 状态转换表

`ENTERING_CONCEALMENT` below denotes a transition record, not a third posture.
Until implemented, every row is contract analysis rather than executable claim.

| Current posture/transition | Event | Condition | Result | Authority |
|---|---|---|---|---|
| EXPOSED | initial deployment | scenario explicitly declares concealed | CONCEALED | Explicit scenario data only |
| EXPOSED | initial deployment | no explicit declaration | EXPOSED | Recommended fail-closed default; automatic initial concealment is **UNKNOWN** |
| EXPOSED | CONCEAL command | alive, deployed, not suppressed, compatible movement/state gates | ENTERING_CONCEALMENT, 75,000 ms | Rule evidence; full capability gates still require contract |
| ENTERING_CONCEALMENT | timer completes | no interruption | CONCEALED | Confirmed |
| ENTERING_CONCEALMENT | another command | transition active | reject other command | Confirmed |
| ENTERING_CONCEALMENT | tank fires | permitted tank-fire condition | cancel transition, EXPOSED | Confirmed |
| ENTERING_CONCEALMENT | suppression begins | — | cancel transition, EXPOSED | Confirmed |
| ENTERING_CONCEALMENT | same-hex combat begins | — | cancel transition, EXPOSED | Confirmed |
| CONCEALED | MOVE starts | valid movement | EXPOSED immediately | Confirmed |
| CONCEALED | DIRECT fire as shooter | valid fire | EXPOSED immediately | Confirmed |
| CONCEALED | GUIDED fire as guider | valid guided fire | remain CONCEALED | Confirmed |
| CONCEALED | GUIDED fire as shooter | valid guided fire | EXPOSED immediately | Confirmed |
| CONCEALED | INDIRECT mission by shooter | — | **UNKNOWN**: generic fire rule suggests exposure, but mission-create versus impact timing is not frozen |
| CONCEALED | suppression begins | already concealed | remain CONCEALED | Confirmed |
| CONCEALED | enemy observes unit | visibility succeeds | posture unchanged; detection relation added/refreshed | Confirmed architecture recommendation |
| CONCEALED | target is attacked | non-suppression result | **UNKNOWN** |
| CONCEALED | target destroyed | destroyed | destruction supersedes posture operationally | Existing destruction model; event semantics still to freeze |
| EXPOSED/CONCEALED | BOARD starts | valid board | **UNKNOWN** posture retention/clear policy |
| EXPOSED/CONCEALED | UNLOAD starts/completes | valid unload | **UNKNOWN** posture result |
| any posture | enter/leave fortification | fortification contract | **OUT OF SCOPE / UNKNOWN in current Engine** |

Terrain entry does not toggle this posture. It independently changes observation
and may provide cover modifiers.

Same-tick interactions require an explicit joint rule. For example, if one side
starts concealment while enemy fire causes suppression in the same sealed batch,
both must read the same pre-state, then the transition and suppression effects
must resolve deterministically. Whether both `STARTED` and `BROKEN` events appear
in that tick is **UNKNOWN** and must be frozen before implementation.

## 5. 多观察者检测语义

Recommended invariant:

```text
RED observer sees BLUE target
!=
BLUE target becomes globally exposed
```

The BLUE target may remain `CONCEALED` while RED observer A has a current
detection relation and RED observer B does not. The RED side observation may
show the target because its projection is the union of authorized friendly
observer contacts. BLUE's own posture does not change merely because RED sees
it.

This distinction prevents information leakage and supports multiple observers,
different sensors and future contact quality. GREEN may retain complete referee
evidence; RED/BLUE receive only detached, policy-constrained contacts. Current
code has no persistent detection memory, so contact persistence and forgetting
are **UNKNOWN / NOT IMPLEMENTED**.

## 6. Fire/Boarding影响

### Direct fire

`NativeEngine._direct_fire_request` recomputes `evaluate_visibility` with target
concealment and target terrain. `DirectFireAdjudicator` requires observable
evidence and applies curated discrete target-state correction. Valid-action
generation also begins from current observed target IDs; the request-level
validation is a defensive authoritative recheck, not a second LOS algorithm.

Missing: successful direct fire does not currently clear the shooter's raw
concealment flag.

### Guided fire

Capability generation uses the **guider's** `see_enemy_bop_ids`, and request
validation recomputes visibility using the same evaluator. The shooter may lack
LOS, as intended. Missing: a completed guided shot currently changes neither
the guider nor shooter's concealment. The confirmed rule is guider retained,
shooter exposed.

### Indirect fire

Indirect fire targets a planned hex and does not require target detection under
the currently versioned `UNOBSERVED` Native policy. Concealment can enter the
damage-effect modifier for an affected unit. There is no duplicate direct-fire
visibility gate. Shooter exposure timing and full spotter policy remain outside
the current executable contract.

### Boarding

An on-board passenger is undeployed and has no independent battlefield hex for
observation/action generation. That is a boarding relation, **not concealment**.
Current BOARD/UNLOAD code neither reads nor changes `concealed`. The rules forbid
performing the concealment transition concurrently with loading/unloading, but
posture retention across BOARD and posture after UNLOAD are **UNKNOWN**.

## 7. Replay影响

Current initial snapshots preserve raw attributes, including an explicitly
present `concealed`. Strict replay re-executes ordered actions and checks official
Native adjudication outputs; it does not currently have a conceal action or
transition to reproduce.

For a future implementation:

1. initial typed posture must be scenario/snapshot authority;
2. concealment intent must enter the ordered action stream (never be inferred
   from state deltas or Action Echo);
3. automatic breaks from MOVE/FIRE and timer completion must be deterministic
   consequences of those inputs;
4. lifecycle events such as `CONCEALMENT_STARTED`,
   `CONCEALMENT_COMPLETED`, and `CONCEALMENT_BROKEN(reason)` are evidence emitted
   only after atomic commit;
5. posture, transition timer and event/RNG/sequence changes must share the tick
   transaction and rollback boundary;
6. the adjudication-semantics version/hash must change and be checked before
   replay step 0.

A replay schema redesign is not inherently required, but old replays must never
be silently reinterpreted as containing concealment actions. They require their
original compatible semantics or a fail-fast compatibility gate.

## 8. 八因子影响

The current demo-compatible Information factor reads
`see_enemy_bop_ids` and therefore measures current contact count. That aligns
with the observation relation, not the target's world posture.

Recommended future separation:

- **A — own concealment posture:** own readiness/posture evidence only; do not
  automatically score it as information advantage;
- **B — enemy observation relations:** primary input for current contacts;
- **C — side information difference:** GREEN-only analysis may compare detached
  RED and BLUE contact sets, but an agent-side observation must never receive
  opponent or GREEN hidden truth.

Contact quality, memory, confidence and information-value weights are
**UNKNOWN**. Reward-model changes are out of scope. Concealment should not alter
Readiness or Status without a separately frozen rule.

## 9. 推荐下一阶段实现方案

Proceed in small authority-first phases:

1. **Authority contract:** define frozen `ConcealmentPosture`, transition model,
   eligibility blockers, interruption precedence and an explicit compatibility
   mapping for old `raw_attributes.concealed`. Keep `keep` UNKNOWN.
2. **Read-only capability:** expose whether an operator can begin concealment;
   do not generate actions from names/types and do not mutate the world.
3. **Action/request contract:** add an explicit intent only after replay-version
   impact is frozen. Decide whether an existing state-change action can express
   it without ambiguity; do not overload movement implicitly.
4. **Transactional lifecycle:** stage start/completion/break effects against the
   same tick pre-state and commit posture, timer, events and sequences atomically.
5. **System integration:** make MOVE, DIRECT shooter, GUIDED shooter,
   suppression and close combat obey the confirmed transition table. Resolve
   the INDIRECT and BOARD/UNLOAD UNKNOWN rows before integrating them.
6. **Observation validation:** keep one `evaluate_visibility` path; add
   multi-observer, terrain-plus-concealment, higher-observer vehicle and contact
   disappearance regressions.
7. **Replay gate:** increment semantics authority and prove action-stream strict
   replay, rollback and old-replay fail-fast/compatibility behavior.
8. **Viewer/agent projection:** expose capability and the seat's authorized
   posture/contact information only after Engine authority exists. The Viewer
   must not calculate detection or infer hidden enemy posture.

### Implementation blockers to freeze first

- initial deployment automatic concealment: **UNKNOWN**;
- BOARD/UNLOAD posture behavior: **UNKNOWN**;
- indirect-fire shooter exposure timing: **UNKNOWN**;
- non-suppressing attack/damage exposure: **UNKNOWN**;
- contact memory and forgetting: **UNKNOWN**;
- same-tick start/interruption event semantics: **UNKNOWN**;
- fortification concealment integration: **SEPARATE CONTRACT**;
- historical Source `keep` mapping: **UNKNOWN / FAIL CLOSED**.

Until those items are resolved, the correct description is:

> The Source-compatible engine implements concealment-aware observation and
> fire modifiers for explicitly supplied posture evidence, but does not yet
> implement an authoritative concealment/exposure lifecycle.
