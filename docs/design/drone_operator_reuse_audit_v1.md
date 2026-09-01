# Drone Operator Reuse Audit v1

## Scope and authority

This audit covers the predeployed, controllable Drone v1 requested for the
Native simplified ruleset. It does not authorize launch, recovery, dynamic
creation, weapons, autonomous behavior, or a separate Drone runtime.

The exact Source identity is supported by independent repository evidence:

- `docs/rules/推演规则_完整纯文本_Final.md`, section 11, defines the UAV
  movement and observation rules. The same source material identifies the
  Source platform represented elsewhere by exact codes `type=3, sub_type=5`.
- `config/source_scenario_conversion/scenarios/2201010105.native-supplement.json`
  contains an exact `3:5` platform record with canonical identity `DRONE`,
  domain `AIR`, and `allows_unarmed=true`.
- `config/source_compat/operator_registry_v1.json` contains the exact `3_5`
  display identity and an empty weapon list. This is supporting semantic/UI
  evidence, not by itself Engine authority.
- `config/rulesets/guidance_profiles_v1.json` also uses exact Source codes
  `3_5` for the separately implemented guidance authority. Drone v1 does not
  assign that profile because this contract explicitly excludes GUIDED FIRE.

`DRONE_SOURCE_IDENTITY_UNKNOWN` does not apply. The missing entry in
`config/source_compat/unit_type_mapping_v1.json` is an implementation gap, not
a rule-evidence gap.

## A1. Canonical identity

| Field | Frozen value |
|---|---|
| Source type | `3` |
| Source subtype | `5` |
| Canonical identity | `DRONE` |
| Domain | `AIR` |
| Deployment | `PREDEPLOYED` |

The Engine's current identity helper recognizes `DRONE` for observation, but
before this work it obtains non-lifecycle identities only from the compatibility
`raw_attributes["canonical_type"]` field. The versioned exact Source mapping
contains only `3_7` (loitering munition). A small generalization is required:
add `3_5` and let the shared canonical resolver consume an exact mapped
`source-<type>-<subtype>` identity before using compatibility metadata.

## A2. Operator model reuse

**OPERATOR_MODEL_REUSE: PASS**

`OperatorState` already represents identity, domain, deployment, position,
movement runtime, observation inputs, health, weapons, relations, and optional
limited lifecycle. Drone v1 requires no `DroneState`, `DroneRuntime`, or Mission.

## A3. AIR movement reuse

**AIR_MOVEMENT_REUSE: PASS**

`MovementPlanner.validate_operator()` accepts the `AIR` domain in `NORMAL`
mode. `MovementPlanner.plan_segment()` applies the configured platform speed
without ground terrain/slope modifiers, and `_movement_cost_mode()` uses
MapRuntime mode 3 when provided. None of these paths names
`LOITERING_MUNITION`. The existing ActionRouter, MovementRuntime, atomic tick,
events, observation refresh, and replay action stream are reusable unchanged.

The frozen Source rule is 8 seconds per hex. Scenario `basic_speed` remains the
authoritative configured platform speed used by that common path.

## A4. Visibility reuse

**VISIBILITY_REUSE: SMALL_GENERALIZATION_REQUIRED**

`ObservationProfiles.limit()` already exposes a common `observer_is_drone` /
`target_is_drone` profile: two hexes against ground targets, no AIR observation,
and one hex for ground observation of the aerial platform. Engine observation
refresh applies LOS, terrain, elevation, and concealment through the normal
visibility evaluator.

`_uses_drone_observation_profile()` already groups canonical `DRONE` and
`LOITERING_MUNITION`; `_source_visibility_height_m()` gives both 200 metres.
No Drone-specific visibility engine is required. The only gap is making the
canonical identity come from the exact versioned mapping rather than requiring
raw compatibility metadata.

## A5. No-lifecycle semantics

**NO_LIFECYCLE_REUSE: PASS**

`OperatorState` represents no finite lifecycle as both
`lifecycle_profile_id=None` and `lifecycle_remaining_ms=None`. Snapshot
validation explicitly accepts that pair. Serialization omits lifecycle fields,
the time advance has no expiry work for the Operator, and no synthetic
infinite duration is required. Drone v1 must remain on this default path.

## A6. No-attack semantics

**NO_ATTACK_CAPABILITY: PASS, with one presentation cleanup**

Source valid actions enumerate direct fire only from `operator.weapon_ids`.
Guided fire additionally requires an explicitly assigned
`guidance_profile_id`; indirect fire requires a carried authorized weapon.
Consequently an unarmed Drone with no guidance profile naturally receives no
SHOOT, DIRECT FIRE, GUIDED FIRE, or INDIRECT FIRE action. A forged SHOOT is
rejected by the ordinary carried-weapon/weapon-profile validation before
resolution, mutation, or RNG consumption.

The generic valid-action builder currently offers `WEAPON_LOCK` to any stopped
Operator with `weapon_locked=false`, including an Operator with no weapons.
That is not a Drone rule. The minimal generic correction is to expose weapon
posture operations only when `weapon_ids` is non-empty. It leaves armed units
unchanged and makes an idle Drone's expected actions `MOVE` and `WAIT`.

## A7. Human and Agent reuse

**HUMAN_AGENT_REUSE: PASS**

Human capabilities are projections of frozen Engine actions and already map
ordinary MOVE/WAIT. Agent authorization freezes those same actions and resolves
only a selected action ID. Neither path depends on a concrete Operator subtype,
and neither needs a Drone-specific schema or direct WorldState access.

## A8. Replay reuse

**REPLAY_REUSE: PASS**

The scenario contains the predeployed Operator. MOVE/WAIT use the ordinary
ordered action stream and authoritative `delta_time_ms`. Snapshot identity is
the generic monotonic replay step identity; snapshot/restore, World hash,
GREEN hash, events, and strict replay contain no Drone-specific branch.

## Audit Gate

There is no `DRONE_SOURCE_IDENTITY_UNKNOWN` blocker and no P0/P1 architectural
blocker. Minimal implementation may proceed. The only required shared changes
are exact canonical mapping/resolution and suppressing meaningless weapon
posture capabilities for all unarmed Operators.

## Frozen Drone v1 contract

| Contract axis | Value |
|---|---|
| Canonical identity | `DRONE` |
| Source identity | `type=3`, `sub_type=5` |
| Domain | `AIR` |
| Deployment | `PREDEPLOYED` |
| Movement | Existing generic AIR `MOVE` |
| Visibility | Existing Drone/loitering observation profile |
| Lifecycle | `NONE` (`None` / omitted, never a sentinel duration) |
| Attack | `NONE` |
| Weapons | Empty |
| Ammunition | Empty |
| Guidance | Not assigned in Drone v1 |
| Human | Existing MOVE/WAIT control |
| Agent | Existing frozen valid actions |
| Replay | Existing strict action-stream replay |

Expected idle actions are adjacent-hex `MOVE` actions plus `WAIT`. While a
movement task is active, the normal framework may expose `STOP_MOVE` and
`WAIT`. No weapon posture or fire action is part of Drone v1.

## Deliberately unsupported

Launchers, launch/deployment delay, dynamic creation, recovery, autonomous
search, autonomous attack, air interception, weapons, ammunition, finite
lifecycle, and a separate Drone Mission/Engine remain not implemented.

## Implementation verification

The audit gate resulted in a configuration/profile-led integration:

- exact `3_5 -> DRONE / AIR` was added to the existing versioned Source unit
  mapping;
- the shared canonical resolver now consumes exact mapped `source-T-S`
  identities before legacy raw compatibility metadata;
- Source frame templates preserve those exact codes for Viewer/product
  projection;
- meaningless weapon posture actions are no longer projected for any unarmed
  Operator;
- no Drone-specific state, action, movement, visibility, replay, Human, or
  Agent implementation was added.

The `drone_basic` Demo executed the existing Human MOVE operation through the
Realtime Gateway, arrived after the configured 8-second AIR traversal, refreshed
the Drone's observation, retained no lifecycle, exposed no attack capability,
and passed strict replay. The matching capability benchmark passed Engine,
Human, Agent, event-trace, World-hash, RNG-hash, and Replay checks.

Verification results on 2026-08-26:

- Drone targeted tests: `11 passed`.
- Related movement/visibility/Source/Human/Agent/Replay/registry tests:
  `208 passed, 1 skipped`.
- Full suite: `1698 passed, 10 skipped, 0 failed`.
- `git diff --check`: PASS before final documentation update; rechecked at
  handoff.

## Final verdict

**PASS — DRONE_V1_OPERATOR_REUSE_CLOSED**

Drone v1 remains an ordinary Operator. Movement Engine, Visibility evaluator,
WorldState schema, Action schema, ActionRouter, Replay core, Human action
schema, and Agent action schema were reused without Drone-specific branches.
