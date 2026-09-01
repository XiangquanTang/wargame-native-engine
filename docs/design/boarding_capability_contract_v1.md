# Boarding Capability Contract v1 (Draft)

Contract status: `MINIMAL_V1_IMPLEMENTED`
BOARDING_ENGINE_STATUS: `MINIMAL_V1_IMPLEMENTED`
BOARDING_EXECUTION_ENABLED: `true`
Authority schema: `native-boarding-profiles/v1.0`

The executable subset is intentionally narrower than the original draft:

- carrier eligibility requires explicit `boarding_profile_id` authority;
- the profile permits one PERSONNEL passenger on one VEHICLE carrier;
- BOARD is passenger-initiated and requires co-location and stopped actors;
- BOARD immediately establishes `ON_BOARD`/`mounted_on`/`passenger_id`;
- BOARD and UNLOAD lock carrier operations for 75,000 ms;
- UNLOAD is carrier-initiated and completes into the carrier hex;
- unknown profiles and all advanced transport semantics fail closed.

The UNKNOWN fields and proposed richer models below remain the post-v1 roadmap;
they do not weaken or implicitly extend the executable subset.

This document began as a future-boundary draft. The opening section now records
the executable Minimal v1 subset; later proposal sections are retained only for
post-v1 authority work.

## Invariants

1. `WorldState` remains the only authoritative state.
2. A Source field is evidence, not Native rule authority.
3. Eligibility is keyed by curated numeric `type_code/subtype_code` identities;
   display names and symbols are forbidden inputs.
4. Capability generation reads one detached pre-state and is deterministic.
5. A capability never mutates state, consumes RNG, records evidence or advances
   a timer.
6. Intent and adjudication events remain separate.
7. RED/BLUE never receive hidden passenger or carrier relationship IDs.
8. UNKNOWN values block execution; they never become permissive defaults.

## Proposed immutable authority models

These are design signatures, not Python implementation in this phase.

```python
@dataclass(frozen=True, slots=True)
class OperatorTypeKey:
    type_code: int
    subtype_code: int


@dataclass(frozen=True, slots=True)
class BoardingProfile:
    profile_id: str
    passenger_type_keys: frozenset[OperatorTypeKey] | Literal["UNKNOWN"]
    carrier_type_keys: frozenset[OperatorTypeKey] | Literal["UNKNOWN"]
    capacity_metric: Literal["OPERATOR", "CURRENT_SQUAD", "SLOT", "UNKNOWN"]
    capacity_value: int | Literal["UNKNOWN"]
    embark_duration_ms: int
    disembark_duration_ms: int
    require_same_hex: bool
    require_stopped: bool
    prohibited_while_suppressed: bool
    require_empty_carrier: bool | Literal["UNKNOWN"]
    allowed_terrain: frozenset[str] | Literal["UNKNOWN"]
    required_altitude_state: str | Literal["UNKNOWN"]
    interruption_policy_id: str | Literal["UNKNOWN"]
    disembark_placement_policy_id: str | Literal["UNKNOWN"]


@dataclass(frozen=True, slots=True)
class BoardingAuthorityDescriptor:
    authority_version: str
    profile_hash: str
    source_table_hash: str
    eligibility_mapping_hash: str | Literal["UNKNOWN"]
    capacity_mapping_hash: str | Literal["UNKNOWN"]
    observation_policy_hash: str | Literal["UNKNOWN"]
    destruction_policy_hash: str | Literal["UNKNOWN"]
```

The current ground and transport-helicopter profiles remain `BLOCKED` whenever
any required field is UNKNOWN.

## Post-v1 richer lifecycle proposal

```text
INDEPENDENT
  -> EMBARKING
  -> EMBARKED
  -> DISEMBARKING
  -> INDEPENDENT
```

Possible interruption outcomes must be profile-authorized, not inferred:

```text
EMBARKING/DISEMBARKING
  -> INTERRUPTED_RESET       # known for helicopter suppression
  -> UNKNOWN                 # ground semantics not frozen
```

Recommended future state data:

```python
@dataclass(frozen=True, slots=True)
class BoardingRelation:
    passenger_id: int
    carrier_id: int
    phase: BoardingPhase
    action_id: str | None
    action_sequence: int | None
    remaining_ms: int
    profile_id: str
    authority_version: str
```

Whether this relation belongs directly on `OperatorState` or in a normalized,
sorted `WorldState.boarding_relations` collection must be decided before Phase
C. It must not live only in `raw_attributes`.

## BoardCapability draft

```python
@dataclass(frozen=True, slots=True)
class BoardCapability:
    carrier_id: int
    passenger_id: int
    capability_type: Literal["BOARD"]
    profile_id: str
    capacity_metric: str
    capacity_available: int | Literal["UNKNOWN"]
    distance_hex: int
    passenger_phase: str
    carrier_phase: str
    status: Literal["AVAILABLE", "BLOCKED"]
    blockers: tuple[str, ...]
    authority_version: str
    authority_hash: str
```

Deterministic ordering:

```text
(carrier_id, passenger_id, profile_id)
```

The query must not return `AVAILABLE` until all applicable authority is known.
Required blocker codes include:

```text
BOARDING_ELIGIBILITY_UNKNOWN
BOARDING_CAPACITY_METRIC_UNKNOWN
BOARDING_CAPACITY_VALUE_UNKNOWN
BOARDING_RELATION_MAPPING_UNKNOWN
BOARDING_INTERRUPTION_POLICY_UNKNOWN
BOARDING_OBSERVATION_POLICY_UNKNOWN
BOARDING_DESTRUCTION_POLICY_UNKNOWN
PASSENGER_NOT_INDEPENDENT
CARRIER_NOT_AVAILABLE
NOT_SAME_HEX
PASSENGER_NOT_STOPPED
CARRIER_NOT_STOPPED
PASSENGER_SUPPRESSED
CARRIER_SUPPRESSED
CARRIER_MARCHING
CARRIER_NOT_EMPTY
TERRAIN_NOT_AUTHORIZED
ALTITUDE_STATE_NOT_AUTHORIZED
```

## DisembarkCapability draft

```python
@dataclass(frozen=True, slots=True)
class DisembarkCapability:
    carrier_id: int
    passenger_id: int
    capability_type: Literal["UNLOAD"]
    profile_id: str
    target_policy: str | Literal["UNKNOWN"]
    target_hex: int | None
    status: Literal["AVAILABLE", "BLOCKED"]
    blockers: tuple[str, ...]
    authority_version: str
    authority_hash: str
```

No target hex may be guessed. If placement authority is UNKNOWN, the result is
`BLOCKED`.

## Same-pre-state validation draft

For a sealed tick, every boarding intent must be validated against the same
pre-state:

```text
sealed batch
  -> resolve participant identities
  -> resolve authoritative BoardingProfile
  -> validate side/ownership/alive/deployed relationship
  -> validate phase, co-location, stopped and suppression state
  -> validate capacity and profile-specific terrain/altitude
  -> stage relation/timer effects
  -> deterministically resolve shared-passenger/shared-carrier/capacity conflicts
  -> apply to working state
  -> atomic commit
```

Conflict ordering cannot silently grant lower actor IDs a gameplay advantage.
The authority contract must define policy for multiple passengers competing for
capacity and one passenger selecting multiple carriers. Canonical ordering may
implement a frozen conflict policy, but cannot invent it.

## Action recommendation

Do not encode boarding as MOVE. MOVE expresses a spatial path; boarding changes
an operator relationship and starts a timed exclusive transition.

Recommended future explicit intents:

```text
ActionType.BOARD
  actor_id = passenger_id
  target_id = carrier_id
  parameters = {profile_id, authority_version, authority_hash}

ActionType.UNLOAD
  actor_id = carrier_id
  target_id = passenger_id
  parameters = {profile_id, authority_version, authority_hash}
```

Minimal v1 places the passenger in the carrier hex at completion and therefore
does not accept a client-selected `target_hex`. Both actions are now explicit
members of `ActionType`; richer placement remains outside v1.

## Future request/effect boundary

```text
BoardCapability
  -> BoardRequest
  -> BoardValidationResult
  -> BoardingTransitionEffect
  -> working WorldState
  -> atomic commit

DisembarkCapability
  -> DisembarkRequest
  -> DisembarkValidationResult
  -> DisembarkingTransitionEffect
  -> working WorldState
  -> atomic commit
```

Requests should include tick, action ID/sequence, participant IDs, profile and
authority hashes, pre-state relation evidence and remaining capacity. Effect
objects must be immutable and must not carry mutable `OperatorState` references.

## Event draft

Possible formal events, subject to a replay semantics review:

```text
BOARDING_STARTED
BOARDING_INTERRUPTED
UNIT_BOARDED
DISEMBARKING_STARTED
DISEMBARKING_INTERRUPTED
UNIT_DISEMBARKED
```

Events describe adjudicated facts; they are not accepted action echoes.

## Replay impact

Future Replay inputs must record the explicit intent:

```json
{
  "action_type": "BOARD",
  "actor_id": 101,
  "target_id": 201,
  "parameters": {
    "profile_id": "...",
    "authority_version": "...",
    "authority_hash": "..."
  }
}
```

```json
{
  "action_type": "UNLOAD",
  "actor_id": 101,
  "target_id": 201,
  "parameters": {
    "target_hex": 1234,
    "profile_id": "...",
    "authority_version": "...",
    "authority_hash": "..."
  }
}
```

Before supporting these actions, strict Replay must fail at step 0 when the
boarding authority version/hash is incompatible. Snapshot, restore and world
hash must include the normalized relation, phase, remaining time and all
relationship invariants. Old replays without BOARD/UNLOAD remain unchanged and
must never have boarding actions inferred from frame differences or Action Echo.

Future GREEN events may contain complete relation evidence. RED/BLUE projection
must remove any hidden passenger/carrier IDs according to the existing Source
perspective policy.

## Human and Agent impact

Human and Agent capability projections must consume the same read-only Engine
query. Neither may infer carrier suitability, capacity, co-location or target
hex from Viewer metadata.

Future Human flow:

```text
select own passenger
  -> server BoardCapability
  -> select available carrier
  -> preview
  -> submit BOARD
```

Future Agent flow:

```text
detached SideObservation + sorted BoardCapability
  -> explicit BOARD/UNLOAD decision
  -> gateway
  -> Engine
```

An observed enemy carrier must not reveal its passenger list or passenger type.

## Capability Registry pre-analysis

Do not add this record until a read-only capability implementation exists:

```text
boarding:
  ENGINE: NOT_IMPLEMENTED
  HUMAN: NOT_IMPLEMENTED
  AGENT: NOT_IMPLEMENTED
  REPLAY: NOT_IMPLEMENTED
  DEMO: PENDING
```

## Future demo

After implementation, `examples/demos/boarding_basic/` should verify only
authorized rules:

```text
infantry and carrier co-located/stopped
  -> BOARD transition for 75 seconds
  -> passenger relationship committed
  -> carrier moves through regular MovementRuntime
  -> UNLOAD transition for 75 seconds
  -> relationship removed at authorized target hex
  -> strict replay equality
```

No demo may contain a rule-specific Engine branch.

## Phased implementation boundary

### Phase A — Complete authority

Create versioned eligibility, capacity, interruption, placement, observation
and destruction policies. Resolve every execution-required UNKNOWN.

### Phase B — Read-only capabilities

Add immutable BoardCapability/DisembarkCapability queries. Return BLOCKED for
missing authority. Do not create actions.

### Phase C — Request and transition runtime

Add explicit BOARD/UNLOAD intents, immutable requests and normalized boarding
relations with deterministic conflict policy. No UI first.

### Phase D — Transactional commit and Replay

Integrate transition start/completion/interruption into the existing working
state and atomic tick. Add events, snapshots, hashes and strict replay gates.

### Phase E — Human and Agent projection

Expose the same Engine-issued capabilities through Product/Human and Agent
interfaces; never calculate eligibility in the client.

### Phase F — Demo and Benchmark

Add `boarding_basic`, strict replay regression and Capability Benchmark only
after the runtime contract is implemented.
