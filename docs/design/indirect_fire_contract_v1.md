# Indirect Fire Contract v1 — Audit and Design Freeze

Status: **AUDIT ONLY / NOT IMPLEMENTED**

Phase A-2 authority facts are frozen in
`config/adjudication/indirect_fire_profiles_v1.json` and exposed only through
the immutable loader in `rules/indirect_fire.py`. The authority document is
explicitly `CONTRACT_ONLY_NOT_EXECUTABLE`.

This document describes a proposed contract for Native simplified indirect
fire. It is not an Engine rule, does not authorize an action, and does not
change the `indirect_fire` capability or Demo status. The implementation must
continue to use
`config/adjudication/source_tables_v1/native_adjudication_tables_final.json`
as its sole machine table source.

## 1. Current indirect-fire capability audit

The repository has substantial source material for indirect-fire outcomes, but
does not have an executable indirect-fire lifecycle.

Current execution boundaries:

```text
EngineAction
  -> ActionRouter.fire_intent()
  -> FireIntent
  -> DIRECT or GUIDED staging
  -> _apply_source_fire_intents()
  -> detached working WorldState
  -> NativeEngine._commit_world()
  -> EngineEvent / GREEN frame / Replay
```

Relevant implementation evidence:

- `core/fire_intent.py` already defines `FireMode.INDIRECT`, `HexTarget`, and a
  frozen `FireIntent`.
- `core/action_router.py::ActionRouter.route_fire_intent` currently routes only
  DIRECT and GUIDED; INDIRECT is rejected.
- `core/action.py::ActionType.FIRE` is already a mode-bearing fire entry point.
  Legacy `ActionType.SHOOT` remains DIRECT.
- `adjudication/request.py::WeaponProfile` currently requires every profile to
  support DIRECT. This contract cannot correctly describe an indirect-only
  artillery weapon.
- `adjudication/guided_fire.py::EffectBundle` is a detached effect contract,
  but its concrete fields and types are GUIDED-specific.
- `core/engine.py::NativeEngine._step_source` provides sealed-batch,
  same-pre-state staging and the existing joint fire aggregation point.
- `core/engine.py::NativeEngine.step_ms` evolves a detached World/RNG/runtime
  and commits only after the whole tick succeeds.
- `io/replay.py` and `replay/source_frame_recorder.py` record ordered
  `EngineAction` values, events, state hashes, ruleset identity, seed, and
  adjudication semantics.

There is no current authoritative representation for a fire mission in flight,
an active bombardment zone, per-mission exposure, cancellation, scatter
direction, or impact-time correction evidence. Therefore indirect fire remains
`NOT_IMPLEMENTED`; the `indirect_fire_basic` Demo is correctly `PENDING`.

## 2. Existing rules

### 2.1 Source-backed rules available now

The curated guide and machine tables establish:

- indirect fire is artillery fire;
- the plan target is a scenario-defined map hex, not a unit target;
- any hex present in the scenario map may be selected as `planned_hex`;
- this global-hex admission policy is not a numeric or infinite weapon range;
- a plan has a 150-second flight phase;
- the explosion zone lasts 300 seconds;
- plan cooldown is 300 seconds;
- an in-flight, unresolved plan can be cancelled and cancellation resets its
  cooldown;
- more than one mission from the same artillery unit may coexist because
  cooldown and mission lifetime overlap;
- correction is evaluated at adjudication time, not necessarily at submission;
- correction levels are unobserved, grid-corrected, and target-corrected;
- eligible correction observers are friendly ground units and UAVs, excluding
  helicopters and loitering munitions;
- the three dispersion tables are present for unobserved, grid-corrected, and
  target-corrected fire;
- hit-target and off-target result tables are present for light, medium, and
  heavy artillery;
- result modifiers cover target terrain, target state, armor, and gun count;
- fire affects friendly as well as enemy units;
- a unit entering the active bombardment hex is adjudicated once;
- movement of the firing artillery during the active explosion phase cancels
  the bombardment point.

Evidence:

- `docs/rules/Native兵棋裁决知识库_Final.md`, section 7;
- `docs/rules/推演规则_完整纯文本_Final.md`, sections 9.1–9.4;
- `native_adjudication_tables_final.json.indirect_fire`;
- `native_adjudication_tables_final.json.runtime_rules.timing`.

### 2.2 Rules that must not be inferred

The source explicitly does not define scatter direction. Phase C-0 therefore
freezes `HEX_NEIGHBOR_6WAY_RANDOM` as a versioned **Native simplified
convention**, not as recovered official behavior. The machine table still
marks the unarmored indirect-fire modifier unresolved. No historical state
delta, unit name, or Action Echo may be used to invent unresolved rules.

The source describes the affected location as the impact/plan hex. It does not
provide a radial blast-distance table. Contract v1 must therefore model one
resolved bombardment hex; a multi-hex blast radius is not authorized.

## 3. Missing machine contracts

### 3.1 Data availability matrix

| Data | Status | Evidence / gap |
|---|---|---|
| Artillery operator identity | AVAILABLE | Exact Source mapping `(type=2, subtype=3)` and weapon 72 exist in the compatibility registry. Registry data is descriptive, not authority. |
| Artillery weapon classes | AVAILABLE | IDs 72/88/89 map to heavy/medium/light artillery in the curated weapon mapping. |
| Dispersion outcome tables | AVAILABLE | Three 2D6 tables exist, including distance bands for unobserved and grid-corrected fire. |
| Hit/off-target result tables | AVAILABLE | Light/medium/heavy result tables exist. |
| Terrain/state/armor/gun-count modifiers | AVAILABLE | Machine table entries exist. |
| Flight/explosion/cooldown timing | AVAILABLE | 150/300/300 seconds in the machine runtime rules. |
| Friendly-fire behavior | AVAILABLE | Explicit in the rule text and source summary. |
| Affected footprint | AVAILABLE, LIMITED | The cited rule supports the resolved hex only; no radial radius is defined. |
| Range policy | AVAILABLE | `SCENARIO_DEFINED_GLOBAL_HEX`: validate map membership only; do not interpret it as infinite range. |
| Maximum weapon range | NOT APPLICABLE | Global-hex admission does not use `maximum_range`; the field remains null. |
| Minimum range | NOT APPLICABLE | Global-hex admission does not use `minimum_range`; the field remains null. |
| Scatter direction | AVAILABLE, NATIVE CONVENTION | Uniform NativeRNG selection among six neighboring hex directions. |
| Meaning/direction for bare `SCATTER` | AVAILABLE, NATIVE CONVENTION | Direction uses the frozen six-way policy; distance remains the referenced 2d6 table result. |
| Unarmored modifier | UNKNOWN | Explicit unresolved item in the curated table. |
| Target domain | AVAILABLE | `AREA`: the selected authority target is a hex, never a unit ID. |
| Ammunition inventory storage | AVAILABLE | Operators already preserve weapon ammunition mappings. |
| Ammunition consumed per mission | MISSING | No curated indirect-fire consumption rule is present. |
| 300-second cooldown value | AVAILABLE | Present in runtime rules, but not wired to an indirect weapon profile. |
| Weapon readiness/unfolded state | AVAILABLE | Operator runtime already records readiness/cooldown/unfolded state. |
| INDIRECT weapon-mode authorization | AVAILABLE, CONTRACT-ONLY | The Phase A-2 authority profile identifies weapons 72/88/89 as INDIRECT artillery; ActionRouter and Engine remain deliberately disconnected. |
| Fire mission lifecycle state | MISSING | WorldState has no flying/active/cancelled mission collection. |
| Per-mission exposure ledger | MISSING | Required to enforce “adjudicated once” without repeated tick damage. |
| Impact-time observer/correction evidence | MISSING | Existing visibility supports units, but no authoritative target-hex correction query/result is defined. |
| RNG roll schedule across multiple occupants | UNKNOWN | The source does not state which rolls are shared per mission versus repeated per affected unit. |

Implementation must stop at these UNKNOWN/MISSING items until a curated Native
convention or source-backed supplement is approved and versioned.

## 4. Recommended data model

All proposed executable contracts should be frozen dataclasses with exact
integer identifiers. They must contain no free-form authority-bearing mapping.

### 4.1 Intent model

Do not add a competing generic fire hierarchy. Define
`IndirectFireIntent` as the following constrained form of the existing
`FireIntent`:

```python
FireIntent(
    mode=FireMode.INDIRECT,
    shooter_id=actor_id,
    weapon_id=weapon_id,
    target=HexTarget(target_hex),
    guider_id=None,
    observer_id=None,
)
```

Contract invariants:

- `mode` is exactly INDIRECT;
- target is exactly `HexTarget`, never a unit ID;
- `shooter_id` is the serialized action's `actor_id`;
- no target unit or hidden contact is required;
- an observer is not selected at submission because correction is determined
  at impact time from the then-current authoritative state;
- `fire_mission_id` is not client supplied. The server derives it
  deterministically from the accepted action/action sequence.

The serialized ingress can reuse `ActionType.FIRE` with an explicitly validated
parameter contract:

```json
{
  "action_type": "FIRE",
  "actor_id": 101,
  "target_id": null,
  "parameters": {
    "fire_mode": "INDIRECT",
    "weapon_id": 72,
    "target_hex": 5042
  }
}
```

`target_id` must remain reserved for unit targets. `target_position` should not
replace the integer map hex identity.

### 4.2 Capability model

Do not enumerate every map hex as a `ValidAction`. Publish one bounded
capability per shooter/weapon:

```python
@dataclass(frozen=True, slots=True)
class IndirectFireCapability:
    shooter_id: int
    weapon_id: int
    range_policy: Literal["SCENARIO_DEFINED_GLOBAL_HEX"]
    minimum_range: None
    maximum_range: None
    target_kind: Literal["HEX"]
    available: bool
    reason_code: str | None
```

Flow:

```text
query capabilities
  -> select shooter/weapon
  -> user or Agent supplies target_hex
  -> server validates target_hex against the current observation/session version
  -> Engine revalidates against the sealed pre-state
```

The capability must not contain enemy units, hidden contacts, or a precomputed
list of thousands of hex actions. A read-only target preview validates that the
selected integer hex belongs to the current scenario map. It must not synthesize
an infinite range or compare against the direct-fire `maximum_range`.

### 4.3 Fire mission state

Indirect fire is delayed and persistent, so a request alone is insufficient.
WorldState eventually needs an authoritative, serialized collection such as:

```python
@dataclass(frozen=True, slots=True)
class IndirectFireMissionState:
    mission_id: str
    action_id: str
    action_sequence: int
    side: str
    shooter_id: int
    weapon_id: int
    launch_hex: int
    planned_hex: int
    phase: FireMissionPhase       # FLYING or ACTIVE
    flight_remaining_ms: int
    impact_hex: int | None
    explosion_remaining_ms: int
    adjudicated_operator_ids: tuple[int, ...]
```

Mission ordering is by stable mission/action sequence. The exposure ledger is
required so remaining in the hex does not silently create a new attack every
tick. Whether an already-present unit is adjudicated once at activation, and
the exact roll-sharing policy for multiple occupants, are implementation gates
that require an explicit Native convention before Phase D.

### 4.4 Request model

Use two lifecycle requests rather than pretending delayed fire is immediate.

Plan-time request:

```python
@dataclass(frozen=True, slots=True)
class IndirectFirePlanRequest:
    tick: int
    action_id: str
    action_sequence: int
    mission_id: str
    shooter_id: int
    weapon_id: int
    launch_hex: int
    planned_hex: int
    ammunition_before: int
    cooldown_before_ms: int
    flight_duration_ms: int
    explosion_duration_ms: int
    cooldown_duration_ms: int
```

Impact-time adjudication request:

```python
@dataclass(frozen=True, slots=True)
class IndirectFireRequest:
    tick: int
    mission_id: str
    shooter_id: int
    weapon_id: int
    planned_hex: int
    impact_hex: int
    correction_level: CorrectionLevel
    observer_id: int | None
    observer_visibility_evidence: VisibilityResult | None
    occupants: tuple[IndirectFireTargetEvidence, ...]
```

`observer_id` is optional because unobserved fire is valid. If more than one
eligible observer exists, selection must be deterministic and evidence must
record all candidates or the stable selection rule. Request construction must
re-evaluate correction against the impact-time pre-state and must not consume
RNG.

### 4.5 Weapon profile

Do not coerce indirect artillery into the current DIRECT-only
`WeaponProfile`. Phase A-2 provides a dedicated frozen `IndirectFireProfile`
carrying the known fields and preserving unresolved values:

```text
weapon_id
artillery_class: LIGHT | MEDIUM | HEAVY
range_policy = SCENARIO_DEFINED_GLOBAL_HEX
minimum_range = None
maximum_range = None
ammunition_key
ammunition_per_mission
requires_unfolded
requires_stopped
flight_ms
explosion_ms
cooldown_ms
supported_fire_modes = {INDIRECT}
```

The profile must be supplied by curated config. The viewer Operator Registry
must never authorize it.

## 5. Adjudicator and Engine integration

### 5.1 Pure adjudicator boundary

`IndirectFireAdjudicator` should receive a validated impact-time request,
curated tables, a detached `NativeRNG`, map geometry, and an explicit approved
scatter-direction policy. It should return a detached resolution:

```text
planned_hex
  -> scatter resolution
  -> impact_hex
  -> affected hex resolution (v1: impact_hex only)
  -> unit adjudication
```

`planned_hex` is command intent; `impact_hex` is a resolved outcome. They must
remain separate fields in mission state, events, GREEN evidence, and any future
Replay semantic version.

```text
IndirectFireRequest
  -> choose correction table
  -> deterministic 2D6 dispersion roll
  -> resolve distance and direction
  -> resolved impact hex
  -> stable occupant order
  -> table-driven damage/suppression rolls and modifiers
  -> IndirectFireEffectBundle + RNG transition + evidence
```

It must not mutate WorldState, emit Recorder frames, or call visibility itself.
Visibility/correction evidence belongs in the request. Every die, modifier,
lookup coordinate, table hash, Native convention flag, and RNG transition must
be retained as evidence.

Suggested effect contracts:

```text
IndirectFireMissionEffect    # add/cancel/update mission runtime
IndirectFireDamageEffect     # per affected unit
IndirectFireSuppressionEffect
IndirectFireResourceEffect   # ammo/cooldown
IndirectFireRngEffect
IndirectFireEffectBundle
```

The current GUIDED `EffectBundle` is reusable as an architectural pattern, not
as a concrete type: its resource, damage, and recovery types are tied to
immediate unit-target guided fire. A later small extraction to mode-neutral
fire effect primitives is acceptable only if DIRECT and GUIDED results remain
byte-for-byte stable.

### 5.2 Engine insertion points

Minimal future integration:

```text
ActionRouter.fire_intent()
  -> recognize FIRE/INDIRECT + HexTarget
NativeEngine._step_source()
  -> stage all new fire plans against the same sealed pre_state
  -> aggregate resource conflicts deterministically
working-state evolution
  -> advance existing mission timers
  -> resolve due impacts using an impact-time pre-state
  -> activate/expire bombardment zones
MovementRuntime arrival
  -> stage one exposure for entry into an active resolved hex
joint effect aggregation
  -> apply to working WorldState
NativeEngine._commit_world()
  -> atomic state/RNG/event publication
```

The existing outer atomic transaction and commit boundary are reusable.
Manual and realtime scheduling require no separate rules.

The exact within-tick order between a due impact and a simultaneous movement
arrival is gameplay semantics and must be explicitly frozen before coding. A
recommended Native convention is impact activation before movement, followed
by entry checks on authoritative arrival events, because current immediate fire
is resolved before `MovementRuntime.advance`. This is a proposal, not a rule
asserted by the source documents.

### 5.3 Events

New `EventType` values are needed; overloading direct-fire events would obscure
the delayed cause chain. Minimum event vocabulary:

```text
INDIRECT_FIRE_PLANNED
INDIRECT_FIRE_CANCELLED
INDIRECT_FIRE_IMPACT_RESOLVED
INDIRECT_FIRE_ZONE_ACTIVATED
INDIRECT_FIRE_UNIT_ADJUDICATED
INDIRECT_FIRE_ZONE_EXPIRED
```

Event ordering must use action sequence, mission sequence, target ID, and event
sequence. Events are created only inside a successful working-state tick.

No new `ActionType` is required: `ActionType.FIRE` plus `fire_mode=INDIRECT` is
sufficient. A cancellation operation still needs a separately frozen product
and action contract before implementation; it must reference an authoritative
mission ID and cannot be inferred from another command.

## 6. Replay impact

### 6.1 What must be recorded

Ordered actions must preserve:

```text
fire_mode=INDIRECT
shooter_id / actor_id
weapon_id
planned_hex
fire_mission_id (server-derived evidence, not client authority)
```

Events/evidence must preserve:

```text
planned_hex and impact_hex as separate evidence fields
correction level and observer evidence
scatter roll, table coordinate, distance and direction
impact occupants in deterministic order
per-target modifiers, rolls and results
friendly-fire results
ammo/cooldown changes
mission activation/cancellation/expiration
RNG before/after
table and policy hashes
```

World snapshots must include the complete fire-mission runtime so strict replay
can verify missions that span ticks.

### 6.2 Compatibility policy

- Existing `SHOOT + weapon_id` remains DIRECT.
- Existing `FIRE/GUIDED` remains GUIDED.
- Missing `fire_mode` must never be inferred as INDIRECT from weapon ID or
  artillery subtype.
- Existing Replay container and action shapes can structurally carry a new
  `FIRE/INDIRECT` action, so a new top-level Replay schema is not inherently
  required.
- The new delayed adjudication and mission runtime are a semantic extension.
  Before release, add an indirect-fire semantics identifier or advance the
  adjudication semantics version with explicit compatibility for old archives.
- Strict replay must fail before step 0 when it encounters an unsupported
  INDIRECT contract. It must re-execute the official Native simplified path;
  restoring recorded outcomes is not acceptable.

Because current strict replay compares one exact adjudication version, version
and backward-compatibility policy must be decided before Phase D. Do not make
old replays depend on new missing fields.

## 7. Implementation phases

### Phase A — Contract and curated configuration

- approve ammunition amount, unarmored modifier, occupant roll schedule, and
  within-tick impact/movement ordering;
- keep the Phase A-2 profile/mission/scatter/impact authority contracts
  immutable and non-executable;
- add schema/provenance validation and no-mutation tests;
- keep ActionRouter execution disabled.

Exit gate: every UNKNOWN/MISSING entry required by execution has an explicit,
hashed source or clearly labelled Native simplified convention.

### Phase B — Capability

- query one capability per shooter/weapon;
- validate ownership, weapon mode, state, readiness, ammunition and scenario
  map membership under `SCENARIO_DEFINED_GLOBAL_HEX`;
- validate a clicked hex server-side without enumerating all hex actions;
- prove no hidden-unit leakage and no World/RNG mutation.

### Phase C — Request and mission runtime

- convert only validated capability + target hex into a plan request;
- add authoritative mission lifecycle and deterministic mission IDs;
- implement flying/active/cancelled/expired transitions without damage;
- cover snapshot/restore and fault rollback.

### Phase D — Adjudicator and atomic integration

- implement table-only dispersion and result resolution using `NativeRNG`;
- stage effects and aggregate with other same-tick fire effects;
- commit mission, damage, suppression, ammo, cooldown and RNG atomically;
- add events and strict replay compatibility/version gates.

### Phase E — Human and Agent adapters

- expose `INDIRECT_FIRE` capability;
- select shooter/weapon, click a target hex, preview, and submit;
- display mission phase and resolved bombardment zone from side-safe evidence;
- do not calculate range, scatter, observers, or damage in the client.

### Phase F — Demo and release evidence

- replace the placeholder only after the Engine contract is executable;
- validate plan, 150-second flight, correction variants, scatter, friendly
  fire, 300-second active zone, entry-once behavior, cancellation, atomic
  rollback, Replay and strict replay;
- then update Capability Registry from `NOT_IMPLEMENTED/PENDING` to the proven
  implementation status.

## 8. Frozen conclusions and remaining decisions

Frozen design conclusions:

1. Reuse `FireIntent`, `FireMode.INDIRECT`, `HexTarget`, `ActionType.FIRE`, the
   sealed fire batch, `NativeRNG`, and the outer atomic commit.
2. Do not reuse DIRECT target visibility: indirect fire targets a hex and
   correction is evaluated at impact time.
3. Do not generate one valid action per hex.
4. Do not use Operator Registry, names, weapon ID heuristics, or hidden contacts
   as rule authority.
5. Model the mission as persistent authoritative state and record its complete
   lifecycle.
6. Preserve old SHOOT and GUIDED replay interpretation exactly.
7. Consume ammunition at mission creation and never refund it on cancellation.
8. Preserve `planned_hex`, resolve a separate `impact_hex`, and affect only
   that resolved hex in v1.
9. Resolve multiple occupants once each in ascending `operator_id` order.

Blocking decisions before implementation:

1. unarmored target modifier;
2. ammunition units consumed per mission;
3. roll sharing across multiple occupants;
4. impact versus movement ordering within one authoritative tick;
5. cancellation action/ingress contract;
6. replay/adjudication semantic compatibility policy.

Until these decisions are curated and versioned, the correct public status is:

```text
Engine: NOT_IMPLEMENTED
Demo: PENDING
Replay support for indirect-fire actions: false
```
