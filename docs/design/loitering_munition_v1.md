# Loitering Munition v1

Status: **IMPLEMENTED — predeployed controllable Operator v1**

This document describes only Native simplified behavior supported by repository
evidence. It does not claim official or SDK-equivalent behavior.

## 1. Authority evidence

The authoritative guide is
`docs/rules/推演规则_完整纯文本_Final.md`, section 10:

- 10.a: a loitering munition moves one hex in 8 seconds and observes from
  200 metres above the current hex elevation.
- 10.b: it observes exposed ground targets at two hexes through the normal LOS
  rules; vehicle concealment does not reduce that limit and personnel
  concealment halves it.
- 10.c–10.e: launch belongs to a vehicle and has deployment/launch constraints.
- 10.f: after finding a target, it may attack immediately without completing
  the submitted movement route.
- 10.g: maximum loiter duration is 1200 seconds, after which it self-destructs;
  destruction of the launcher also destroys it.
- 10.h repeats the launcher-destruction coupling.

The machine adjudication table
`config/adjudication/source_tables_v1/native_adjudication_tables_final.json`
maps Source weapon 76 to `loitering_munition` and contains personnel and vehicle
attack-level tables with a two-hex table domain.

The versioned Engine identity is `(type=3, subtype=7) ->
LOITERING_MUNITION/AIR`. Viewer metadata remains descriptive and does not grant
authority.

## 2. v1 Operator identity

The predeployed v1 entity is an ordinary `OperatorState`. It is not a mission,
projectile, or second engine. A scenario explicitly assigns
`lifecycle_profile_id=LOITERING_MUNITION_PREDEPLOYED_V1` and initializes
`lifecycle_remaining_ms=1200000`.

The versioned lifecycle profile binds that explicit assignment to canonical
identity `LOITERING_MUNITION`, domain `AIR`, Source identity `3/7`, and expiry
behavior `DESTROY_OPERATOR`. Raw `launcher`, `launch_ids`, names, and Viewer
symbol metadata never authorize it.

## 3. Reused capabilities

### Movement

The entity uses ordinary `MOVE`. AIR NORMAL movement uses its explicit
`basic_speed`; a value of 90 km/h produces the documented 8 seconds per 200m
hex. Ground terrain and ground slope modifiers are not applied to AIR. If a
MapRuntime movement matrix is supplied, its mode-3 values remain authoritative.

No `LOITERING_MOVE` action or movement runtime was added.

### Visibility

The existing visibility evaluator, LOS implementation, terrain/concealment
handling, side observation refresh, and RED/BLUE/GREEN projection are reused.
The explicit lifecycle profile supplies the canonical identity when raw
compatibility metadata is absent.

### Human and Agent control

Existing MOVE/SHOOT/WAIT valid actions are exposed by the standard capability stream.
Agents remain limited to detached side observations and frozen valid actions.
No platform-specific Human or Agent action schema is required.

## 4. Generic limited lifecycle

`OperatorState` adds two typed authority fields:

- `lifecycle_profile_id: str | None`
- `lifecycle_remaining_ms: int | None`

Both must be absent or present together. Operators without a lifecycle omit both
keys from serialization, preserving historical state hashes.

NativeEngine advances finite lifetimes using authoritative `delta_time_ms`. It
splits time advancement at the nearest expiry boundary, so movement cannot
continue past the expiry instant inside a large tick. This is independent of
the number of calls to `step`.

At expiry, the existing `UNIT_DESTROYED` fact is emitted with
`reason_code=LIFECYCLE_EXPIRED`; blood, health, alive/state, and active movement
runtime are synchronized. The remaining lifetime is zero. Snapshot and strict
Replay bind the lifecycle authority version and content hash only when the
scenario actually uses a finite lifecycle.

This is intentionally not a generic scheduler. Suppression, cooldown, boarding,
and indirect-fire timers remain unchanged.

## 5. Attack policy and self-consumption

The intended reusable path is:

```text
SHOOT(actor=loitering operator, weapon_id=76, target_id)
  -> existing visibility/LOS/range/domain validation
  -> existing Source table resolution and NativeRNG
  -> existing aggregate DamageEffect/atomic commit
  -> same atomic transaction consumes the attacking operator
```

Target damage does not require a new Damage Engine. The versioned policy
`LOITERING_MUNITION_WEAPON_76_V1` binds explicit lifecycle profile, canonical
identity and weapon 76 to the existing direct-fire validation and Source attack
tables. Range is two hexes; target domains are PERSONNEL and VEHICLE; the target
must be observable through the existing LOS/terrain/concealment evaluator.

Rule 10.f authorizes attack without completing the submitted movement route.
The policy therefore allows weapon 76 to interrupt the ordinary movement or
stopping transition. The attack still uses the weapon's explicit
`can_fire_while_moving` authority, and successful consumption clears the normal
movement runtime through the shared destruction transition.

User authority frozen on 2026-08-26 defines:

- `resource_policy=SELF_AS_MUNITION`; no ammunition key or slot exists;
- `consume_condition=ATTACK_RESOLUTION_COMMITTED`;
- every validated weapon-76 resolution consumes the attacker, including loss,
  suppression-only and `NO_EFFECT` results;
- validation rejection does not consume the attacker.

The current Native direct-fire tables do not expose a separate hit-check or
`MISS` outcome. Their formal outcomes are loss, suppression and `NO_EFFECT`;
the consumption rule is deliberately independent of that outcome shape.

Staging records the target result with the existing NativeRNG. During the same
joint-fire working-world transaction, aggregate target effects are applied and
the attacking Operator is destroyed with
`reason_code=LOITERING_MUNITION_CONSUMED`. No second roll, external mutation or
ammunition event occurs.

## 6. Replay semantics

Replay remains action-stream driven. MOVE/SHOOT/WAIT and authoritative time are
re-executed. Expiry is derived from the initial lifecycle state plus recorded
`delta_time_ms`; final position, health, remaining time, and destroyed state are
not restored as results.

Classic and Source-compatible strict replay fail before stepping when a
lifecycle-bearing scenario lacks the required lifecycle authority version/hash.
An attack-capable loitering scenario additionally binds the attack authority
version/hash. Replays whose initial operators have no matching lifecycle and
weapon pair retain their prior serialized state and interpretation.

## 7. Unsupported future rules

NOT IMPLEMENTED:

- launcher release and typed launcher relation;
- 75-second deployment and 75-second launch;
- launcher ammunition and dynamic operator creation;
- launcher-destruction coupling;
- autonomous search or autonomous attack;
- air-interception-specific rules;
- multi-munition scheduling.

## 8. Current verification boundary

Implemented and tested: exact identity/profile loading, predeployment lifecycle,
1/5/10-second and large-tick expiry, MOVE, movement-time coupling, visibility
refresh, weapon-76 Source-table adjudication, SELF_AS_MUNITION consumption,
illegal-action preservation, event determinism, snapshot/restore, old-operator
serialization, Human/Agent frozen capability reuse, strict attack/expiry
Replay, `loitering_munition_basic`, `loitering_munition_expiry`, and the shared
capability Benchmark runner.
