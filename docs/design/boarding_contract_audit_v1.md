# Boarding / Embarkation Authority Audit v1

Status: `AUDIT_COMPLETE_SUPERSEDED_BY_MINIMAL_V1`
Rules label: `Native simplified rules`
BOARDING_ENGINE_STATUS: `MINIMAL_V1_IMPLEMENTED`
BOARDING_EXECUTION_ENABLED: `true`

> Implementation note (2026-08-25): the later user-frozen Minimal Boarding v1
> contract explicitly resolves a narrow subset of the gaps recorded by this
> audit. It authorizes only an explicitly assigned, single-passenger ground
> profile, same-hex BOARD, carrier-hex UNLOAD, and a 75-second exclusive timer.
> Capacity systems, type/subtype inference, helicopters, destruction/escape,
> passenger observation inheritance, and interruption rules remain outside v1
> and fail closed. The evidence inventory below is retained as historical
> design rationale rather than a claim about current implementation status.

This audit freezes evidence and implementation blockers only. It does not
authorize a BOARD/UNLOAD action, infer a carrier from a name, or promote Source
compatibility fields into Native authority.

## Scope and conclusion

The repository contains meaningful boarding rules and historical state
evidence, but not a complete executable authority contract. The following are
machine-supported facts today:

- general embark and disembark duration is 75 seconds;
- the two units must be co-located and stopped before the operation starts;
- suppression, marching, move-to-stop transition and other simultaneous work
  constrain the operation;
- carried personnel do not inherit vehicle suppression, but cannot disembark
  while the vehicle is suppressed;
- vehicle losses propagate by the same discrete loss count to carried
  personnel;
- transport-helicopter loading has additional open-terrain, ultra-low-altitude,
  stopped, unsuppressed and empty-carrier conditions.

Production implementation is blocked because carrier/passenger eligibility,
ground-carrier capacity, authoritative Source-to-Native relationship mapping,
ground interruption completion semantics, disembark placement, observation,
objective occupancy and several destruction cases are not fully specified.

## Rule Evidence

| Evidence | Location | Source summary | Machine interpretation | Authority status |
|---|---|---|---|---|
| General start conditions | `docs/rules/推演规则_完整纯文本_Final.md:76-81` | Co-located unit and vehicle must both be stopped; each direction takes 75 seconds; neither may start suppressed. | `same_hex=true`, `passenger_stopped=true`, `carrier_stopped=true`, `duration_ms=75000`, pre-start suppression blockers. | PARTIAL: eligibility and interruption completion remain UNKNOWN. |
| Move-to-stop boundary | `docs/rules/推演规则_完整纯文本_Final.md:44-47` | Boarding waits for the movement-to-stopped transition to finish. | Both participants require no active movement segment or move-to-stop transition. | DETERMINED. |
| Marching exclusion | `docs/rules/推演规则_完整纯文本_Final.md:57-63` | Marching vehicles cannot embark/disembark. | `carrier.movement_mode != MARCHING`. | DETERMINED. |
| Operation exclusivity | `docs/rules/推演规则_完整纯文本_Final.md:78` | Loading/unloading takes 75 seconds, permits no other command, and cannot be voluntarily cancelled. | A transition reserves both participants; user cancellation is forbidden. | DETERMINED for voluntary cancellation; automatic interruption handling is PARTIAL. |
| Suppression effects | `docs/rules/推演规则_完整纯文本_Final.md:151-156` | Suppressed vehicles prevent boarding/disembarking; carried personnel do not inherit suppression but cannot disembark. | Suppression is checked on start and during transition; carrier suppression blocks disembark. | DETERMINED except progress reset/resume semantics. |
| Close combat interruption | `docs/rules/推演规则_完整纯文本_Final.md:220-226` | Close combat interrupts boarding transitions; un-dismounted personnel cannot fire. | Entering close combat interrupts an in-progress transition; EMBARKED passengers have no direct-fire capability. | PARTIAL: rollback/progress semantics are UNKNOWN. |
| Transport helicopter loading | `docs/rules/推演规则_完整纯文本_Final.md:228-236` | Infantry can load onto a transport helicopter in open terrain at ultra-low altitude; both are stopped and unsuppressed; loading/unloading takes 75 seconds. | Dedicated air-transport profile with personnel domain, terrain/altitude/status predicates. | PARTIAL: exact source code mappings and altitude code are not curated machine authority. |
| Helicopter capacity | `docs/rules/推演规则_完整纯文本_Final.md:233-235` | Passenger squad count may not exceed carrying units; loading requires an empty helicopter. | Capacity is a discrete squad-count constraint and `empty_before_embark=true`. | BLOCKED: the authoritative capacity value and field mapping are UNKNOWN. |
| Helicopter damage and interruption | `docs/rules/推演规则_完整纯文本_Final.md:235-236` | Carrier and loaded unit take equal loss; suppression during loading/unloading causes failure and requires a fresh operation. | Equal discrete loss propagation; transition reset on fire-induced suppression for this profile. | DETERMINED for helicopter profile only. |
| Aggregation interaction | `docs/rules/推演规则_完整纯文本_Final.md:238-245` | Passenger and vehicle aggregate separately; boarding/suppression blocks aggregation. | Carrier and passenger remain separate operator identities and strength ledgers. | DETERMINED representation constraint; aggregation is outside Boarding v1. |
| Native timing table | `config/adjudication/source_tables_v1/native_adjudication_tables_final.json:118-131` | Machine table records boarding and disembarking as 75 seconds. | Sole current machine authority for both durations. | DETERMINED. |
| Native suppression table | `config/adjudication/source_tables_v1/native_adjudication_tables_final.json:133-149` | Carried personnel do not inherit vehicle suppression and cannot disembark while it persists. | Explicit suppression policy facts. | DETERMINED. |
| Source relationship fields | `config/adjudication/source_tables_v1/native_adjudication_tables_final.json:3048-3069` | `passenger_ids`, `car`, and `on_board` are aligned historical relationship fields. | Read-only source evidence; not permission to create or alter a relationship. | EVIDENCE_ONLY. |
| Source importer | `src/wargame_native_engine/compat/source_frame/importer.py:185-201` | Imports `passenger_ids`, `car`, `on_board`, and passenger collection membership. | Can reconstruct a historical relation-shaped snapshot. | EVIDENCE_ONLY. |
| Source-to-World conversion | `src/wargame_native_engine/compat/source_frame/importer.py:90-125` | Preserves source passenger membership as raw evidence and maps `on_board` only to `deployed=False`. | Native `OperatorState` does not receive an authoritative carrier relationship. | IMPLEMENTATION_GAP. |
| Capacity supplement | `examples/migration_report_small/unmapped_fields.json` | `loading_capacity` is explicitly preserved in raw attributes only. | It cannot authorize capacity without curated mapping and provenance. | UNKNOWN/EVIDENCE_ONLY. |
| Side information policy | `config/source_compat/source_perspective_policy_v1.json` | Passenger/carrier relation IDs are filtered to visible/allowed IDs. | A visible carrier must not reveal hidden passenger identities. | DETERMINED information-boundary invariant. |
| Existing damage handling | `src/wargame_native_engine/core/engine.py:3784-3824` and `:3898-3934` | Direct-fire staging reads raw passenger IDs and applies the vehicle loss count to those operators. | Existing combat compatibility behavior is not a boarding state-transition implementation. | IMPLEMENTED_DAMAGE_ONLY. |

`docs/rules/Native兵棋裁决知识库_Final.md:82-107,145-150,331-339`
corroborates the 75-second duration, relationship fields, suppression behavior
and equal discrete passenger losses. It is a Phase 4A guide; the curated JSON
remains the sole runtime table source.

## 2.1 Unit relationship

### Determined

- Boarding is a relation between two distinct operator identities.
- The evidence consistently describes personnel as passengers and a vehicle as
  carrier.
- Transport helicopter loading is explicitly restricted to infantry.
- Historical frames can express both directions of the relation:
  `carrier.passenger_ids` and `passenger.car` plus `passenger.on_board`.

### UNKNOWN / blocked

- Which exact ground `type_code/subtype_code` pairs are authorized carriers.
- Which exact personnel subtypes may use each ground carrier.
- Whether APC, IFV, pickup or another vehicle class has different rules.
- Whether equipment or one vehicle may be carried by another vehicle.
- Whether `car`, `passenger_ids`, `loading_capacity`,
  `valid_passenger_types`, or `max_passenger_nums` is authoritative for a new
  Native scenario. All are compatibility evidence until curated.
- Whether the Source relation is guaranteed bidirectionally consistent in all
  historical frames.

Names and Viewer `icon_key`/Operator Registry symbols are display metadata and
must never authorize boarding.

## 2.2 State changes

Current Native `OperatorState` has no `carrier_id`, passenger ledger,
boarding phase or boarding timer. Source conversion reduces `on_board` to
`deployed=False` and preserves the rest under raw metadata.

A future Native state must distinguish at least:

```text
INDEPENDENT -> EMBARKING -> EMBARKED -> DISEMBARKING -> INDEPENDENT
```

The following remain UNKNOWN and must be frozen before implementation:

- whether an EMBARKED passenger retains `current_hex`, uses `null`, or derives
  position from the carrier;
- whether it participates in spatial stacking;
- whether it has a separately observable contact while embarked;
- whether it contributes to objective control;
- whether carrier destruction automatically destroys, unloads, or strands any
  surviving passenger after equal-loss application.

The implementation must not equate `deployed=False` with EMBARKED: deployment
and carriage are different semantics.

## 2.3 Capacity

Known for transport helicopters: capacity compares passenger squad count with
the carrier's carrying-unit count, and loading starts only when empty.

UNKNOWN:

- all numeric capacities;
- ground-carrier capacity metric;
- whether capacity counts operators, squads (`blood`), weight, or slots per
  ground profile;
- whether wounded/reduced units consume current or maximum strength capacity;
- how simultaneous embark requests compete for remaining capacity.

No default such as “10 personnel” is permitted. `loading_capacity` remains raw
evidence until a versioned mapping defines its unit and eligible carrier keys.

## 2.4 Time and interruption

Determined:

- embark duration: `75000 ms`;
- disembark duration: `75000 ms`;
- no voluntary cancellation;
- participants cannot execute another command during the transition;
- transition cannot start until fully stopped and unsuppressed;
- marching is prohibited;
- close combat interrupts the transition;
- helicopter suppression during load/unload fails the operation and requires
  a new one.

UNKNOWN:

- for general ground boarding, whether interruption resets progress, pauses it,
  or leaves a recoverable transition;
- exact same-tick conflict precedence between suppression, destruction,
  movement start and transition completion;
- whether a failed ground operation has a cooldown.

## 2.5 Movement and objective participation

The purpose of carriage and historical relation shape imply carrier-associated
movement, but the exact authoritative position representation is not stated.
Therefore “passenger follows carrier” is a required future semantic decision,
not yet executable authority.

An EMBARKED passenger must not receive independent MOVE actions. Whether it
occupies a hex for stacking, close combat or objective control is UNKNOWN.

## 2.6 Combat and observation

Determined:

- an undismounted passenger cannot fire in close combat;
- vehicle discrete loss `N` causes carried personnel discrete loss `N`;
- carried personnel do not inherit vehicle suppression;
- transport-helicopter and passenger are adjudicated as a combined target for
  equal losses under the stated helicopter conditions.

UNKNOWN:

- whether an embarked passenger may use any non-fire capability;
- whether it may be selected as an independent target;
- visibility/contact projection of passenger versus carrier;
- non-loss effects when the carrier is destroyed;
- whether all direct, guided and indirect damage modes use identical passenger
  propagation outside the explicit rule cases.

Current Direct Fire code already propagates losses from raw passenger evidence,
but that is not proof that runtime boarding exists.

## 2.7 Disembark

Determined:

- disembark is an explicit timed operation, not a MOVE side effect;
- it requires stopped/unsuppressed conditions and excludes other work;
- carrier suppression forbids it;
- transport-helicopter unloading requires open terrain and ultra-low altitude.

UNKNOWN:

- whether ground passengers disembark only into the carrier hex or may select
  an adjacent hex;
- stacking validation at completion;
- what happens when the selected/computed hex becomes invalid during 75 seconds;
- whether passenger state after completion is stopped, transitioning, or may
  move immediately;
- whether enemy presence prohibits or interrupts completion.

## Current architecture gap

| Layer | Current evidence | Boarding status |
|---|---|---|
| Action | `ActionType` has MOVE/FIRE/transitions but no BOARD/UNLOAD. | NOT_IMPLEMENTED |
| Operator | No authoritative carrier relation, boarding phase or timer. | NOT_IMPLEMENTED |
| World | One operator map can preserve identities, but has no relationship invariants. | NOT_IMPLEMENTED |
| Engine | Reads raw passenger IDs only for direct-fire loss propagation. | DAMAGE_COMPAT_ONLY |
| Observation | Source side projection filters relationship IDs and protects hidden passengers. | PARTIAL_READ_ONLY |
| Human/Agent | No Native capability or operation exists. | NOT_IMPLEMENTED |
| Replay | Can preserve historical Source frames but cannot replay a Native boarding intent. | NOT_IMPLEMENTED |

## Implementation blockers

1. A versioned carrier/passenger eligibility registry keyed by numeric identity,
   not by name.
2. A versioned capacity metric and value for every executable carrier profile.
3. Native relationship and lifecycle representation, including position and
   spatial participation.
4. Ground interruption and completion policies.
5. Disembark target-hex and invalidation policy.
6. Passenger observation/targeting and objective-control policy.
7. Carrier-destruction resolution after equal discrete losses.

The Minimal Boarding v1 authority resolves eligibility through the explicit
typed `boarding_profile_id` field. Operators without such an assignment remain
ineligible; raw Source fields and display metadata never authorize execution.

## Capability Registry pre-analysis

Recommended future record, not added to the Registry in this phase:

```text
boarding:
  ENGINE: NOT_IMPLEMENTED
  HUMAN: NOT_IMPLEMENTED
  AGENT: NOT_IMPLEMENTED
  REPLAY: NOT_IMPLEMENTED
  DEMO: PENDING
```

## Audit verdict

The repository has enough evidence to design a contract and freeze several
invariants, but not enough authority to implement general boarding safely.
Transport-helicopter boarding is better specified than ground boarding, yet is
still blocked by numeric capacity and type/altitude/terrain mapping authority.
