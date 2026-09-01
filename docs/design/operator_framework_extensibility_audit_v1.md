# Operator Framework Extensibility Audit v1

## 0. Audit metadata and scope

- Repository: `wargame_native_engine`
- Audit date: 2026-08-26
- Starting branch: `feature/source-perspectives-recorder`
- Starting HEAD: `a46c3b1917e4c13dca6f0d2d076ba9b968d59a37`
- Audit mode: read-only architecture audit; this document is the only file added by this audit.
- Rule character: every implemented rule discussed below is a **Native simplified rule**. It is not described as official or SDK-equivalent behavior.
- Evidence boundary: current working-tree source, configuration, tests, `docs/design`, and `docs/rules`. Existing uncommitted work was treated as authoritative audit input and was not reset, cleaned, discarded, or overwritten.

The audit question is whether the current system can honestly be described as:

```text
one authoritative NativeEngine
        +
a reusable Operator state foundation
        +
Profile/Capability-driven extension points
```

The answer is **PARTIAL**. The transaction, movement, observation, damage, event, RNG, and replay pipelines are substantially reusable. Exact subtype checks are not spread throughout the Engine. However, the runtime is not yet a fully profile-driven Operator framework: several rule decisions still read `operator_type` or curated values in `raw_attributes`, capability families are explicitly enumerated across adapters, and no generic finite-operator-lifecycle contract exists.

This is closest to **MODEL 2: most abilities reuse common pipelines; a small number of domain- or capability-specific rules require explicit integration**.

---

## 1. Current architectural shape

The actual Source-compatible execution path is:

```text
Human / Agent
      |
detached SideObservation + Engine-issued capabilities/actions
      |
SeatOperation / EngineAction
      |
ActionRouter common validation
      |
detached same-pre-state staging
      |
Movement / Visibility / Fire / Boarding / Concealment systems
      |
deterministic aggregate resolution
      |
working WorldState
      |
atomic authoritative commit
      |
Event + GREEN evidence + ordered action stream
      |
strict replay by re-execution
```

Evidence:

- `NativeEngine.step()` enters the exact-millisecond Source runtime at `src/wargame_native_engine/core/engine.py:3288`.
- `_step_source()` clones the authoritative world into `pre_state` before validation at `src/wargame_native_engine/core/engine.py:3540-3554`.
- Common actor/side/deployment/boarding-lock checks belong to `ActionRouter.common_error()` at `src/wargame_native_engine/core/action_router.py:54-81`.
- Direct, guided, indirect, boarding, and concealment intents are staged from the same pre-state at `src/wargame_native_engine/core/engine.py:3592-3642`.
- Effects and time systems are applied in a fixed order at `src/wargame_native_engine/core/engine.py:3660-3692`.
- The authoritative object is updated from one committed state at `src/wargame_native_engine/core/engine.py:3375-3397`.
- Source replay records ordered input actions and expected state hashes, then reconstructs actions and calls the Engine again in `src/wargame_native_engine/io/replay.py:320-435`.

This is a unified Engine. Boarding and indirect fire have capability-specific runtime records, but they do not create controller-owned state mutation loops.

---

## 2. `OperatorState` real state model

`OperatorState` is not a single state enum. It is a mutable authoritative record with multiple mostly orthogonal axes (`src/wargame_native_engine/core/operator.py:11-68`).

```text
OperatorState
├── identity
│   ├── operator_id
│   ├── side
│   ├── operator_type (runtime domain string)
│   └── subtype (compatibility identity string)
├── existence and deployment
│   ├── alive
│   ├── health / max_health
│   ├── blood / max_blood
│   ├── state
│   └── deployed
├── spatial and movement
│   ├── x / y / altitude / heading
│   ├── speed / max_speed / basic_speed / move_range
│   ├── current_hex / cur_pos_millionths
│   ├── move_path / movement_mode / stopped
│   ├── segment_* runtime
│   ├── movement action/sequence/block/stop flags
│   ├── move_to_stop_remaining_ms
│   └── fatigue level/recovery
├── combat
│   ├── weapon_ids
│   ├── attack_range / attack_damage (legacy/default ruleset fields)
│   ├── weapon_locked / weapon_unfolded
│   ├── weapon_cooldown_remaining_ms
│   └── suppression_remaining_ms
├── posture
│   └── concealment_posture
├── relation
│   ├── guidance_profile_id
│   ├── guidance_affiliation_id + evidence
│   ├── boarding_profile_id
│   ├── mounted_on
│   └── passenger_id
├── transition and recovery
│   ├── transition_kind / transition_remaining_ms
│   ├── guided_fire_recovery_remaining_ms
│   └── movement_locked
└── compatibility/raw
    └── raw_attributes
```

### 2.1 Authority classification

| Axis | Current fields | Classification | Notes |
|---|---|---|---|
| Stable identity | `operator_id`, `side` | typed authority | Validated on construction. |
| Runtime domain | `operator_type` | typed authority in practice | Parsed through `_unit_domain()`; unknown domains fail closed in most rule paths. It is a string rather than a typed enum. |
| Source fine identity | `subtype`, raw `type/sub_type`, raw `canonical_type` | mixed | `subtype` is explicit, but canonical rule identity is still often carried in raw compatibility metadata. |
| Existence/damage | `alive`, health/blood, destroyed `state` | typed authority | Zero health/blood forces `alive=False` and `state="destroyed"` at `operator.py:79-90`. |
| Movement runtime | `current_hex`, segment and mode fields | typed authority | Used by the common movement pipeline and serialized when a hex runtime exists. |
| Weapon runtime | weapon list, posture, cooldown | typed authority | Ammunition is an important exception: it remains in `raw_attributes`. |
| Suppression/fatigue | typed counters | typed authority | Advanced by the authoritative clock. |
| Concealment | `concealment_posture` | typed authority | Legacy raw `concealed` is promoted only at construction and projected back for compatibility (`operator.py:149-203`). |
| Guidance | profile, affiliation, recovery, evidence | typed authority | Affiliation without evidence is rejected (`operator.py:105-133`). |
| Boarding | profile and two relation IDs | typed authority | Lifecycle itself lives in `WorldState.boarding_transitions`. |
| Observation contacts | raw `see_enemy_bop_ids` | derived state stored in raw | Recomputed by `_refresh_source_observation()`; not an identity capability. |
| Armor, ammunition, canonical type, visibility height | selected raw/normalized fields | curated compatibility used by rules | These are the largest remaining typed-authority boundary debt. Unknown raw values must not authorize a rule. |
| Operator Registry symbol/name | viewer registry | presentation only | Its own provenance explicitly states it does not authorize adjudication. |

### 2.2 Derived properties

- `position` derives from `x/y` (`operator.py:168-170`).
- `cur_pos` derives from millionths (`operator.py:172-174`).
- `moving` derives from `segment_to_hex is not None` (`operator.py:176-178`).
- `concealed` derives from typed `ConcealmentPosture` (`operator.py:180-182`).
- Legal actions and visible contacts are derived from the current authoritative state and are not independent Operator authority.

### 2.3 `state` string assessment

The `state` string is not the complete lifecycle. The authoritative meaning is the conjunction of `alive`, `deployed`, movement segment fields, transitions, cooldowns, suppression, relations, posture, and mission/transition records. The current validators generally check these axes explicitly. A new Operator must not infer permission from `state` alone.

---

## 3. Does Engine execution dispatch by capability or by unit type?

### 3.1 Classification A — acceptable identity resolution

| Location | Condition | Purpose | Assessment |
|---|---|---|---|
| `rules/guidance.py:114-124` | exact `(type_code, subtype_code)` mapping | Convert curated Source identity to a guidance profile assignment | Acceptable conversion-boundary identity lookup; it does not scan names. |
| `config/source_compat/unit_type_mapping_v1.json` | type code -> `PERSONNEL`/`VEHICLE`/`AIR` | Source domain normalization | Acceptable and fail-closed; subtype mapping remains explicitly `UNRESOLVED`. |
| `config/source_compat/operator_registry_v1.json` | exact `type_subtype` key | Viewer name/symbol/description | Acceptable presentation lookup; not Engine authority. |

### 3.2 Classification B — profile/domain rule dispatch

| Location | Dispatch | Purpose | New Operator impact |
|---|---|---|---|
| `movement/planner.py:96-145` | `AIR`, `PERSONNEL`, otherwise supported ground path | Select movement mode restrictions and fatigue behavior | Existing domains reuse it; a new domain needs an explicit rule. |
| `visibility/profiles.py:21-36` | observer/target `UnitDomain`, plus drone flags | Observation distance profile | Existing domains reuse it; a novel sensor class needs a profile resolver entry. |
| `engine.py:778-805` | `BoardingProfile` carrier/passenger domain sets | BOARD eligibility | Data/profile-driven and reusable. |
| `engine.py:2727` | `WeaponProfile.target_domains` | Fire target-domain legality | Data-driven and reusable. |
| `engine.py:2850-2909` | `GuidanceProfile` scope/status restrictions | Guided capability | Profile-driven; one scope contains an explicit vehicle requirement. |
| `adjudication/direct_fire.py:187-234` | target domain and weapon class | Select curated personnel/vehicle tables | Reasonable rule/table dispatch, not subtype dispatch. |
| `adjudication/indirect_fire.py:821-826` | personnel/vehicle target domain | Select supported target rule | Explicit domain boundary; other domains remain unsupported. |

### 3.3 Classification C — hard-coded platform/domain branches

The following branches are real. None is an exact subtype `if/elif` inside the Engine, but they mean the runtime is not fully profile-driven.

| File / function | Line | Condition | Effect | Reasonable special rule? | Must this location change for a new Operator? |
|---|---:|---|---|---|---|
| `movement/planner.py::validate_operator` | 92-95 | raw canonical type is `ARTILLERY` | Reject MOVE | Current explicit artillery rule, but reads raw canonical identity | Yes for a new immobile canonical class unless moved behind a movement profile. |
| `movement/planner.py::validate_operator` | 96-104 | domain `AIR` | Requires map movement cost mode 3 and `NORMAL` mode | Reasonable domain rule | No for another AIR Operator with the same semantics. |
| `movement/planner.py::validate_operator` | 111-143 | domain `PERSONNEL` | Charge/fatigue/marching restrictions | Reasonable domain rule | No for ordinary personnel; yes for exceptions. |
| `movement/planner.py::_movement_cost_mode` | 264-270 | `VEHICLE`/`PERSONNEL`/`AIR` | Select map cost channel | Necessary domain dispatch | New domain requires an explicit mapping. |
| `movement/planner.py::_friendly_ground_count` | 249-256 | only personnel/vehicle | Apply ground stacking | Reasonable domain boundary | AIR does not need a new branch; a new ground domain would. |
| `movement/runtime.py::advance` | 82, 161, 178 | personnel | Fatigue/suppression movement behavior | Reasonable current rule | Only if new Operator needs nonstandard fatigue/suppression behavior. |
| `engine.py::_concealment_capability_from_pre_state` | 507-511 | AIR/unknown domain | Reject ENTER_CONCEALMENT | Explicit frozen scope | An AIR concealment rule would require an Engine/profile change. |
| `engine.py::_guided_pair_allowed` | 2893 | side-wide guider requires vehicle shooter | Restrict guided pairing | Current profile-scope rule partly hard-coded | A new permitted shooter domain requires code change. |
| `engine.py::_change_source_state` | 3931 | MARCHING requires vehicle | State-change legality | Reasonable domain rule | Existing vehicle Operators reuse it. |
| `engine.py::_change_source_state` | 3957 | CHARGE requires personnel | State-change legality | Reasonable domain rule | Existing personnel Operators reuse it. |
| `engine.py::_build_unit_tactical_overlay` | 3206-3258 | personnel/vehicle only | Restrict tactical overlay queries | Viewer/query feature limitation, not adjudication | An AIR overlay needs query extension, not movement/combat changes. |
| `engine.py::_uses_drone_observation_profile` | 5779-5785 | raw canonical is `DRONE` or `LOITERING_MUNITION` | Select drone observation profile | Existing special sensor policy, but hard-coded/raw-driven | A new platform sharing this profile requires editing this set. |
| `engine.py::_source_visibility_height_m` | 5788-5801 | domain/canonical/altitude | Select supported LOS height category | Necessary map-runtime bridge, partly hard-coded | A new height class needs profile/config support. |
| `adjudication/direct_fire.py::validate` | 67-68 | AIR attacker | Requires hover | Reasonable domain combat rule | Every AIR shooter inherits it; an exception needs explicit policy. |
| `adjudication/direct_fire.py` | 187-276 | personnel vs vehicle / vehicle target | Select attack and damage tables | Necessary current table semantics | New ground Operator can reuse its domain; new target domain needs adjudication tables. |

No Engine-wide giant `if subtype == ...` chain was found. The main architectural debt is instead a set of scattered domain/canonical decisions and the use of curated raw fields for some rule inputs.

---

## 4. Capability architecture

### 4.1 Current generation model

Source `valid_actions()` delegates to `_source_valid_actions()` (`engine.py:2918-2938`, `engine.py:5270-5427`). Its behavior is mostly state- and profile-driven:

| Capability | Current source of authority | New Operator with matching data |
|---|---|---|
| `MOVE` | alive/deployed/not mounted/not locked, current segment state, movement domain/policy, adjacent path validation | Automatically receives it if its domain and map cost mode are supported and `basic_speed` is legal. |
| `STOP_MOVE` | derived `operator.moving` | Automatic. |
| `DIRECT FIRE` (`SHOOT`) | `weapon_ids`, current contacts, scenario `WeaponProfile`, `DirectFireAdjudicator.validate()` | Automatic for an existing weapon/profile/table and supported target domain. |
| `GUIDED FIRE` | typed `GuidanceProfile`, affiliation/scope, supported fire mode, guider visibility, weapon readiness | Automatic after explicit profile/relationship/weapon authority assignment. |
| `INDIRECT FIRE` | separate read-only `get_indirect_fire_capabilities()` plus server-side hex validation | Profile/authority-driven, but intentionally not expanded into one valid action per hex. Human/Agent adapters consume a separate capability surface. |
| `BOARD` | explicit `boarding_profile_id`, `BoardingProfile`, same-state relationship checks | Automatic after explicit profile assignment. |
| `UNLOAD` | typed carrier/passenger relationship plus profile/state checks | Automatic. |
| `ENTER_CONCEALMENT` | generic capability query with hard ground-domain scope | Automatic for supported stopped ground Operators. |
| `WEAPON_LOCK` / `WEAPON_UNFOLD` | stopped + no transition | Generic, but currently generated even without a per-Operator declarative capability flag. This can over-project meaningless posture actions for weaponless Operators. |
| `WAIT` | every controllable deployed Operator | Automatic. |

`ActionRouter.supported_types` explicitly enumerates operation families (`core/action_router.py:30-36`). That is appropriate for closed wire contracts but means a **new capability**, as opposed to a new Operator using an existing capability, must be integrated through Router/Product/Human/Agent/Replay tests.

### 4.2 Human and Agent surfaces

- Human capability projection has an explicit ordered operation list at `viewer/human_capabilities.py:26-38`, then projects only Engine-issued actions/options. It does not infer type legality.
- Boarding projection explicitly consumes only Engine-issued BOARD/UNLOAD tuples (`viewer/human_capabilities.py:225-261`).
- Realtime Agent capability assembly uses detached controller views and Engine/session capability queries (`agents/realtime.py:1413-1465`).
- The random Agent enumerates known operation types, but chooses from the supplied legal action/capability set (`agents/random_seat.py:17-24`, `agents/random_seat.py:150-180`).
- `SeatOperation` and Product operations are operation-family contracts, not unit-family contracts. No `TankMove`, `DroneMove`, or `LoiteringMove` schema exists.

### 4.3 Capability-model verdict

```text
CAPABILITY MODEL: PARTIAL
```

It is closer to data/state-driven than type-driven for existing operations. It is not a single declarative capability registry that the Engine dynamically dispatches. The public `config/capabilities/native_capability_registry_v1.json` is a traceability/status registry, not runtime authorization.

---

## 5. Movement extensibility

### 5.1 Common pipeline

An already deployed Operator with a current hex, positive movement parameters, no blocking transition, and a supported domain/map movement mode can reuse:

```text
EngineAction(MOVE, explicit adjacent path)
  -> ActionRouter common checks
  -> RoutePlanner / SourceMovementPlanner validation
  -> MovementRuntime segment progression
  -> deterministic movement events
  -> authoritative working-state commit
  -> observation refresh
  -> ordered-action replay
```

No path is inferred; the action must carry explicit adjacent hexes. This pipeline does not require per-subtype action classes.

### 5.2 AIR audit

AIR movement is **conditionally implemented**, not universally available:

- `SourceMovementPlanner.validate_operator()` accepts AIR only when the map movement dataset declares cost mode `3`, and only in `NORMAL` mode (`movement/planner.py:96-104`).
- `_movement_cost_mode()` maps AIR/NORMAL to cost mode `3` (`movement/planner.py:264-270`).
- `tests/test_map_runtime_data.py:418` verifies that an explicit AIR cost mode enables authoritative adjacent AIR progress.
- `tests/test_source_movement.py:651` verifies fail-closed `AIR_MOVEMENT_UNSUPPORTED` when that authority is absent.

Therefore a predeployed AIR Operator can use ordinary MOVE **only on a map/runtime package that provides the required AIR movement authority**. Flight altitude changes, climb/descent, autonomous routes, and launch are not part of MOVE.

### 5.3 Verdict

```text
MOVEMENT EXTENSIBILITY: PASS for existing domains with complete map policy
AIR MOVEMENT EXTENSIBILITY: PARTIAL
```

A new Operator in an existing domain normally does not require Movement Engine changes. A new domain or genuinely different movement semantics does.

---

## 6. Visibility extensibility

The common visibility chain is reusable:

```text
Operator domain + profile selection
  -> maximum-distance gate
  -> shared LOS authority
  -> concealment and target-terrain modifiers
  -> per-observer contact IDs
  -> detached side observation
```

Evidence:

- `ObservationProfiles.limit()` selects observer/target limits at `visibility/profiles.py:21-36`.
- `evaluate_visibility()` always uses the shared LOS result and applies concealment/terrain modifiers at `visibility/observation.py:41-90`.
- `_refresh_source_observation()` performs deterministic observer/target iteration and writes derived contacts at `engine.py:5429-5475`.
- LOS traverses the full intervening hex ray and records blocking hexes at `visibility/line_of_sight.py:44-84`.

Current DRONE and LOITERING_MUNITION support is real but narrow:

- `_uses_drone_observation_profile()` recognizes curated raw canonical values `DRONE` and `LOITERING_MUNITION` (`engine.py:5779-5785`).
- Both use the drone-to-ground observation limit in `ObservationProfiles`.
- `_source_visibility_height_m()` assigns 200m to these canonical platforms (`engine.py:5788-5801`).
- `tests/test_source_visibility.py:124` covers both canonical names.

The Engine does not need a second visibility algorithm for these entities. It does, however, need a cleaner identity/profile resolver if future sensor classes should be added without editing Engine sets.

```text
VISIBILITY EXTENSIBILITY: PARTIAL
```

The LOS/evaluation pipeline passes; profile selection remains partly hard-coded and partly raw-driven.

---

## 7. Combat and damage extensibility

### 7.1 Reusable path

For direct fire, the reusable contract is:

```text
weapon_id on Operator
  -> scenario WeaponProfile
  -> visibility, range, target-domain and readiness validation
  -> curated attack-level table
  -> NativeRNG dice/evidence
  -> discrete CombatOutcome
  -> sorted aggregate loss/suppression/resource effects
  -> atomic commit
```

`DirectFireAdjudicator` does not branch on exact subtype. It validates the carried weapon, ammunition, target domain, observation, range, weapon posture/cooldown, movement, AIR hover, and close-combat restriction (`adjudication/direct_fire.py:42-72`). Table selection is based on weapon class and target domain, not unit name.

Guided fire changes target acquisition authority but reuses the direct-fire effect model. Indirect fire has a distinct mission/scatter/effect contract but still commits sorted effects through the authoritative world and Native RNG.

### 7.2 Weapon 76 / loitering-munition evidence

Available:

- Weapon mapping `76 -> loitering_munition` in `config/source_compat/weapon_id_mapping_v1.json:15`.
- Curated mapping evidence in `native_adjudication_tables_final.json:3246-3249`.
- Personnel attack-level table at `native_adjudication_tables_final.json:575-599`.
- Vehicle attack-level table at `native_adjudication_tables_final.json:825-832`.
- Operator display identity `3_7` exists at `config/source_compat/operator_registry_v1.json:241-246`.

Missing before weapon 76 can enter the executable common path:

1. an Engine-authoritative scenario/operator assignment of weapon 76 (the Viewer registry records no executable weapons for `3_7`);
2. an executable `WeaponProfile` with explicit target domains, ammunition key, readiness/unfolding/movement/cooldown policy;
3. a frozen loitering attack policy stating whether the attacking Operator is consumed/destroyed and when;
4. a version/hash evidence boundary for that special attack policy;
5. capability and replay tests proving the same action stream re-executes it.

No evidence requires a new general Damage Engine. A minimal implementation can use a special attack policy to stage attacker consumption while reusing the existing weapon table lookup, `CombatOutcome`/damage effect, deterministic RNG, aggregation, and atomic commit. Whether weapon 76 should be represented as the Operator's own weapon or another exact contract remains an Authority decision; it must not be guessed from its name.

---

## 8. Lifecycle and timer architecture

### 8.1 Existing time-bearing state

| Runtime | Storage / implementation | Clock semantics |
|---|---|---|
| Movement segment | Operator `segment_elapsed_ms`, `segment_traversal_ms` | advanced by `MovementRuntime` |
| Move-to-stop | Operator `move_to_stop_remaining_ms` | movement runtime |
| State/weapon transition | Operator `transition_kind`, `transition_remaining_ms` | movement/source action runtime |
| Weapon cooldown | Operator `weapon_cooldown_remaining_ms` | movement/source time advance |
| Suppression | Operator `suppression_remaining_ms` | movement/source time advance |
| Fatigue recovery | Operator `fatigue_recovery_elapsed_ms` | movement policy/runtime |
| Guided recovery | Operator `guided_fire_recovery_remaining_ms` | source time advance |
| Boarding | `WorldState.boarding_transitions` with `remaining_ms` | separate boarding lifecycle (`core/boarding.py:61-122`, `core/boarding.py:176-216`) |
| Indirect mission flight/active area | `WorldState.indirect_fire_missions` | separate mission lifecycle (`core/indirect_fire.py`) |

All are advanced under the Engine clock in a deterministic order. They are not controller timers.

### 8.2 Missing generic abstraction

```text
GENERIC_OPERATOR_LIFECYCLE_TIMER_NOT_IMPLEMENTED
```

There is no small contract equivalent to:

```text
lifecycle policy
+ remaining authoritative duration
+ deterministic expiry behavior
```

The existing `transition_kind` is not sufficient: it covers named transition semantics and does not represent an Operator's finite existence. Boarding and FireMission records are capability-specific and should not be reused to model a predeployed loitering entity.

A **small, explicit finite-Operator-lifetime contract** is justified for future loitering munitions, temporary unmanned platforms, decoys, and temporary sensors. This recommendation does not imply unifying every cooldown/timer or introducing ECS. Existing combat, movement, boarding, and indirect timers should remain in their current contracts.

---

## 9. Domain and profile architecture

Current domain authority is `UnitDomain` (`PERSONNEL`, `VEHICLE`, `AIR`, `UNKNOWN`) derived from `OperatorState.operator_type`.

| Concern | Main rule input | Current shape |
|---|---|---|
| Movement | domain + movement mode/map cost authority + some raw canonical identity | mixed |
| Visibility | domain pair + drone canonical flag + height category | mixed |
| Targetability | weapon target domains + observation + target alive/deployed | profile-driven within supported domains |
| Concealment | typed posture + hard ground-domain scope | mixed |
| Boarding | explicit Operator profile + domain sets + typed relation | profile-driven |
| Direct firing | weapon profile + domain/table selection | strongly profile/table-driven |
| Guided firing | guidance profile + typed affiliation + weapon fire modes | profile-driven with one hard domain restriction |
| Indirect firing | versioned authority profiles/policies | strongly profile-driven |
| Destruction | common alive/health/blood state and effect aggregation | reusable; boarding adds an explicit carrier-passenger cascade |

Domain is a legitimate high-level rules entry, not merely presentation. Exact subtype is rarely inspected by Engine execution. The remaining undesirable path is `identity -> raw canonical set -> rule`, most visibly in artillery movement and drone/loitering visibility profile selection.

---

## 10. Relation extensibility

### 10.1 Relation classes found

| Relation | Classification | Evidence |
|---|---|---|
| `mounted_on` / `passenger_id` | Engine typed authority | `OperatorState`; validated mutually and persisted. |
| `guidance_affiliation_id` + evidence | Engine typed authority | `OperatorState`; requires `GuidanceAffiliationEvidence`. |
| `car_id`, `launcher_id`, `launch_ids`, `owner`, legacy `passenger_ids` | Source compatibility/raw evidence | Preserved by Source models/mapping; raw fields do not by themselves authorize a Native rule. |
| Operator registry relationships/symbols | Viewer/presentation | Must not authorize Engine behavior. |

There is no generic typed `OperatorRelation` store. That is not currently a defect requiring a super-relation system: boarding and guidance have different invariants and correctly use focused typed fields/contracts.

For a future launched loitering entity, an optional typed `launcher_id` plus provenance/evidence is a proportionate minimum. Raw Source `launcher`/`launch_ids` must not be promoted automatically, and v1 predeployed loitering Operators do not need the relation to move, observe, attack, expire, or replay.

---

## 11. Replay extensibility

Strict Source replay is action-stream driven:

1. scenario creates the initial WorldState;
2. the recorder stores explicit `ordered_actions`, delta time, and expected state hash;
3. replay reconstructs each `EngineAction` using `EngineAction.from_dict()`;
4. a new Engine re-executes each tick;
5. strict verification compares ordered inputs, actions/events, state hashes, final state, and GREEN frame hashes (`io/replay.py:320-469`).

There is no replay branch that restores a Tank, Infantry, Drone, or Loitering Munition by subtype. `WorldState.to_dict()/from_dict()` serializes Operators generically and uses deterministic ordering (`core/world.py:75-170`).

Consequences:

- A new Operator present in the initial scenario and using existing MOVE/FIRE/WAIT/destroy semantics is naturally representable.
- A new fine identity alone does not require a Replay schema change.
- A genuinely new action, lifecycle expiry policy, or attack policy does require explicit semantic version/hash evidence and strict replay coverage. Replay must re-execute it; it must not restore inferred results.
- Unknown `ActionType` values still fail at `EngineAction.from_dict()` (`core/action.py:50-61`), which is correct for an unknown wire contract.

```text
REPLAY NEW-OPERATOR EXTENSIBILITY: PASS for existing actions and state axes
REPLAY NEW-CAPABILITY EXTENSIBILITY: PARTIAL; explicit contract/version integration required
```

---

## 12. RED / BLUE / GREEN and Agent isolation

### 12.1 Working isolation

- `NativeEngine.observe()` returns friendlies and visible enemies as detached `OperatorObservation` values; visible enemies exclude `raw_attributes` (`engine.py:358-379`).
- RED/BLUE source frames include only friendly plus currently visible enemy IDs; friendly relations are cleaned against included IDs (`observations/perspective.py:164-219`, `264-290`).
- Opponent actions and valid actions are filtered by side and referenced visible IDs (`perspective.py:292-319`).
- Judge information is dropped unless all referenced Operators are allowed, and Native RNG/table/hash evidence is stripped (`perspective.py:321-357`).
- GREEN is a detached complete referee projection; `PerspectiveProjector` hashes the input before/after to reject mutation (`perspective.py:135-162`).
- Agents consume controller observations and legal actions/capabilities through the session/gateway path. No reviewed Agent path receives mutable `WorldState`.

### 12.2 New-Operator projection risk

`source_perspective_policy_v1.json` says visible enemies are rebuilt from safe fields and unknown fields must not be copied from GREEN. Top-level visible enemy records correctly use `type=-1`, `sub_type=-1`, empty weapons and relations. However, `_visible_enemy()` currently embeds exact `operator_type`, `subtype`, health, and max health in `extra.observation` (`perspective.py:232-262`), while these fields are not in the configured visible-enemy safe-field allowlist (`config/source_compat/source_perspective_policy_v1.json:44-51`).

This is a **P1 projection-policy inconsistency**. It does not expose raw attributes or Native evidence, but a newly introduced fine subtype such as loitering munition can become identifiable through `extra.observation` despite the top-level UNKNOWN projection. It should be resolved at the observation projection boundary before treating fine new subtype identity as enemy-safe public data. It does not require an Engine or Operator redesign.

Any new private runtime—lifetime remaining, launcher relation, ammunition, autonomous target state—must default to GREEN/own-side only and be explicitly side-projected. Adding it to `OperatorState` alone must never imply enemy visibility.

---

## 13. Human and Agent reuse

If a new Operator receives an existing MOVE, SHOOT/FIRE, WAIT, posture, or relationship capability from Engine authority:

- Human Web can reuse the existing `SeatOperation`/Product operation and capability panel for that operation type.
- Agent controllers can reuse the same detached legal action or capability option.
- The command gateway continues to validate observation versions/action IDs and submit to `RealtimeAgentSession`/Engine.
- Replay sees the same generic `EngineAction`, not an Operator-specific action.

No `LoiteringMove`, `TankMove`, or `DroneMove` is necessary or desirable.

A genuinely new capability (launching an entity, laying mines, electronic warfare) still requires a new frozen operation/action contract plus Product/Human/Agent projection. That is expected behavior, not evidence that every **Operator** needs a special path.

---

## 14. Config/profile data-driving matrix

| Capability/concern | Data-driven | Engine hard-coded | Assessment |
|---|---:|---:|---|
| Identity | exact Source type->domain mapping; exact Viewer type/subtype registry | runtime domain stored as string; selected canonical raw checks | Mixed. Viewer identity is not authority. |
| Movement | terrain/mode/slope/road/time policy | domain branches, artillery raw canonical prohibition, AIR mode mapping | Mixed, reusable inside existing domains. |
| Visibility | observation limit object + map/LOS data | drone/loiter canonical set and supported height categories | Mixed; evaluator itself is reusable. |
| Combat | weapon profiles, weapon mapping, sole curated adjudication table source | domain/table branch and AIR hover rule | Mostly data/profile-driven. |
| Concealment | typed posture | ground-only scope and release hooks in action paths | Mixed. |
| Boarding | versioned profile with explicit assignment | lifecycle application code | Profile-driven authority with necessary capability code. |
| Guidance | versioned guidance profiles, exact source mapping, weapon fire modes | side-wide vehicle shooter restriction | Mostly profile-driven. |
| Indirect fire | versioned authority, ammunition, observation, damage, execution policies | mission lifecycle implementation | Strongly profile-driven. |
| Lifecycle | boarding/indirect policies separately | each runtime owns its timers; no finite Operator lifetime | Capability-specific, not generic. |
| Replay | scenario/ruleset/seed/version/hash + generic ordered actions | known ActionType and capability semantics are explicit code | Generic for new Operators; explicit for new capabilities. |
| Capability registry | status/traceability data | runtime does not dispatch from it | Documentation/benchmark authority only. |

---

## 15. Loitering munition as an architecture probe

Audit assumption for this section only: the v1 entity already exists at scenario initialization as a controllable AIR Operator; launch is intentionally out of scope.

### 15.1 Reuse matrix

| Capability | Reuse degree | What is still needed |
|---|---|---|
| Operator identity | Profile/config only | Exact Engine-authoritative identity assignment for type 3/subtype 7; Viewer entry already exists but cannot authorize rules. |
| Initial WorldState | 100% reusable | A scenario Operator record with explicit domain/runtime values. |
| MOVE | Reusable with condition | Map runtime must provide AIR cost mode 3; speed/path policy must be explicit. Existing rules prose says 8 seconds/hex, but conversion to executable movement authority must be curated. |
| Visibility | Mostly reusable | Current code already treats canonical LOITERING_MUNITION as the drone profile at 200m. Replace/raw-bound that identity through an authoritative profile resolver over time; no new LOS algorithm. |
| Targetability by enemies | Reusable within current AIR-capable weapons/observation rules | Exact enemy weapon target-domain profiles and AIR observation policy must exist. Current ground/direct damage supports personnel/vehicle targets, not arbitrary AIR damage tables. |
| Attack target acquisition | Mostly reusable | Explicit weapon 76 profile and current-contact validation; no special target scanner. |
| Damage to ground target | Mostly reusable | Weapon 76 mapping/tables exist. Freeze attack policy and reuse current discrete effect/atomic commit path. |
| Attacker consumption on attack | Small Engine extension | A typed, versioned self-consumption/attack policy staged in the same fire batch; do not create a second damage engine. |
| Limited lifetime | Missing common extension | Minimal finite-Operator-lifetime authority and deterministic expiry/death event. |
| Death/destruction state | 100% reusable | Expiry and attack policy must invoke the existing destruction state/effect semantics atomically. |
| Launcher relation | Not needed for assumed v1 | Later add optional typed `launcher_id` + evidence; never authorize from raw `launcher`. |
| Replay | Reusable after semantics are explicit | Existing MOVE/FIRE inputs work; lifecycle/attack policy need version/hash and event/hash regression. |
| Human | Reusable for existing MOVE/FIRE | Capability projection must not expose unsafe enemy subtype/private lifetime/launcher data. |
| Agent | Reusable for existing MOVE/FIRE | Agent receives detached observation/capability only. |

### 15.2 What can be reused now

100% reusable:

- generic scenario Operator creation and authoritative WorldState storage;
- explicit adjacent MOVE action shape and movement staging/commit, when AIR map authority exists;
- same-pre-state transaction, deterministic event ordering, NativeRNG, state hash, and strict replay machinery;
- RED/BLUE contact filtering structure and GREEN evidence structure;
- existing Human/Agent MOVE/FIRE operation shapes;
- common destroyed/alive state application.

Profile/config work only:

- exact identity/domain assignment;
- movement speed/height authority;
- visibility profile selection (current behavior already exists, but should stop depending directly on raw canonical values);
- weapon 76 executable profile and target domains;
- table mapping/evidence references.

Small Engine extensions:

- finite Operator lifetime/expiry;
- a loitering attack policy that stages self-consumption in the existing fire effect transaction;
- side-safe projection rules for any new private runtime.

Not appropriate now:

- launch/deployment from a carrier;
- autonomous search/target selection;
- inferred launcher relationships;
- new Damage Engine;
- a Mission abstraction solely to work around missing Operator lifecycle.

---

## 16. New-Operator cost pressure test

| Probe | Existing reusable modules | Config/profile work | Code that truly needs change |
|---|---|---|---|
| A. Ordinary reconnaissance UAV (`MOVE + Visibility`) | Operator/World, AIR MOVE pipeline, LOS/evaluator, observation, Human/Agent MOVE, replay | identity, AIR movement cost/speed, sensor profile/height | Ideally only profile resolution; current special drone selection may require adding a canonical value in Engine if it is not `DRONE`. |
| B. Predeployed loitering munition (`MOVE + Visibility + Attack + Lifetime`) | all of A plus direct fire damage/effects, NativeRNG, commit, replay/action structures | weapon 76 executable profile, attack/lifetime authority | small finite-lifecycle extension and self-consuming attack policy; side projection gate. |
| C. Engineering vehicle (`MOVE + future special capability`) | vehicle movement, visibility, targetability, destruction, Human/Agent/replay for existing actions | exact identity and any movement/visibility profiles | Only the genuinely new engineering capability needs ActionRouter/Product/Human/Agent/event/replay integration. The vehicle itself does not. |

This pressure test does **not** show that every new Operator requires edits to Engine, Observation, Replay, Human, Agent, and Viewer. Existing capabilities are broadly reusable. New rule families appropriately require explicit contracts at those boundaries.

---

## 17. Architecture smells and severity

### P0

None found in the reviewed extension paths:

- no Agent/Human mutable `WorldState` access;
- no Viewer registry authorizing Engine rules;
- no per-subtype replay state restoration;
- no controller-owned adjudication loop;
- no replay inference of missing actions.

### P1

1. **Visible-enemy safe-field policy inconsistency.** `PerspectiveProjector._visible_enemy()` places exact `operator_type`, `subtype`, health, and max health under `extra.observation`, even though the configured safe-field allowlist excludes them and top-level identity is UNKNOWN. A fine new Operator can therefore disclose more classification than intended. Location: `observations/perspective.py:232-262`; policy: `source_perspective_policy_v1.json:44-51`.
2. **Rule-relevant compatibility data remains in `raw_attributes`.** Canonical platform identity, armor, ammunition, visibility height, contacts, and a legacy passenger list are read from raw/normalized data in selected Engine paths (`engine.py:1887-1900`, `4852-4938`, `5314-5317`, `5779-5801`; `movement/planner.py:92-95`). These reads are generally curated and fail closed, but the boundary is not a uniform typed Profile authority. New Operators increase the risk of raw evidence accidentally becoming authorization.
3. **`GENERIC_OPERATOR_LIFECYCLE_TIMER_NOT_IMPLEMENTED`.** A limited-life Operator currently needs a bespoke timer path. This is the only clear common runtime abstraction blocking a clean predeployed loitering v1.

### P2

1. Platform-profile selection for DRONE/LOITERING_MUNITION is a hard-coded raw canonical set rather than an Engine-owned profile assignment.
2. Operation families are duplicated as explicit lists/switches across ActionRouter, Product, Human UI, and Agent adapters. Existing operations remain reusable; only a new capability pays this cost.
3. Weapon lock/unfold capability generation is state-driven but lacks an explicit per-Operator capability flag, so weaponless/new Operators may receive presentation-noise actions.
4. `state` remains a free string beside many typed axes. Cross-axis validity depends on validators rather than a single formal product-state contract.
5. AIR movement depends on map movement mode 3 and supports only current NORMAL semantics; absent authority correctly rejects it.

### P3

1. Viewer icon/symbol mapping contains subtype presentation logic. This is acceptable because it is downstream and non-authoritative, but its registry name can be mistaken for Engine authority.
2. Capability Registry is a release/traceability registry, not runtime capability dispatch. Documentation should keep that distinction explicit.

---

## 18. Minimal improvement direction — no broad refactor

The current architecture already reuses roughly the important 80–90% of execution infrastructure for Operators using established actions. Do **not** rewrite it as ECS, replace WorldState, introduce a universal state machine, or force all timers into one system.

The highest-value incremental boundaries are:

1. Resolve curated identity into a narrow Engine-owned Operator rule profile before movement/visibility/combat read it; preserve unknown values as metadata and fail closed.
2. Add one small finite-Operator-lifetime/expiry contract; do not absorb weapon cooldown, suppression, boarding, or indirect mission timers.
3. Add only focused typed relations when a rule needs them, with evidence and side-safe projection; do not create a universal relation graph.

---

# Final Verdict

## A. Current overall architecture

```text
PARTIAL
```

It is reasonable to describe the project as a **unified authoritative Native Engine with a reusable Operator state foundation**. It is not yet accurate to describe it as a fully generic, purely Profile/Capability-driven Operator framework. Existing action families are highly reusable; profile selection and limited lifecycle remain incomplete.

## B. New-Operator integration model

```text
MODEL 2:
大部分能力复用，少数能力特判
```

The code is materially better than MODEL 1: exact subtype branches are not proliferating through core execution. It has not reached MODEL 3 because domain/canonical branches and explicit operation-family adapters remain.

## C. Recommended current identity for loitering munition

```text
Operator
```

Under the audited v1 assumption—already present at initialization, independently movable/observable/controllable, attackable, and destructible—it fits `OperatorState`, not FireMission. A future launch process may create/activate that Operator through a dedicated capability, but this does not make the airborne entity itself a Mission.

## D. Minimal loitering-munition extension points

1. Versioned Engine-authoritative Operator/weapon 76 profile and explicit scenario assignment.
2. Small finite Operator lifetime with deterministic expiry/destruction.
3. Versioned attack/self-consumption policy that reuses the existing damage/effect/RNG/atomic commit path.
4. Side-safe projection for lifetime and future launcher relation.

Launch, autonomous attack, and launcher-destruction cascade are outside the assumed predeployed v1 and should remain later contracts.

## E. Most valuable common abstractions for future Operators

### P1 — Narrow Engine-owned Operator rule-profile resolution

Centralize only the curated facts already used by rules—domain, movement profile, visibility profile/height, targetability/armor/ammunition references—and prevent unknown raw compatibility fields from authorizing behavior.

### P2 — Minimal finite-Operator-lifetime policy

Provide deterministic remaining duration and explicit expiry behavior for temporary Operators. Do not unify all existing timers.

### P3 — Focused typed launcher relation with projection evidence

Add it only when launch rules are implemented. Reuse the guidance/boarding pattern: typed ID, explicit evidence, validation, snapshot/hash, and RED/BLUE scrubbing. Do not add a universal relation framework.

## F. Is it appropriate to implement loitering munition now?

```text
YES — for the explicitly scoped predeployed, player/Agent-controlled v1.
```

There is no core-architecture rewrite blocker. The recommended order is:

```text
Authority freeze
  -> Engine-owned Operator/weapon profile
  -> finite lifecycle
  -> weapon 76 attack/self-consumption policy
  -> existing MOVE/Visibility/FIRE capability validation
  -> Replay semantics/version/hash review
  -> RED/BLUE projection safety
  -> Human/Agent exposure
  -> Demo/Benchmark
```

Launching, carrier capacity, autonomous search, and launcher-death coupling should not be folded into this first v1.

---

## 19. Focused verification

The audit used existing tests only; no audit-specific test or business code was added. No full test suite was required for this read-only architecture review.

Command:

```bash
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src \
pytest -p no:cacheprovider -q \
  tests/test_world.py \
  tests/test_source_movement.py \
  tests/test_map_runtime_data.py \
  tests/test_source_visibility.py \
  tests/test_direct_fire_adjudication.py \
  tests/test_guidance_authority.py \
  tests/test_operator_registry.py \
  tests/test_source_perspectives.py \
  tests/test_agents.py \
  tests/test_fire_intent.py \
  tests/test_source_replay_recorder.py
```

Result:

```text
217 passed in 7.30s
```

Focused coverage:

- Operator serialization and WorldState round-trip;
- Source movement including AIR fail-closed and explicit AIR map mode;
- visibility/DRONE/LOITERING profile behavior;
- direct and guided fire contracts;
- side perspective isolation;
- Agent detached observation/action behavior;
- Source strict replay recording/re-execution.

## 20. Repository-state note

The worktree was already materially dirty before this audit. This audit deliberately preserves all existing tracked and untracked changes. The only path introduced by this audit is:

```text
docs/design/operator_framework_extensibility_audit_v1.md
```

Final `git status --short` and `git diff --check` results are reported in the handoff rather than claiming a clean worktree.
