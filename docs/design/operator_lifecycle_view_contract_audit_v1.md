# OperatorLifecycleView Contract Audit v1

Status: **CONTRACT CLOSED — IMPLEMENTATION VERIFIED**

Proposed schema: `native-operator-lifecycle-view/1.0`

Rules characterization: historical-replay-compatible **Native simplified**
semantics; this document does not claim official or SDK-equivalent behavior.

## 1. Executive Summary

The current authoritative `OperatorState` plus the small number of normalized
World relations are sufficient to derive a deterministic, read-only lifecycle
view without adding state, advancing time, enumerating targets, or copying
Engine legality.  Every v1 lifecycle axis can select one primary typed source;
legacy `state` and `raw_attributes` are corroboration, import compatibility, or
separate resource/observation data, never an alternative View authority.

The internal authoritative contract is ready.  The enemy-facing contract is
not: the current perspective policy does not freeze disclosure of typed timer,
fatigue, concealment, boarding, weapon-readiness, or limited-lifecycle fields,
and its configured visible-enemy safe fields do not fully match the current
`extra.observation` construction.  This does not block an internal
`AuthoritativeOperatorLifecycleView`; it requires a separate policy freeze
before any RED/BLUE projection may consume the full view.

Final audit results:

```text
AUTHORITATIVE_OPERATOR_LIFECYCLE_VIEW_READINESS: READY
PERSPECTIVE_PROJECTION_PREPARATION: READY_AFTER_POLICY_FREEZE
DETERMINISM_READINESS: PASS
VIEW_VERSIONING_READINESS: READY
VIEW_HASH_READINESS: READY
PROJECTION_COST: CHEAP
```

No P0 or P1 contract blocker was found.  Existing P2 compatibility and
perspective issues are explicitly isolated below rather than normalized into
the View.

## 2. Scope and Non-goals

This audit freezes fields, meanings, sources, applicability, integrity states,
units, versioning, hashing, and dependency direction.  It does **not** create a
View class, projector, World field, snapshot field, capability, blocker,
encoder, reward, training environment, or rule.

The dependency direction is one-way:

```text
WorldState / OperatorState / versioned rule authority
                    |
                    v
AuthoritativeOperatorLifecycleView       (pure derived value)
                    |
                    v
Perspective filter / Decision projection
                    |
                    v
Human / Agent / Training consumers
```

The Engine must never call the View to decide legality.  The View must never
mutate or retain an independently advancing lifecycle.

## 3. Authoritative State Inventory

`OperatorState` is the primary record.  Boarding transition state is the only
v1 per-Operator lifecycle input that requires World context.  Versioned
identity, movement, guidance, boarding, weapon, and finite-lifecycle authorities
may be supplied to the future projector as immutable interpretation context.

| Semantic | Authoritative field/source | Owner | Type/unit | Time-driven | Capability effect | Serialized/replayed | View candidate | Verdict |
|---|---|---|---|---:|---:|---:|---:|---|
| operator identity | `operator_id`, `side`, `operator_type`, `subtype` plus exact Source/profile resolver | Operator + authority | int/text | no | yes | yes | yes | unique or UNKNOWN |
| domain | `operator_type` parsed as `UnitDomain` | Operator | enum-like text | no | yes | yes | yes | unique or UNKNOWN |
| survival | `alive` | Operator | bool | effect-driven | yes | yes | yes | primary authority |
| damage measures | `health/max_health`, `blood/max_blood` | Operator | float/int | effect-driven | yes | yes | yes | evidence and decision state; not death authority |
| deployment | `deployed` | Operator | bool | action/relation-driven | yes | yes | yes | unique |
| spatial presence | `current_hex is not None` | Operator | optional hex id | movement/relation-driven | yes | yes | yes | unique; absence is not destruction |
| continuous presentation position | `x`, `y`, `altitude`, `heading` | Operator | numeric | movement-driven | observation | yes | separate PositionView except altitude/domain metadata | mostly exclude |
| active movement segment | `segment_from_hex`, `segment_to_hex`, elapsed/traversal ms | MovementRuntime | optional ids + ms | yes | yes | yes | yes | unique |
| remaining movement task | `move_path`, movement action/sequence | MovementRuntime | ordered ids | yes | yes | yes | boolean/count only; route separate | unique |
| movement mode | `movement_mode` | MovementRuntime | `MovementMode` | transition/action | yes | yes | yes | unique or UNKNOWN |
| stopping | `move_to_stop_remaining_ms`, `stopped` | MovementRuntime | ms + bool | yes | yes | yes | yes | unique with integrity checks |
| movement pause | `movement_task_pause_reason` | MovementRuntime | optional typed enum | timer/effect-driven | yes | yes | yes | unique |
| movement blocker | `movement_blocked_reason` | MovementRuntime | optional code | event-driven | yes | yes | yes | unique local status, not capability blocker set |
| generic transition | `transition_kind`, `transition_remaining_ms` | Engine/MovementRuntime | optional code + ms | yes | yes | yes | yes | unique |
| fatigue | `fatigue_level`, `fatigue_recovery_elapsed_ms` + movement policy | MovementRuntime | level + ms | yes | yes | yes | yes | unique for personnel |
| suppression | `suppression_remaining_ms` | Operator/MovementRuntime | ms | yes | yes | yes | yes | unique |
| concealment | `concealment_posture` | Operator | typed enum | persistent/action-driven | yes | via raw compatibility today | yes | unique typed source |
| boarding role | `mounted_on`, `passenger_id` | Operator relation | optional operator ids | action-driven | yes | yes | role/on-board only | unique with World relation check |
| boarding transition | active `WorldState.boarding_transitions` record | World | typed immutable record/ms | yes | yes | yes | yes | requires World context |
| boarding movement lock | `movement_locked` | carrier Operator | bool | transition-driven | yes | yes | yes | unique |
| carried weapons | `weapon_ids` | Operator | ordered ids | resource/config-driven | yes | yes | applicability only | full inventory belongs Resource/Capability View |
| weapon posture | `weapon_locked`, `weapon_unfolded` | Operator | bools | transition-driven | yes | yes | yes | unique with integrity checks |
| weapon cooldown | `weapon_cooldown_remaining_ms` | Operator | ms, Operator-global | yes | yes | yes | yes | unique at current granularity |
| ammunition | Native `raw_attributes.ammunition` / Source-frame fallback plus weapon authority | resource subsystem | map of counts | effect-driven | yes | yes | no | separate ResourceView |
| guidance applicability | `guidance_profile_id`, guided-capable carried weapon authority | Operator + authority | optional id/set | no | yes | yes | yes | context-derived applicability |
| guided recovery | `guided_fire_recovery_remaining_ms` | Operator | ms | yes | yes | yes | yes | unique |
| indirect planning applicability | carried weapon + indirect authority profile | authority | ids/profile | no | yes | replay manifest/profile | yes | context-derived applicability |
| indirect planned cooldown | `indirect_fire_planned_cooldown_remaining_ms` | shooter Operator | ms | yes | yes | yes | yes | unique; mission-independent |
| indirect mission lifecycle | `WorldState.indirect_fire_missions` | World mission subsystem | mission records | yes | separate | yes | no | separate MissionView |
| limited lifecycle | `lifecycle_profile_id`, `lifecycle_remaining_ms` + authority | Operator | optional id + ms | yes | yes | yes | yes | unique paired contract |

## 4. State Authority Map

| Axis | Primary View authority | Secondary inputs | Mapping verdict |
|---|---|---|---|
| Survival | `alive` | health/blood/state are integrity evidence | **UNIQUE** |
| Deployment | `deployed`, `current_hex`, typed boarding relation | active World boarding transition | **UNIQUE** |
| Movement | segment fields, stop timer, `stopped`, typed transition | speed/mode as consistency evidence | **UNIQUE** |
| Movement task | `move_path`, active segment, typed pause/block reason | action identifiers are provenance only | **UNIQUE** |
| Fatigue | typed level/elapsed + versioned movement policy | domain determines applicability | **UNIQUE** |
| Suppression | `suppression_remaining_ms` | movement pause consumes but does not own it | **UNIQUE** |
| Concealment | `concealment_posture` | raw `concealed` is compatibility only | **UNIQUE** |
| Boarding | typed relations + one active World transition | `state=ON_BOARD` is corroboration only | **UNIQUE** |
| Weapon | typed posture/transition/cooldown at Operator-global granularity | weapon authority determines applicability | **UNIQUE** |
| Guided | typed recovery timer + explicit profile/weapon authority | capability remains separate | **UNIQUE** |
| Indirect | typed shooter cooldown + indirect weapon/profile authority | missions remain separate | **UNIQUE** |
| Limited lifecycle | paired typed profile/remaining + lifecycle authority | canonical identity is evidence | **UNIQUE** |

An unmapped canonical identity, unknown enum value, missing authority profile, or
cross-field contradiction does not create a second source.  It produces
`UNKNOWN`, `MISSING`, or `INCONSISTENT` according to section 9.

## 5. Legacy State String

`LEGACY_STATE_STRING_ROLE: PARTIAL`

The string is still written and sometimes read by current Engine code, but it
mixes independent axes.  It cannot be the View authority.

| Value | Current role | Typed View source |
|---|---|---|
| `ACTIVE`, `ready` | legacy/presentation baseline | survival + deployment + movement axes |
| `MOVING`, `MARCHING` | movement presentation | active segment + `movement_mode` |
| `STOPPED` | movement presentation | `stopped`, timers, segment/task fields |
| `TRANSITIONING`, `TRANSITIONING_TO_STOP` | transition presentation | typed transition/stop timers |
| `SUPPRESSED` | compatibility guard in some validators | typed suppression timer |
| `ON_BOARD` | compatibility/presentation | `mounted_on` plus World relation |
| `BLOCKED` | transient movement presentation | `movement_blocked_reason` and task fields |
| `destroyed`, `DESTROYED` | destruction corroboration | `alive` |
| `CLOSE_COMBAT` | legacy Direct-fire input with no implemented typed lifecycle | excluded from v1; integrity/UNKNOWN if encountered |

`DO_NOT_USE_AS_VIEW_AUTHORITY`: the complete `state` string, including every
value above.  The future projector may compare it against typed axes and append
a deterministic integrity code, but must not use it to repair or override them.

## 6. Raw Compatibility

`RAW_COMPATIBILITY: PARTIAL`, with a strict v1 read boundary.

| Semantic | Typed/authority source | Raw/source-compatible value | Current direction | Stale risk | LifecycleView rule |
|---|---|---|---|---:|---|
| concealment | `concealment_posture` | `concealed` | typed→raw; construction migration raw→typed | yes if externally mutated | typed only |
| suppression | `suppression_remaining_ms` | `suppression_remaining_seconds` | effect writes both; only typed decrements | **known stale** | typed only |
| canonical identity | lifecycle profile or exact `source-T-S` mapping | `canonical_type` legacy fallback | compatibility fallback | yes/unversioned | exact resolver; otherwise UNKNOWN |
| contacts | visibility evaluation | `see_enemy_bop_ids` | derived observation cache | refresh-dependent | exclude |
| ammunition | no typed inventory today | `ammunition` / nested Source bullets | current resource authority | controlled Engine writes | exclude to ResourceView, do not reinterpret |
| passenger relations | `mounted_on`, `passenger_id` | raw passenger IDs/`car` | compatibility/export and some legacy damage evidence | possible | typed World relation only |
| armor/normalized value | adjudication/source mapping | raw armor/value records | adjudication compatibility | mapping-dependent | exclude |
| visibility height | profile/exact identity plus allowed explicit input | `visibility_height_m` | visibility-only | mapping-dependent | exclude |
| weapon/source fields | scenario weapon authority | nested Source frame | import/resource fallback | mapping-dependent | exclude |

`VIEW MUST NOT READ`:

- `raw_attributes` as a whole;
- `suppression_remaining_seconds`;
- raw `concealed` after `OperatorState` construction;
- `see_enemy_bop_ids`;
- raw passenger/launcher relations;
- raw state/movement fields;
- raw canonical identity as a permissive fallback;
- ammunition, armor, source weapon, or visibility evidence as lifecycle state.

The ammunition exception remains an Engine resource authority today, but it is
not a lifecycle semantic and therefore does not belong in this View.

## 7. Lifecycle Semantic Axes

The recommended v1 field groups are:

1. `identity`: operator id, side, canonical identity status/value, domain.
2. `survival`: alive, destroyed, health/blood measures.
3. `deployment`: deployed, spatial status, current hex.
4. `movement`: semantic state/mode, segment/task presence, progress, pause,
   block, stop and generic transition timers.
5. `fatigue`: applicability, level, recovery progress/remaining.
6. `suppression`: active and remaining milliseconds.
7. `concealment`: typed posture.
8. `boarding`: role, on-board state, active transition, movement lock; relation
   IDs belong in a separate RelationView.
9. `weapon_lifecycle`: applicability, posture, Operator-global cooldown and
   relevant transition.
10. `guided_recovery`: applicability, active, remaining, lifecycle-ready.
11. `indirect_planning_recovery`: applicability, active, remaining,
    lifecycle-ready.
12. `limited_lifecycle`: enabled/profile/status/remaining.
13. `integrity`: overall status and sorted deterministic codes.

Full action instances, target availability, route cells, mission records,
resource inventories, observation contacts, and session controllability are not
part of the lifecycle semantic core.

## 8. Movement Semantic Contract

Integrity is evaluated before normal mapping.  An impossible combination maps
to `INCONSISTENT`; an unsupported enum/code maps to `UNKNOWN`.

| Semantic state | Required authoritative mapping |
|---|---|
| `MOVING` | `segment_to_hex != None`, valid segment endpoints/timing, positive traversal, `stopped=false`; a suppression pause reason may be present as a **pending pause** while the current segment finishes |
| `STOPPING` | no active segment, `move_to_stop_remaining_ms > 0`, `stopped=false`; remaining task may be preserved |
| `PAUSED` | no active segment/stop transition, `move_path` nonempty, typed `movement_task_pause_reason != None`, and the pause invariant is satisfied |
| `BLOCKED` | no active segment, `movement_blocked_reason != None`, and an authoritative remaining task is retained; this reports state only and promises no resume policy |
| `TRANSITIONING` | no active segment/stop transition and `transition_kind != None` with positive remaining ms; the transition group identifies movement-mode versus weapon transition |
| `STOPPED` | `stopped=true`, speed zero, no segment, no stop/generic transition, no paused or blocked retained task |
| `UNKNOWN` | an enum/code or required authority mapping is unsupported but fields are not demonstrably contradictory |
| `INCONSISTENT` | fields violate an invariant, for example moving without a segment or paused without a task |

Companion fields prevent state-name overloading:

- `mode`: exact `MovementMode` or UNKNOWN;
- `has_active_segment`;
- `has_remaining_task` and `remaining_path_length`, not the path itself;
- `segment_progress_millionths` and derived `segment_remaining_ms`;
- `pause_pending`, `pause_reason`;
- `blocked_reason`;
- `stop_transition_remaining_ms`;
- `operator_transition.kind/remaining_ms`.

Speed zero alone never means STOPPED.  A suppression pause reason alone never
means PAUSED while the current segment is still active.

## 9. Timer Semantics

All View timer values use integer **milliseconds**.  No ticks, implicit seconds,
wall clock, normalized ML values, or floating-point durations are permitted.

| Timer/progress | `None` | `0` | Positive | Missing/invalid | View semantic |
|---|---|---|---|---|---|
| segment remaining (derived) | no active segment | arrival boundary | segment active | inconsistent segment fields | active progress only |
| move-to-stop | not representable in typed field | inactive/complete | STOPPING | missing typed field is MISSING | local stop transition |
| generic transition | valid only when kind is also absent | complete/inactive only when kind absent | TRANSITIONING | kind/time mismatch is INCONSISTENT | operator transition |
| fatigue recovery elapsed | N/A for non-personnel | no accumulated recovery | accumulated progress | policy missing → MISSING for derived remaining | progress, not readiness by itself |
| suppression | not representable | inactive | active blocker | missing typed field is MISSING | `active = remaining > 0` |
| weapon cooldown | N/A represented by applicability, not `None` | blocker cleared | cooldown active | missing typed field is MISSING | Operator-global lifecycle readiness |
| Guided recovery | N/A represented separately | recovery blocker cleared | recovering | authority needed to classify applicability | lifecycle readiness only |
| Indirect planned cooldown | N/A represented separately | planning blocker cleared | cooling down | authority needed to classify applicability | shooter planning readiness only |
| Boarding transition | no active World transition | COMPLETED record; not active | ACTIVE transition | relation/record mismatch is INCONSISTENT | active transition lifecycle |
| limited lifecycle | **no limited lifecycle** when profile is also `None` | expired/consumed; living Operator is INCONSISTENT | finite lifecycle active | unpaired profile/time is MISSING/INCONSISTENT | enabled/status/remaining |

Typed integer fields that default to zero remain present on `OperatorState` even
when the associated capability is not applicable.  Applicability must come from
explicit profile/weapon/domain authority, not from zero.

## 10. Readiness, Capability, and Legality

These are three different layers:

1. **Lifecycle readiness**: one local timer/posture blocker is clear.  Examples:
   `guided_recovery_remaining_ms == 0`, weapon cooldown zero, or Indirect plan
   cooldown zero.
2. **Capability availability**: the Engine combines identity/profile,
   survival, deployment, relation, posture, resources, observation-independent
   operator rules, and other blockers to expose a capability/valid action
   family.
3. **Action-instance legality**: the Engine validates one concrete target,
   weapon, guider, carrier, path, or hex against the sealed pre-state.

Therefore names must be narrow: `guided_recovery_ready`,
`weapon_cooldown_clear`, and `indirect_plan_cooldown_clear` are acceptable;
`can_guided_fire`, `can_fire`, `can_move`, and `controllable` are not v1
LifecycleView fields.  The View does not enumerate or cache `valid_actions`.

Session/seat ownership is also outside lifecycle control eligibility.  If a
future Decision Projection needs `controllable`, it must combine perspective,
seat authorization, and Engine capability rather than derive it from `alive`.

## 11. UNKNOWN, MISSING, NOT_APPLICABLE, and INCONSISTENT

The four meanings are distinct and stable:

- `UNKNOWN`: authoritative input exists but its value or mapping is not known to
  schema v1 (unknown movement mode, unresolved canonical identity).
- `MISSING`: data or authority required by the contract should exist but is
  absent (transition kind without required time/profile, known profile id not
  loadable).
- `NOT_APPLICABLE`: explicit authority proves that the axis does not apply
  (Drone finite lifecycle; Operator with no explicit Guided role/weapon for
  Guided recovery semantics).
- `INCONSISTENT`: available authoritative fields contradict a frozen invariant
  (alive finite-lifecycle Operator at zero, passenger relation without matching
  carrier, pause reason without a task).

`None`, zero, missing, and NOT_APPLICABLE must not be serialized as synonyms.
An unknown or inconsistent axis fails closed and sets the top-level integrity
status; the projector never repairs World state.

## 12. Perspective Classification

The authoritative View is full GREEN-derived state and is **not** a RED/BLUE
API.  The table freezes only input classification; enemy disclosure remains a
future policy decision.

| Field group | GREEN | OWN side | observed enemy | Policy status |
|---|---|---|---|---|
| operator id / side | full | full | id/side association currently observable | defined for current observation |
| canonical identity/domain | full | full | current policy/config and `extra.observation` disagree on exact identity | **POLICY_UNDEFINED** |
| alive/destroyed | full | full | current safe field | defined at coarse level |
| health/blood | full | full | health currently appears in side observation extra but is absent from configured safe fields | **POLICY_UNDEFINED** |
| deployed/spatial/current hex | full | full | current visible contact exposes location; deployment detail not separately frozen | partial/undefined |
| movement semantic/mode/speed | full | full | only legacy `state` is currently safe-listed | **POLICY_UNDEFINED** |
| remaining path/task/progress | full | full | no disclosure rule | **POLICY_UNDEFINED**; default hidden |
| fatigue | full | full | no disclosure rule | **POLICY_UNDEFINED** |
| suppression active/remaining | full | full | no typed disclosure rule | **POLICY_UNDEFINED** |
| concealment posture | full | full | observation uses posture but disclosure is not frozen | **POLICY_UNDEFINED** |
| boarding role/relation/transition | full | own subject to relation cleaning | relation ids are explicitly filtered; semantic disclosure absent | **POLICY_UNDEFINED** |
| weapon posture/cooldown | full | full | no disclosure rule | **POLICY_UNDEFINED** |
| Guided/Indirect recovery | full | full | no disclosure rule | **POLICY_UNDEFINED** |
| finite lifecycle/profile/remaining | full | full | no disclosure rule | **POLICY_UNDEFINED** |
| integrity codes | full/debug | own policy not frozen | never copy by default | GREEN_ONLY pending policy |

Recommended structure:

```text
AuthoritativeOperatorLifecycleView
        -> explicit PerspectiveLifecyclePolicy
        -> perspective-safe Decision Semantic Projection
```

RED/BLUE must never receive the authoritative object and must never fill unknown
enemy fields from GREEN.

## 13. Snapshot and Restore Contract

`VIEW_SERIALIZATION_AUTHORITY: DO_NOT_SERIALIZE_AS_AUTHORITY`

The View is recomputable and must not enter `WorldState`, the authoritative
snapshot schema, or World hash.  Correct verification is:

```text
World A -> View A
World A -> snapshot -> restore -> World B -> View B
assert View A == View B
```

Saving a View as an optional debug/benchmark artifact is permissible only when
clearly labeled derived and versioned.  Restore must ignore it.

## 14. Strict Replay Contract

Strict Replay continues to execute initial scenario + ordered actions + exact
`delta_time_ms`, reconstructing authoritative World state.  The View is then
recomputed.  A View hash may be compared as additional derived verification,
but cannot replace World hash, GREEN hash, RNG comparison, events, or action
re-execution.

Recorder facts, Replay input, and the View remain separate:

```text
Action stream -> Engine -> World + fact events -> Recorder
                         |
                         -> current LifecycleView
World/event stream -> Analyzer (separate derived analysis)
```

## 15. Versioning and Semantic Hash

Recommended schema identifier: `native-operator-lifecycle-view/1.0`.

- A changed field meaning, authority source, enum meaning, applicability rule,
  unit, or UNKNOWN behavior requires a semantic version bump.
- A backward-compatible additive optional field may use a minor version under
  the project's chosen policy; changing existing meaning requires a major
  contract revision.
- Renderer order, comments, or non-semantic presentation do not change the
  contract.

`HASH_RECOMMENDED` for regression, Replay verification artifacts, API checks,
and benchmarks.  Canonicalization must:

1. include `schema_version`, operator id, all semantic groups, explicit
   applicability/status markers, and sorted integrity codes;
2. serialize enums by stable string values and integers in their stated units;
3. sort all maps/sets and use the repository's canonical JSON/SHA-256 rules;
4. exclude the hash field itself, World hash, perspective, timestamps,
   `created_at`, memory identity, raw attributes, renderer formatting, caches,
   RNG, and external services.

The authoritative View hash and a future perspective-projected hash are
different contracts and must never be compared as if interchangeable.

## 16. Proposed Non-Implemented View Schema

The following is a semantic draft, not Python code and not a World schema.
Perspective `OWN` means suitable for own-side policy consideration, not that the
current observation object already exposes the field.

| Field | Source class | Semantic | Perspective class |
|---|---|---|---|
| `schema_version` | DERIVED/CONTRACT | exact schema id | GREEN, OWN |
| `semantic_hash` | DERIVED | canonical hash excluding itself | GREEN; projected hash separate |
| `operator_id` | OPERATOR_TYPED | stable identity | GREEN, OWN, OBSERVED_ENEMY |
| `identity.side` | OPERATOR_TYPED | owning side | GREEN, OWN, OBSERVED_ENEMY |
| `identity.domain` | OPERATOR_TYPED/DERIVED | parsed `UnitDomain` or UNKNOWN | GREEN, OWN; enemy policy partial |
| `identity.canonical.status/value` | DERIVED from profile/exact mapping | KNOWN/UNKNOWN identity | GREEN, OWN; enemy UNDEFINED |
| `survival.alive` | OPERATOR_TYPED | primary survival fact | GREEN, OWN, OBSERVED_ENEMY |
| `survival.destroyed` | DERIVED | exact `not alive` | GREEN, OWN, OBSERVED_ENEMY |
| `survival.health/max_health` | OPERATOR_TYPED | Native health measures | GREEN, OWN; enemy UNDEFINED |
| `survival.blood/max_blood` | OPERATOR_TYPED | discrete Source-compatible measures | GREEN, OWN; enemy UNDEFINED |
| `deployment.deployed` | OPERATOR_TYPED | deployment axis | GREEN, OWN; enemy UNDEFINED |
| `deployment.spatial_status` | DERIVED + WORLD_RELATION | ON_MAP/ON_BOARD/OFF_MAP/INCONSISTENT | GREEN, OWN; enemy UNDEFINED |
| `deployment.current_hex` | OPERATOR_TYPED | optional authoritative hex | GREEN, OWN, observed location subject to perspective |
| `movement.status` | DERIVED | STOPPED/MOVING/STOPPING/PAUSED/BLOCKED/TRANSITIONING/UNKNOWN/INCONSISTENT | GREEN, OWN; enemy UNDEFINED |
| `movement.mode` | OPERATOR_TYPED | exact mode or UNKNOWN | GREEN, OWN; enemy UNDEFINED |
| `movement.speed` | OPERATOR_TYPED | current Engine speed | GREEN, OWN; enemy UNDEFINED |
| `movement.has_active_segment` | DERIVED | segment exists | GREEN, OWN; enemy UNDEFINED |
| `movement.segment_progress_millionths` | OPERATOR_TYPED | integer sub-hex progress | GREEN, OWN; enemy UNDEFINED |
| `movement.segment_remaining_ms` | DERIVED | traversal minus elapsed | GREEN, OWN; enemy UNDEFINED |
| `movement.has_remaining_task/path_length` | DERIVED | task presence/count, not cells | GREEN, OWN; enemy UNDEFINED |
| `movement.pause_pending/pause_reason` | OPERATOR_TYPED/DERIVED | explicit pause semantics | GREEN, OWN; enemy UNDEFINED |
| `movement.blocked_reason` | OPERATOR_TYPED | local movement blocker evidence | GREEN, OWN; enemy UNDEFINED |
| `movement.stop_transition_remaining_ms` | OPERATOR_TYPED | stop timer | GREEN, OWN; enemy UNDEFINED |
| `operator_transition.kind/remaining_ms` | OPERATOR_TYPED | generic named transition | GREEN, OWN; enemy UNDEFINED |
| `fatigue.applicability` | DERIVED from domain/authority | applicable/N/A/unknown | GREEN, OWN; enemy UNDEFINED |
| `fatigue.level/recovery_elapsed_ms` | OPERATOR_TYPED | current fatigue/progress | GREEN, OWN; enemy UNDEFINED |
| `fatigue.recovery_remaining_ms` | DERIVED with movement policy | time to next level recovery while eligible | GREEN, OWN; enemy UNDEFINED |
| `suppression.active/remaining_ms` | DERIVED + OPERATOR_TYPED | typed suppression lifecycle | GREEN, OWN; enemy UNDEFINED |
| `concealment.posture` | OPERATOR_TYPED | EXPOSED/CONCEALED | GREEN, OWN; enemy UNDEFINED |
| `boarding.role/on_board` | DERIVED + WORLD_RELATION | NONE/PASSENGER/CARRIER/inconsistent | GREEN, OWN; enemy UNDEFINED |
| `boarding.transition.type/remaining_ms` | WORLD_RELATION | active BOARD/UNLOAD transition | GREEN, OWN; enemy UNDEFINED |
| `boarding.movement_locked` | OPERATOR_TYPED | carrier action lock fact | GREEN, OWN; enemy UNDEFINED |
| `weapon_lifecycle.applicability` | DERIVED from carried weapon authority | applicable/N/A/unknown | GREEN, OWN; enemy UNDEFINED |
| `weapon_lifecycle.locked/unfolded/posture` | OPERATOR_TYPED/DERIVED | current posture and integrity | GREEN, OWN; enemy UNDEFINED |
| `weapon_lifecycle.cooldown_active/remaining_ms` | OPERATOR_TYPED/DERIVED | Operator-global cooldown only | GREEN, OWN; enemy UNDEFINED |
| `guided_recovery.applicability` | DERIVED from explicit guidance/weapon authority | applicable/N/A/unknown | GREEN, OWN; enemy UNDEFINED |
| `guided_recovery.active/remaining_ms/ready` | OPERATOR_TYPED/DERIVED | recovery blocker only | GREEN, OWN; enemy UNDEFINED |
| `indirect_planning.applicability` | DERIVED from indirect authority | applicable/N/A/unknown | GREEN, OWN; enemy UNDEFINED |
| `indirect_planning.cooldown_active/remaining_ms/ready` | OPERATOR_TYPED/DERIVED | planning blocker only | GREEN, OWN; enemy UNDEFINED |
| `limited_lifecycle.enabled/profile_id/status/remaining_ms` | OPERATOR_TYPED + authority | N/A/ACTIVE/EXPIRED/UNKNOWN | GREEN, OWN; enemy UNDEFINED |
| `integrity.status` | DERIVED | OK/UNKNOWN/MISSING/INCONSISTENT | GREEN; own/enemy policy UNDEFINED |
| `integrity.codes` | DERIVED sorted tuple | stable non-mutating findings | GREEN; default hidden |

Relationship identifiers (`carrier_id`, `passenger_id`, guidance affiliation)
are deliberately not in the core View; a separate relation projection may join
them under perspective policy.  Indirect missions remain a MissionView.

## 17. Invariants

Future implementation tests should evaluate, never repair, at least:

1. `not alive` implies no active movement segment; destruction cleanup should
   have cleared task/transition runtime.
2. `alive` plus zero finite lifecycle is inconsistent.
3. finite lifecycle profile and remaining time are either both absent or both
   present, and the profile exists.
4. `mounted_on != None` implies no independent current hex and a reciprocal
   carrier relation.
5. `passenger_id != None` implies reciprocal passenger relation.
6. one Operator cannot be both passenger and carrier.
7. one Operator participates in at most one ACTIVE boarding transition.
8. active segment requires endpoints, positive traversal, bounded elapsed, and
   `stopped=false`.
9. no segment implies segment timing/progress/evidence are clear.
10. STOPPING requires positive stop timer, no active segment, and
    `stopped=false`.
11. PAUSED requires explicit pause reason and a remaining task.
12. suppression pause requires a movement task; the suppression timer may be
    zero only while another required boundary (for example stop transition)
    still prevents resume.
13. STOPPED requires no active segment or transition and speed zero.
14. generic transition kind/time must be paired; zero-duration stable snapshots
    with a retained kind are inconsistent.
15. weapon lock/unfold booleans must form a recognized posture or transition.
16. Guided recovery positive implies `guided_recovery_ready=false`.
17. Indirect planned cooldown positive implies
    `indirect_plan_cooldown_clear=false`.
18. typed concealment posture must be a known enum; raw mismatch is diagnostic
    P2 evidence and never overrides typed state.
19. `current_hex=None` alone never implies destroyed, missing, or undeployed.
20. legacy `state` disagreement produces an integrity code, never a write-back.

## 18. Excluded Fields

`EXCLUDED_FROM_LIFECYCLE_VIEW`:

- the complete `raw_attributes` mapping;
- `see_enemy_bop_ids` and all contact/LOS evidence;
- full remaining movement path cells and pathfinding/route candidates;
- weapon/ammunition inventories and damage/adjudication tables;
- guidance affiliation, carrier/passenger, launcher, and target relation IDs
  (separate RelationView);
- Indirect Fire missions and active-area ledgers (separate MissionView);
- full valid action instances, action masks, target/hex enumeration, and
  capability records;
- Replay manifest/storage metadata, recorder event sequence, RNG, adjudication
  evidence, World/GREEN hashes, and action history;
- GREEN analysis, EightFactorAnalyzer output, advantage/effective-power values,
  winner/reward/target-value labels;
- normalized RL features, ObservationEncoder/ActionEncoder output, LLM text,
  renderer coordinates/symbols, and UI formatting;
- objective/scoring/terminal state except where a future Decision Projection
  explicitly joins them.

Eight-factor and reward analysis remain downstream, separate channels.  They
must not become lifecycle authority.

## 19. Determinism, Cost, and Dependency Direction

`DETERMINISM_READINESS: PASS`.

All included fields derive from immutable reads of typed Operator state,
normalized World relation records, and versioned authority.  The projector must
sort integrity codes and any indexed relation input by stable ids.  It must not
read RNG, wall clock, events, mutable caches, external services, LOS, or target
enumeration.

`PROJECTION_COST: CHEAP`:

- O(number of Operators + active Boarding transitions) for a batch projection;
- O(1) per Operator after a deterministic boarding-transition index;
- no LOS search, pathfinding, damage lookup, capability enumeration, or mission
  simulation.

Recommended dependency/module location:

```text
src/wargame_native_engine/projection/operator_lifecycle.py
```

A new narrow `projection` package makes the boundary explicit: it may import
`core` records and read-only rule authority; `core/engine.py` must not import it.
`observations`, `session`, viewer, analysis, and future training code may depend
on it through deliberate adapters.

Recommended type: nested `@dataclass(frozen=True, slots=True)` records with
string-valued enums and immutable tuples.  This matches current contracts,
prevents accidental mutation, remains easy to compare/test, and canonicalizes
without a runtime validation dependency.  A plain dict or TypedDict is too easy
to mutate; Pydantic adds unnecessary authority/runtime coupling.

Suggested pure API shape (not implemented):

```text
project_operator_lifecycle(world, operator_id, authority_context)
    -> AuthoritativeOperatorLifecycleView
```

World context is required only for reciprocal boarding relations and the active
boarding transition.  Authority context is required for canonical identity,
applicability, and fatigue threshold interpretation.  Nothing is copied back to
the Operator.

## 20. Findings and Blockers

### P0

NONE.

### P1

NONE.  No v1 semantic requires stale raw state, copied Engine legality, RNG,
wall clock, or a new authoritative field.

### P2

1. Raw suppression seconds can remain stale after typed milliseconds advance;
   the View contract explicitly forbids reading them.
2. Typed concealment currently persists through a typed→raw→typed serialization
   path; the View still reads the reconstructed typed posture only.
3. Legacy `state` is still a compatibility guard in several Engine paths and
   `CLOSE_COMBAT` has no typed lifecycle transition.  V1 does not infer a close
   combat axis.
4. The configured visible-enemy safe-field list excludes exact health/type
   details that current `extra.observation` can contain.  Enemy lifecycle field
   disclosure needs a dedicated policy freeze.
5. Weapon cooldown is Operator-global; per-weapon granularity is unresolved.
   V1 reports only the actual Operator-global timer.
6. Boarding relation IDs and lifecycle details require explicit perspective
   disclosure policy rather than reusing GREEN relation state.

### P3

1. `move_to_stop_remaining_ms` and generic `transition_remaining_ms` require
   separate names in the View to avoid unit/state confusion.
2. Canonical identity resolution is currently an Engine helper plus versioned
   mapping loaders; implementation should reuse/extract a read-only resolver
   boundary rather than copy the algorithm.

`CONTRACT BLOCKERS: NONE`.

Unknown enemy disclosure is a **PERSPECTIVE POLICY UNDEFINED** condition, not an
internal contract blocker.  The policy freeze must cover exact identity/domain,
health/blood, deployment, movement mode/task/progress, fatigue, suppression,
concealment, boarding relations/transitions, weapon posture/cooldown,
Guided/Indirect recovery, finite lifecycle, and integrity codes.

## 21. Implementation Readiness and Required Tests

```text
AUTHORITATIVE_OPERATOR_LIFECYCLE_VIEW_READINESS: READY
PERSPECTIVE_PROJECTION_PREPARATION: READY_AFTER_POLICY_FREEZE
VIEW_VERSIONING_READINESS: READY
VIEW_HASH_READINESS: READY
```

The next implementation round must remain read-only and cover:

- **V1** same Operator/World produces the same View;
- **V2** View and nested records are immutable;
- **V3** construction does not mutate World/Operator;
- **V4** construction does not advance any timer;
- **V5** snapshot/restore produces the same View;
- **V6** strict replay final World produces the same View;
- **V7** stale raw suppression does not affect View;
- **V8** `current_hex=None` ON_BOARD retains correct lifecycle semantics;
- **V9** suppression-paused movement maps PAUSED, not STOPPED, while pending
  mid-segment pause remains MOVING with `pause_pending=true`;
- **V10** Guided recovery exact boundary changes only recovery readiness;
- **V11** Indirect cooldown exact boundary changes only planning readiness;
- **V12** Loitering Munition limited lifecycle is enabled and deterministic;
- **V13** Drone limited lifecycle is NOT_APPLICABLE with `remaining_ms=None`;
- **V14** invalid authoritative combinations map INCONSISTENT/UNKNOWN and do
  not repair World;
- **V15** omitted legacy default fields do not create semantic drift;
- **V16** canonical semantic hash is deterministic and differs from World hash
  by contract.

Existing tests already evidence the underlying sources: Operator/off-map
round-trips, exact Guided and Indirect timer boundaries, suppression pause and
strict replay, Boarding transitions/relations, Loitering finite lifecycle,
Drone no-lifecycle, Human/Agent capability consumption, and strict Replay
snapshot comparison.  They validate the inputs; V1–V16 must validate the future
projection itself.

## 22. Implementation Verification

Phase 1 implements only the internal authoritative projection.  No GREEN frame,
SideObservation, Human/Agent capability, training, or Engine legality API was
changed.

### Implementation boundary

- module: `wargame_native_engine.projection.operator_lifecycle`;
- schema: `native-operator-lifecycle-view/1.0`;
- API: `project_operator_lifecycle(world, operator_id)` and the optional stable
  id-ordered `project_world_operator_lifecycles(world)` batch helper;
- representation: nested `dataclass(frozen=True, slots=True)` value objects,
  stable string enums, and immutable tuples;
- dependency: `core`/`rules` are read by `projection`; Engine does not import
  the View;
- identity: one shared read-only canonical resolver selects lifecycle profile,
  then exact versioned Source mapping.  Engine retains its legacy raw fallback,
  while the View explicitly disables it.

The projected value contains no mutable World/Operator reference, Engine,
callback, cache, RNG, or clock.  It performs no time advance, completion,
movement resume, state cleanup, action generation, target enumeration, LOS,
pathfinding, or damage lookup.

### Semantic and integrity behavior

All duration outputs are integer milliseconds.  Suppression and concealment
read only their typed authorities.  Movement distinguishes STOPPED, MOVING,
STOPPING, PAUSED, BLOCKED, TRANSITIONING, UNKNOWN, and INCONSISTENT; a
suppression requested during an active segment remains MOVING with
`pause_pending=true` until the typed pause boundary.  The full future path is
not projected.

Applicability is explicit: APPLICABLE, NOT_APPLICABLE, UNKNOWN, or MISSING.
Local recovery/cooldown readiness never claims capability or concrete-action
legality.  Boarding uses typed reciprocal relations and the one relevant
ACTIVE World transition without emitting relation identifiers.  Indirect
mission records remain outside this Operator view.

Integrity codes are stable, sorted, and report-only.  Invalid timer, segment,
transition, weapon posture, lifecycle pair, legacy cross-check, and Boarding
relation combinations change only the derived integrity result; projection
never repairs World.

### Semantic hash

`semantic_hash` is SHA-256 over the canonical JSON representation of every
semantic field including schema version and explicit status/applicability,
excluding the hash field itself.  Canonical conversion uses stable enum strings,
integer milliseconds, dataclass field names, and sorted integrity codes.  Raw
attributes, wall clock, RNG, World hash, renderer state, Replay metadata, and
mutable identity are excluded.  It is a derived verification artifact, never
World authority.

### Verification

`tests/test_operator_lifecycle_view.py` covers the contracted V1–V22 surface:

- deterministic equality/hash, nested immutability, no World mutation and no
  timer advancement;
- snapshot/restore and strict action-stream Replay recomputation;
- stale raw suppression, raw concealment, and unrelated metadata isolation;
- ON_BOARD `current_hex=None`, reciprocal relation failure, PAUSED versus
  mid-segment `pause_pending`;
- Guided and Indirect exact recovery boundaries;
- Loitering finite lifecycle and Drone NOT_APPLICABLE lifecycle;
- unknown movement mode, malformed transition/lifecycle pairs, destroyed
  movement, and other inconsistent combinations;
- legacy/default serialization normalization without semantic drift.

Initial focused result: 21 passed.  Related Operator, Movement, Suppression,
Boarding, Guided, Indirect, Loitering, Drone, snapshot, and Replay regression:
241 passed.  Full-suite result: 1745 passed, 10 skipped, 0 failed in 248.80
seconds.

Projection of typed Operator axes is O(1).  The standalone convenience API
performs the necessary O(active Boarding transitions) relation lookup; the
batch API builds that deterministic index once, giving O(1) projection work per
Operator and O(Operators + active Boarding transitions) total cost.  Results are
sorted by Operator id and no persistent projection cache is introduced.

## Final Contract Statement

`AUTHORITATIVE_OPERATOR_LIFECYCLE_VIEW_CONTRACT_READY`

`AUTHORITATIVE_OPERATOR_LIFECYCLE_VIEW_V1_IMPLEMENTED`

`OPERATOR_LIFECYCLE_VIEW_CONTRACT_CLOSED`

`PERSPECTIVE_PROJECTION_REQUIRES_POLICY_FREEZE`

`OPERATOR_LIFECYCLE_VIEW_IMPLEMENTATION_READY`
