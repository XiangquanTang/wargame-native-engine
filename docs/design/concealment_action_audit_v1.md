# ENTER_CONCEALMENT Action Audit v1

Status: implementation-entry audit
Date: 2026-08-25
Scope: explicit, immediate `ENTER_CONCEALMENT` for Native simplified land units

This audit precedes implementation. It does not authorize automatic
concealment, concealment levels/areas, camouflage, contact memory, recovery,
air-unit concealment, indirect-fire exposure, or any inferred Source rule.

## 1. Visibility authority today

The Source-compatible observation path is:

```text
WorldState
  -> NativeEngine._refresh_source_observation()
  -> evaluate_visibility()
       - ObservationProfiles/domain distance
       - LOS/elevation/intermediate terrain blocking
       - target terrain modifier
       - target concealment modifier
  -> observer.raw_attributes["see_enemy_bop_ids"]
  -> detached SideObservation
```

`NativeEngine._refresh_source_observation`, direct-fire request construction,
guided-fire visibility and indirect-fire target evidence currently read
`bool(target.raw_attributes.get("concealed", False))`. Terrain is supplied
separately. `evaluate_visibility()` halves the observation limit for concealed
targets, independently applies the FOREST/SETTLEMENT target-cell modifier, and
still requires LOS.

There is one LOS/observation evaluator in the Source-compatible rule path. The
Tactical Overlay deliberately supplies `target_concealed=False` because its
contract is theoretical unconcealed sensor coverage, not actual contacts.

Implementation consequence: introduce typed posture as runtime authority and
replace adjudication-time raw reads. Preserve `raw_attributes["concealed"]` as a
compatibility projection only.

## 2. Operator state today

`core/operator.py::OperatorState` has no typed `concealed`, `hidden`,
`camouflage`, or `posture` field. It has a detached `raw_attributes` mapping.
The raw `concealed` value currently survives snapshot/hash/replay but has no
operation, timer or lifecycle event.

Required boundary:

```text
ConcealmentPosture.EXPOSED | CONCEALED
  -> compatibility projection raw_attributes["concealed"]
```

Runtime Visibility/Fire must read only the typed posture. Legacy raw input may
be promoted once at `OperatorState` construction/deserialization through an
explicit compatibility adapter; it must not remain a rule-time authority.

For old replay/hash compatibility, default EXPOSED must not add a serialized
field or a new raw key to old snapshots. A CONCEALED posture is persisted by the
existing raw compatibility field; restore promotes it back to the typed enum.
This avoids changing historical action JSON and snapshot hashes while making
the in-memory rule source typed.

## 3. Action and capability architecture

Current authoritative input chain:

```text
SeatOperation
  -> ProductOperationService preview
  -> frozen valid action
  -> SeatDecision / RealtimeAgentSession
  -> CommandGateway / DecisionWindow
  -> EngineAction
  -> ActionRouter
  -> NativeEngine._step_source()
```

`ActionType` is a closed enum. `EngineAction` serialization and `action_id` hash
include `action_type`; adding `ENTER_CONCEALMENT` does not alter old action
records. `ActionRouter.supported_types` is the common ownership/alive/deployed/
boarding gate. `_source_valid_actions()` is the Engine-owned capability source.

Human capability presentation groups frozen Engine actions in
`viewer/human_capabilities.py`; `ProductOperationService` previews only matching
frozen actions. Agent `SeatActionCapabilities` exposes detached operation types
and frozen actions. Neither client needs to evaluate movement, domain,
suppression, boarding or transition conditions.

Required new path:

```text
get_concealment_capabilities(side, detached pre-state)
  -> available record becomes frozen ENTER_CONCEALMENT valid action
  -> Product/Human/Agent consume that action
  -> ActionRouter common validation
  -> same-pre-state concealment validation/staging
  -> working-state posture + compatibility projection
  -> atomic tick commit
```

## 4. Frozen v1 validation contract

The Engine must return one deterministic `ConcealmentCapability` per side-owned
operator, including unavailable records and stable reason codes. AVAILABLE
requires all of:

- actor alive and deployed;
- operator domain is PERSONNEL or VEHICLE;
- not mounted/on-board and not boarding locked;
- `segment_to_hex is None`, no active movement task, `speed == 0`, and fully
  stopped;
- movement mode is not MARCHING, CHARGE_1 or CHARGE_2;
- no suppression timer/state;
- no generic transition or move-to-stop timer;
- posture is EXPOSED.

Required explicit failures include `AIR_UNIT_NOT_SUPPORTED`, `MOVING`,
`SUPPRESSED`, `ON_BOARD`, and `ALREADY_CONCEALED`. Unknown/non-land domains fail
closed. This v1 operation is immediate; it does not implement the broader
75-second rule transition.

## 5. Frozen exposure effects

Confirmed v1 posture breaks:

| Accepted authoritative action/effect | Actor affected | Result |
|---|---|---|
| MOVE | moving unit | EXPOSED before movement starts |
| legacy SHOOT / DIRECT | shooter | EXPOSED |
| FIRE GUIDED | shooter | EXPOSED |
| FIRE GUIDED | guider | unchanged |
| suppression | target | unchanged |

Rejected MOVE/FIRE must not expose an operator. Detection, ordinary damage,
BOARD/UNLOAD, and INDIRECT fire do not change posture in v1 because their
contracts remain UNKNOWN or explicitly out of scope.

## 6. Atomicity and events

`_step_source()` already executes on a detached working WorldState and commits
through `_commit_world()`. Concealment validation must use the sealed tick
pre-state and create a frozen staged intent. Applying the intent or exposure
effect only mutates the working state.

Events:

- `UNIT_CONCEALED`: actor, previous posture, new posture;
- `UNIT_EXPOSED`: actor, previous posture, new posture, reason.

Events are accumulated on the working tick and become observable only after the
successful atomic commit. A failed tick restores World, sequences, last events
and recorder behavior through the existing transaction boundary. No RNG is
required.

## 7. Replay contract

New input record:

```json
{
  "action_type": "ENTER_CONCEALMENT",
  "actor_id": 101
}
```

It enters existing `ordered_actions` through `EngineAction.to_dict()` and is
re-executed by strict replay. Replay must never restore `concealed=true` as a
substitute for the action. Old action JSON, ordered-action hashes and old replay
meaning remain unchanged; no replay-schema redesign is required for the new
enum value.

Because the new action changes adjudication behavior, replay compatibility must
continue to use the repository's existing semantics/version gate. This change
must not silently reinterpret a legacy SHOOT, FIRE, MOVE or snapshot.

## 8. Human and Agent projection

Add `ENTER_CONCEALMENT` to Product operation types, command menus, Human
capability projection and the pure frontend supported-capability list. Preview
must resolve the Engine-frozen action; it must not synthesize one from displayed
unit fields. `SeatOperation("ENTER_CONCEALMENT", actor_ids=(id,))` is sufficient;
no target or weapon fields are valid.

Agents continue to receive only detached side observations and frozen valid
actions. They must not receive `WorldState` or calculate eligibility locally.

## 9. Demo and regression boundary

`concealment_basic` must use the standard Demo Library and action-stream replay:

```text
ENTER_CONCEALMENT
  -> UNIT_CONCEALED
MOVE
  -> UNIT_EXPOSED
  -> strict replay world/event/hash match
```

The demo must not branch Engine behavior on its demo ID. Tests must separately
cover air, moving, suppressed and on-board rejection; Direct and Guided exposure
semantics; guider posture retention; and strict replay.

## 10. Explicitly unresolved/out of scope

- automatic initial concealment;
- 75-second enter transition (overridden by this task's immediate v1 scope);
- indirect-fire shooter exposure timing;
- BOARD/UNLOAD posture outcome;
- exposure from detection, damage or target attack;
- fortification concealment;
- contact memory/forgetting;
- Source `keep` mapping;
- air-unit concealment.

All remain fail closed or unchanged. No implementation may infer them from
unit name, subtype, terrain or historical state delta.
