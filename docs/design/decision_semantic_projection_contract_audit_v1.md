# Decision Semantic Projection Contract Audit v1

## 1. Executive Summary

This is a read-only contract audit. It does not implement a Decision Semantic
Projection, Capability View, encoder, mask, reward, renderer, Human migration,
or Engine rule. The only repository change made by this round is this document.

The repository has enough authority and perspective isolation to implement a
model-agnostic side decision projection without reading mutable `WorldState` or
GREEN data. The future projection must compose already-safe inputs:

```text
detached SideObservation / Controller snapshot
  + OWN and OBSERVED_ENEMY lifecycle perspective payloads
  + frozen Engine valid-action snapshot
  + explicit Engine capability-query records
  + public decision-window metadata
  -> immutable SideDecisionSemanticProjection
```

Engine validation remains the final authority. The projection is not a
validator and must not reproduce rules from lifecycle fields. In particular,
local lifecycle readiness, actor/family capability availability, and legality
of one parameterized action are three separate contracts.

Current `valid_actions` are deterministic, Engine-produced **concrete action
instances**, not a capability-family Boolean map. The training mask is a pure
fixed-capacity encoding of that frozen list and contains no independent rule
logic. Indirect fire is intentionally parameterized outside the generic list;
its Engine capability and target-hex validation APIs remain separate.

Runtime capability records are heterogeneous. Indirect fire and concealment
produce explicit blocked records/reasons, while Guided fire and BOARD/UNLOAD
produce only currently legal parameter tuples, and generic MOVE/SHOOT/WAIT/
weapon-posture families are represented by concrete `ValidAction` instances.
Therefore v1 must preserve evidence provenance and use `UNKNOWN` when absence
cannot distinguish `BLOCKED` from `NOT_APPLICABLE`. It must never invent a
uniform blocker by inspecting lifecycle fields. This is a P2 completeness
limitation, not a P1 contract blocker.

Readiness:

```text
DECISION_SCHEMA_VERSION_READINESS: READY
DECISION_HASH_READINESS: READY
CAPABILITY_CONTRACT_READINESS: READY
ACTION_MASK_AUTHORITY_READINESS: READY
DECISION_SEMANTIC_PROJECTION_CONTRACT_READINESS: READY
HUMAN_DECISION_INTEGRATION_READINESS: READY
AGENT_DECISION_INTEGRATION_READINESS: READY
TRAINING_DECISION_INTEGRATION_READINESS: READY
```

## 2. Scope and Non-goals

The audit freezes the semantic composition, capability evidence, action-family
boundary, side context, identity joins, ordering, versioning, hashing,
noninterference, and future implementation tests for a side-level decision
snapshot.

It does not implement or change:

- `DecisionSemanticProjection`, `DecisionState`, or `DecisionView`;
- a generic Capability framework or blocker framework;
- `ObservationEncoder`, `ActionEncoder`, tensor layouts, or normalization;
- `NativeTrainingEnv`, rewards, winner labels, or Eight Factor analysis;
- LLM JSON/text rendering, prompts, or tool schemas;
- Human/Viewer schemas;
- Engine action validation, `valid_actions`, visibility, timers, or World data;
- Replay authority or authoritative hashes.

## 3. Current Human Decision Flow

The current Human path is:

```text
SessionManager.controller_view
  -> detached ControllerView
       observation: side-safe SideObservation record
       source_frame: side-safe Source-compatible frame
       valid_actions: current Engine ValidAction records
       frame_index / step_index / exact sim_time_ms
  -> SideController / ProductOperationService
  -> capability/menu presentation and parameter preview
  -> frozen action ID or server-built parameterized proposal
  -> CommandGateway decision window
  -> NativeEngine.step
  -> Engine revalidation/adjudication
```

`SideController.stage_action` accepts only an ID from the current controller
snapshot. `DecisionWindow` freezes each side's actions, rejects unknown/stale
IDs and duplicate actor actions, and merges the two side submissions only after
both have submitted. MOVE routes and Indirect target hexes are server-side
proposals/validation, not browser rule calculations.

`ProductOperationService.capabilities()` provides useful presentation summaries
but some unavailable codes are explanations inferred from the frozen unit menu
and legacy public state. These texts/codes are not a complete Engine blocker
authority and must not be promoted unchanged to the machine contract.

`HUMAN_DECISION_AUTHORITY: PASS`.

## 4. Current Agent Decision Flow

The classic Agent path is:

```text
GameSession.observe(side)              -> detached SideObservation
GameSession.valid_actions(side)        -> Engine ValidAction tuple
freeze_valid_actions(side, values)     -> immutable ID -> detached action map
Agent.act(copies)                      -> AgentAction(valid_action_id)
resolve_agent_selection                -> action from frozen server snapshot
joint side batch                       -> GameSession -> NativeEngine.step
```

The runner obtains both sides' observations/actions before the joint step. An
Agent-supplied action body is never authorization; only the selected ID in the
pre-exposure frozen map is resolved.

Realtime and product-operation Agents receive `AgentDecisionContext` with a
detached side observation, decision/frame/time identity, and
`SeatActionCapabilities`. Observation-version ownership and decision-window
indexes reject stale/cross-side submissions. Product operations are previewed
and revalidated at application time.

Agents receive neither mutable `WorldState`, GREEN frames, authoritative
lifecycle Views, RNG, nor adjudication evidence.

`AGENT_DECISION_AUTHORITY: PASS`.

## 5. Current Training Decision Flow

### Compatibility NativeTrainingEnv

```text
ControllerView.observation
  -> SideObservationEncoder (JSON clone; rejects green/frame_analysis)
ControllerView.valid_actions
  -> NativeActionEncoder
       exact current action order
       action IDs and detached action records
       True for current actions, False for fixed padding
selected integer
  -> current frozen action ID
  -> SideController / Gateway / Engine
```

The encoder does not test alive, suppression, cooldown, range, visibility, or
any other rule. It rejects over-capacity lists rather than silently truncating
authority.

### NativeMultiSeatEnv / NativeDecisionSession

The recommended two-seat environment gets both side `ControllerView` values,
builds side capabilities through `ProductOperationService`, and hashes one
immutable decision snapshot containing decision index, frame index,
`sim_time_ms`, side observation, valid actions, and capabilities. RED and BLUE
must share the same frame/time/window before a joint decision is accepted.

Actions are submitted through product previews and one Gateway decision
window. `NativeDecisionSession` owns no Engine or World object.

Reward, winner, outcome, and transition evaluation fields exist in training
result/transition records, but are not supplied in `AgentDecisionContext` as
policy observation. The future interface must make this separation explicit:

```text
POLICY_INPUT: Decision Semantic Projection + action authority
TRAINING_SUPERVISION: reward / winner / outcome / evaluation labels
```

`TRAINING_DECISION_AUTHORITY: PASS`.

## 6. Capability Authority

The capability registry under `config/capabilities/` is traceability metadata,
not runtime action authority. Runtime authority is the Engine and the named
query/validation APIs below.

| Capability | Runtime authority | Producer form | Operator-level? | Needs parameters? | Current consumers | Contract verdict |
|---|---|---|---|---|---|---|
| MOVE | movement planner + Engine source action staging | adjacent concrete `ValidAction`; destination proposal separately plans an explicit path | actor-local readiness plus map state | path/destination/mode | Human, Agent, training | ENGINE_GROUNDED |
| STOP_MOVE | Engine `valid_actions` + stop staging | concrete action only while Engine exposes it | actor-local | no target | all | ENGINE_GROUNDED |
| WAIT | Engine `valid_actions` / empty-side decision behavior | concrete actor/side action | actor/side | no | all | ENGINE_GROUNDED |
| DIRECT_FIRE (`SHOOT`) | `NativeEngine.valid_actions`, direct-fire request validation/adjudicator, final staging | concrete shooter/weapon/target instances | no | target + weapon | all | ENGINE_GROUNDED |
| GUIDED_FIRE | `get_guided_fire_capabilities`, request validation, final staging | only currently legal guider/shooter/weapon/target tuples; then concrete FIRE action | no | all four identities | Human/Agent/valid actions | ENGINE_GROUNDED; absence ambiguous |
| INDIRECT_FIRE | `get_indirect_fire_capabilities`, `validate_indirect_fire_target_hex`, final plan request/staging | AVAILABLE/BLOCKED shooter+weapon records; target hex separate | family/weapon record | target hex | Human/Agent product path | ENGINE_GROUNDED; not generic valid_actions |
| BOARD | `get_board_capabilities`, request build and revalidation | only currently legal passenger/carrier tuples; concrete valid action | no | carrier/passenger | Human/Agent/valid actions | ENGINE_GROUNDED; absence ambiguous |
| UNLOAD | `get_unload_capabilities`, request build and revalidation | only currently legal carrier/passenger tuples; concrete valid action | no | carrier/passenger | Human/Agent/valid actions | ENGINE_GROUNDED; absence ambiguous |
| ENTER_CONCEALMENT | `get_concealment_capabilities`, blocker, final staging | one available/blocked record per own operator; available records become valid actions | yes | no target | Human/Agent/valid actions | ENGINE_GROUNDED |
| WEAPON_LOCK | Engine source `valid_actions` + transition staging | concrete action | actor-local | no | all | ENGINE_GROUNDED; no blocker record |
| WEAPON_UNFOLD | Engine source `valid_actions` + transition staging | concrete action | actor-local | no | all | ENGINE_GROUNDED; no blocker record |
| CHANGE_STATE | Engine staging validators | concrete action where exposed | actor-local | transition/mode | legacy/source consumers | ENGINE_GROUNDED; family API absent |

The Decision layer may normalize these records but may not manufacture missing
status. It must retain an `evidence_kind` so that “one executable instance
exists” is not confused with a complete actor-local capability proof.

## 7. Valid Actions Authority

`ValidAction` is an immutable wrapper around one complete `EngineAction`, with
priority, source, and deterministic `action_id`. `NativeEngine.valid_actions`
sorts records deterministically and, in Source mode, caches serialized records
by the authoritative World hash. The cache returns reconstructed detached
records.

Thus current `valid_actions` are:

- concrete action instances, not just `ActionType` names;
- side-bound and actor-bound;
- parameterized for enumerated MOVE, direct fire, Guided, BOARD and UNLOAD;
- incomplete as a universal parameter space (notably Indirect target hex and
  destination MOVE proposals are queried separately);
- frozen by the Gateway/Agent authorization boundary;
- revalidated by Engine staging, so an ID snapshot never replaces final
  legality.

No downstream consumer is authorized to create a valid action by copying the
shape of an `EngineAction`.

## 8. Blocker Taxonomy

### Capability-level blocker evidence

These can enter an OWN capability snapshot only when copied from an explicit
Engine capability query:

- Indirect authority blockers, including authority gaps and the actor-local
  `weapon_cooldown_remaining` and
  `indirect_fire_planned_cooldown_remaining` codes;
- Concealment reasons such as `DESTROYED`, `ON_BOARD`, `NOT_DEPLOYED`,
  `MOVING`, `SUPPRESSED`, or `TRANSITION_IN_PROGRESS`.

The codes are deterministic strings and tested, but there is no single global
blocker schema/version covering all action families. v1 must namespace codes
by producer and record producer/version metadata where available.

### Action-instance blocker evidence

These belong to preview/validation responses, not persistent state:

- target not found/not observed;
- target hex missing/out of range/invalid domain;
- stale capability or request;
- invalid weapon, target, guider, carrier, passenger, path, or mode;
- actor not controlled, wrong side, duplicate actor action;
- exact direct/Guided/Indirect/BOARD/UNLOAD validation failures.

They must not be enumerated over every possible target/destination in the
Decision projection.

### Perspective-safe

OWN actor-local capability reasons from an explicit Engine query are safe.
Stable action-family availability derived from the side's own frozen action
catalog is safe. Target IDs may occur only in the separately authorized action
catalog/preview, never in a generic actor blocker.

### Do not expose

- Product/UI natural-language messages as machine authority;
- `_capability_unavailable` approximations as final Engine reasons;
- hidden enemy state, GREEN evidence, RNG/table evidence, relationship IDs;
- a target-sensitive rejection promoted to a global family blocker;
- raw exception text or integrity/debug codes.

## 9. Action Mask Authority

`NativeActionEncoder` performs no legality calculation. It copies the current
frozen `valid_actions` in canonical order, assigns `True` to those entries and
`False` only to capacity padding. Selection resolves the current integer back
to the corresponding current action ID; stale/out-of-range/padded indexes are
rejected before submission. Gateway and Engine validation still follow.

```text
ACTION_MASK_RULE_DUPLICATION: NONE
ACTION_MASK_GREEN_INPUT: NONE
ACTION_MASK_AUTHORITY: ENGINE_VALID_ACTION_SNAPSHOT
ACTION_MASK_AUTHORITY_READINESS: READY
```

Read-only probes compared identical RED observation evidence while changing
enemy exact health, suppression, weapon/Guided/Indirect timers, finite
lifecycle remaining time, and a valid hidden mounted-passenger relation. The
RED action records and encoded masks remained identical. The lifecycle probe
used two valid finite-lifecycle states with the same identity and different
remaining time; the boarding probe compared a completed hidden passenger with
an otherwise equivalent carrier-only scenario.

One deliberately inconsistent profile/domain mutation changed target
legality, but `NativeEngine._validate_operator_lifecycle_snapshot` rejects that
World (`OPERATOR_LIFECYCLE_DOMAIN_MISMATCH`); it is not an admissible
noninterference pair.

The future tests must keep the precondition explicit: both compared states
must satisfy World invariants, have identical legal perspective evidence, and
not change a policy-authorized action-instance fact.

## 10. Perspective-safe Input Contract

The Decision projector may consume only immutable/detached values supplied by
trusted producers:

1. a side identifier and frozen decision-window identity;
2. side-safe `SideObservation` data or a smaller typed side-context projection;
3. `OwnOperatorLifecyclePayload` values;
4. `ObservedEnemyOperatorLifecyclePayload` values;
5. the side's frozen Engine `ValidAction`/action-catalog records;
6. explicit detached Engine capability query records;
7. public ruleset/engine/perspective-policy metadata.

It must not consume or look up:

- `WorldState` or `OperatorState`;
- GREEN/authoritative lifecycle payloads or authoritative lifecycle hashes;
- Source GREEN frame, Native evidence, RNG, events, adjudication tables;
- hidden enemy data or raw attributes;
- mutable Engine/Session/Gateway service references;
- wall clock, deadline monotonic timestamps, or a live capability callback.

Missing lifecycle/capability input is `MISSING` or `UNKNOWN`, never repaired by
World/GREEN backfill.

## 11. Side-level Decision Context

The current Human/Agent/Training entry points are side-level and permit a joint
batch with at most one action per selected actor. v1 should therefore be one
side-level snapshot with reusable per-operator records, not a collection of
independent local views.

Recommended side context:

- side (`RED`/`BLUE` normalized);
- decision-window index;
- fact/frame index;
- exact authoritative `sim_time_ms` (not wall clock and not `tick` alone);
- stage;
- `terminated` and `truncated` decision-time status;
- current side-safe objective records;
- current public mission/terminal-rule context already in SideObservation;
- stable sorted controllable own operator references;
- stable sorted current observed-contact references.

Public objective scores are allowed by perspective policy, but native
`SideObservation` currently carries objective records while scores live in the
parallel Source frame. v1 should not import the Source record merely to fill a
score field. Include scores only after a single typed side-safe producer is
selected; until then score is excluded rather than GREEN-backfilled.

Exclude from policy input:

- reward, return, winner, outcome labels and Eight Factor analysis;
- server monotonic open/deadline timestamps;
- full action/event history and Replay metadata;
- scenario seed, RNG state, World hash, GREEN hash;
- renderer/UI state and normalized model features.

At a terminal snapshot, `terminated`/`truncated` may be present for protocol
closure, but winner/outcome remain the result/supervision channel.

## 12. Operator-level Decision Context

Decision v1 composes, rather than remaps:

```text
own_operators:
  tuple[OwnOperatorLifecyclePayload, ...]

observed_enemies:
  tuple[ObservedEnemyOperatorLifecyclePayload, ...]
```

OWN lifecycle timers, movement task semantics, health and operational state
therefore retain the perspective contract. Enemy schema remains the fixed
allow-listed identity/position/alive/coarse-motion payload, with no hidden
lifecycle shape.

The identity join must be exact:

- OWN lifecycle `operator_reference` == side observation `operator_id` ==
  capability/action `actor_id`;
- observed contact reference == action target reference where the Engine has
  issued such an action.

No name, display label, source subtype, or approximate position matching is
allowed. Missing/mismatched references fail closed.

## 13. Capability Snapshot Contract

### Status semantics

```text
AVAILABLE:
  explicit Engine capability record says AVAILABLE, or at least one frozen
  Engine action instance exists; evidence kind states which meaning applies.

BLOCKED:
  an explicit Engine capability producer returned BLOCKED/reason evidence.

NOT_APPLICABLE:
  an explicit Engine authority says the actor does not possess the family.
  Absence from valid_actions is never enough.

UNKNOWN:
  the supplied Engine evidence cannot distinguish blocked, inapplicable, or
  missing parameter opportunity.

MISSING:
  a producer required by the declared snapshot contract did not provide its
  record or the actor/reference join failed.
```

### Non-implemented draft

```text
DecisionCapabilitySnapshot {
  schema_version: "native-decision-capability-snapshot/1.0"
  decision_window_index: int
  side: Side
  operators: tuple[OperatorCapabilitySummary, ...]  # sorted actor reference
  action_catalog_hash: sha256                       # side-safe catalog only
}

OperatorCapabilitySummary {
  operator_reference: int
  families: tuple[CapabilityFamilyStatus, ...]      # stable family order
}

CapabilityFamilyStatus {
  family: stable enum
  status: AVAILABLE | BLOCKED | NOT_APPLICABLE | UNKNOWN | MISSING
  evidence_kind:
    VALID_ACTION_INSTANCE_PRESENT
    | ENGINE_CAPABILITY_QUERY
    | NO_CONCLUSIVE_ENGINE_EVIDENCE
  available_instance_count: int | null
  blocker_codes: tuple[NamespacedBlockerCode, ...]
}

NamespacedBlockerCode {
  producer: stable string
  code: stable string
}
```

`available_instance_count` is not a count of all theoretically legal
parameters; it is only the count represented in the current Engine action
catalog. Blockers are empty unless an explicit Engine query produced them.

## 14. Action Family vs Action-instance

An action family answers only what the current authority evidence supports for
an actor. An action instance binds all currently required parameters and is
still subject to final revalidation.

Examples:

- MOVE family evidence does not legalize an arbitrary destination/path;
- DIRECT_FIRE family availability means at least one Engine-issued
  shooter/weapon/target instance, not every target;
- GUIDED availability is currently evidenced by exact guider/shooter/weapon/
  target tuples;
- INDIRECT capability can be AVAILABLE before a target hex is chosen; the hex
  must pass the dedicated validator;
- BOARD/UNLOAD availability is pair-specific;
- no family status makes a forged action legal.

Preview/query is ephemeral. Its result is bound to a decision window and must
not become long-lived Decision state or replay authority.

## 15. valid_actions Recommendation

`HYBRID` is the recommended contract:

1. Decision Semantic Projection contains cheap, stable family summaries and an
   `action_catalog_hash`/snapshot reference.
2. A sibling immutable `ActionCatalogSnapshot` contains the current concrete
   Engine `ValidAction` records and is the sole source for the compatibility RL
   mask and action-ID selection.
3. Parameterized MOVE/Indirect/other future large domains use on-demand
   ActionQuery/Preview services bound to the same decision window.

Embedding every complete action in the semantic state would conflate state and
legality, enlarge hashes, and scale poorly. Omitting the catalog entirely would
break the current safe RL and classic-Agent authorization model. HYBRID keeps
both concerns explicit.

## 16. Action Mask Noninterference

The mask is an encoded action catalog, not a lifecycle field. Its formal
noninterference rule is:

> For two invariant-valid Worlds A and B, if perspective-safe observation,
> OWN lifecycle inputs, observed-enemy lifecycle inputs, public side context,
> and all policy-authorized action-instance facts are equal, then the Engine
> action catalog, capability-family snapshot, encoded mask, and Decision
> projection for side P must be equal.

This condition does not demand that an action catalog ignore a legitimately
changed observed target, own state, public objective, or current legal
parameter fact. It forbids the Decision layer, capability adapter, or encoder
from consulting hidden state independently.

Action-catalog and mask hashes are side-safe hashes of content already supplied
to that side. They must not contain World or authoritative lifecycle hashes.

## 17. Snapshot and Replay

Decision projections do not enter `WorldState`, authoritative snapshots, or
World hashes. Correct verification is recomputation:

```text
World A
  -> side observation + lifecycle perspective + capability/action snapshots
  -> Decision A
snapshot/restore or strict replay
  -> recompute the same inputs
  -> Decision B
assert Decision A == Decision B and hashes equal
```

Current World snapshot/restore, strict Replay, side observation, lifecycle
perspective, and valid-action determinism provide the required sources. The
actual Decision equality tests remain implementation-stage work.

Replay may record Decision schema/policy versions or a derived Decision hash as
verification metadata. It must never restore World state from a Decision
payload or inject a final action mask.

## 18. Version and Hash Contract

Recommended versions:

```text
decision_schema_version:
  native-decision-semantic-projection/1.0

capability_snapshot_schema_version:
  native-decision-capability-snapshot/1.0

perspective_schema_version:
  native-operator-lifecycle-perspective/1.0

perspective policy:
  id      = native-source-perspectives
  version = 1.1.0
  hash    = fe797540222c8d8749670eac8b52283a0dfdd2ef685c531df64977f2f69c0d28
```

The Decision envelope should also record public Engine/ruleset versions. There
is no current single runtime capability-authority hash spanning every family;
the Decision contract must not pretend the traceability registry is that
authority. Per-feature authority versions/hashes remain attached to records
where they already exist.

`semantic_hash` is recommended and uses canonical JSON -> UTF-8 -> SHA-256.
Canonicalization requirements:

- stable schema field order and sorted operator/contact/capability tuples;
- stable Enum string values;
- integer milliseconds without normalization;
- sorted unique blocker codes with producer namespaces;
- canonical current action-catalog hash/reference;
- exact representation of `UNKNOWN`, `MISSING`, and `NOT_APPLICABLE`;
- no wall clock, memory address, mutable iteration order, renderer output,
  reward, winner, World hash, authoritative lifecycle hash, or hidden value.

The semantic hash is a derived regression artifact, never adjudication or
restore authority.

## 19. Proposed Non-Implemented Schema

```text
SideDecisionSemanticProjection {
  schema_version: "native-decision-semantic-projection/1.0"

  authority_metadata: {
    engine_version
    ruleset_id
    ruleset_version
    perspective_schema_version
    perspective_policy_id
    perspective_policy_version
    perspective_policy_hash
    capability_snapshot_schema_version
  }

  snapshot: {
    side
    decision_window_index
    frame_index
    sim_time_ms
    stage
    terminated
    truncated
  }

  side_context: {
    objectives                 # only current SideObservation authority
    mission                    # only current public mission/rule context
    controllable_operator_references
  }

  own_operators: tuple[OwnOperatorLifecyclePayload, ...]
  observed_enemies: tuple[ObservedEnemyOperatorLifecyclePayload, ...]
  capabilities: DecisionCapabilitySnapshot

  action_space: {
    catalog_schema_version
    catalog_hash
    action_count
    query_contract_version
  }

  semantic_hash
}
```

The core schema contains no GREEN variant. GREEN remains a referee/evaluation
view. A separately authorized debug tool may compose GREEN data, but normal
RED/BLUE decision APIs must never share that type.

Collections are immutable tuples and sorted by exact reference. The projection
stores values only, never World/Observation/Engine references or callbacks.

## 20. Training Boundary

The target training flow is:

```text
Engine -> side observation -> lifecycle perspective
       -> capability/action snapshots -> Decision Projection
       -> ObservationEncoder -> tensor/model observation

model action -> ActionEncoder -> frozen action ID or SeatOperation
             -> preview/Gateway -> Engine

Engine result -> TrainingSupervision(reward/outcome/evaluation)
```

The encoder owns enum indexing, padding, variable-entity tensor layouts, timer
normalization, and discrete action indexing. Decision semantics retain typed
Enums, integer milliseconds, and variable-length tuples.

Current training has a sound authority boundary but has not yet integrated the
new lifecycle perspective or future Decision schema. This is planned
integration, not an architectural repair.

## 21. LLM and Human Boundaries

An LLM renderer may transform the same Decision projection into structured
JSON/text and expose parameterized preview/submit tools. It must not read World,
GREEN, raw observation internals, or rebuild suppression/cooldown rules.

Human UI should treat the Decision projection as the standard semantic core of
`ControllerView`, not as a total replacement for transport, preview results,
or Source-compatible rendering. Recommended relationship:

```text
ControllerView
  contains/addresses Decision Semantic Projection as its semantic core
  plus transport/window status and separate ActionQuery/Preview results
```

Rule Agent, RL, LLM, and Human may use different encoders/renderers, but the
state and capability semantics remain shared.

## 22. Determinism, Purity, and Cost

Required purity:

```text
World mutation:          NONE
Observation mutation:    NONE
Lifecycle mutation:      NONE
Capability mutation:     NONE
Timer advance:           NONE
RNG:                     NONE
Engine step:             NONE
Visibility/LOS:          NONE
Pathfinding:             NONE
Target enumeration:      NONE in semantic projection
```

The projector is O(own operators + observed contacts + already-produced
capability/action records). It must not scan World, run LOS/pathfinding, or
generate parameter combinations. Expensive destination/target queries remain
on demand.

The snapshot point is one completed authoritative observation refresh and one
matching open decision window. Observation, lifecycle, capability, and action
catalog records must share side, decision index, frame index, and exact
`sim_time_ms`. A mismatch fails closed rather than being refreshed piecemeal.

## 23. Findings and Contract Blockers

### P0

NONE.

No Agent/training policy input was found to contain GREEN or mutable World
state. The training mask is not an independent rule engine. Read-only
noninterference probes found no hidden-field change in the current mask for the
admissible tested pairs.

### P1

NONE.

Human, classic Agent, realtime/product Agent, and training submission paths all
ultimately use the same Engine action authority and final Engine validation.
The future projection can be constructed entirely from perspective-safe,
detached inputs.

### P2

1. `HETEROGENEOUS_RUNTIME_CAPABILITY_RECORDS`: only some families emit
   explicit BLOCKED records and stable reasons. Absence is ambiguous.
2. `PRODUCT_UNAVAILABLE_REASON_IS_PRESENTATION_EVIDENCE`: UI/product fallback
   codes infer explanations from menus/legacy state and are not a versioned
   cross-family Engine blocker taxonomy.
3. `VALID_ACTIONS_AND_PARAMETER_QUERIES_ARE_SPLIT`: generic valid actions,
   MOVE destination proposals, and Indirect target validation have different
   shapes. HYBRID composition is required.
4. `CAPABILITY_AUTHORITY_HAS_NO_SINGLE_VERSION_HASH`: the capability registry
   is traceability metadata; it is not runtime authority.
5. `SIDE_SCORE_TYPED_SOURCE_GAP`: scores are policy-public in Source frames but
   absent from native SideObservation. Decision v1 should omit rather than
   backfill them until one typed producer is chosen.
6. `CONTROLLERVIEW_PARALLEL_REPRESENTATIONS`: observation, Source frame, valid
   actions, product capability summary, and future Decision semantics can drift
   unless Decision becomes the shared semantic core.
7. `ACTION_INDEX_IS_WINDOW_LOCAL`: RL integer action indexes are deterministic
   only inside one frozen catalog and must not be treated as stable across
   windows/scenarios.
8. Existing lifecycle P2s remain outside scope: stale raw suppression
   compatibility, concealment typed/raw serialization, operator-global weapon
   cooldown ambiguity, and missing typed CLOSE_COMBAT axis.

### P3

1. Capability/rejection codes use mixed capitalization and namespaces.
2. Some typed capability records lack a shared protocol/base type.
3. A dedicated `projection/decision_semantic.py` module and frozen dataclasses
   are recommended for implementation.

### Contract Blockers

NONE.

Uniform, richly explained BLOCKED/NOT_APPLICABLE status for every family is not
currently available. This does not block v1 because fail-closed `UNKNOWN` and
explicit evidence provenance prevent invented rules. It would become a P1 only
if an implementation claimed complete blocker coverage or derived missing
statuses from lifecycle fields.

## 24. Readiness and Required Implementation Tests

### Readiness

```text
DECISION_SEMANTIC_PROJECTION_CONTRACT_READINESS: READY
CAPABILITY_SNAPSHOT_READINESS: READY
ACTION_MASK_AUTHORITY_READINESS: READY
HUMAN_DECISION_INTEGRATION_READINESS: READY
AGENT_DECISION_INTEGRATION_READINESS: READY
TRAINING_DECISION_INTEGRATION_READINESS: READY
```

`CAPABILITY_SNAPSHOT_READINESS: READY` is conditional on the frozen v1 rule:
only explicit Engine evidence may yield AVAILABLE/BLOCKED/NOT_APPLICABLE;
ambiguous absence yields UNKNOWN. A future “complete blocker explanation” API
would require additional Engine producers and its own version.

### Required implementation tests

- **D1** same perspective-safe inputs -> same Decision projection/hash;
- **D2** hidden enemy exact health difference -> unchanged projection,
  capability family snapshot, action catalog and mask when legal evidence is
  otherwise identical;
- **D3** hidden enemy cooldown/suppression difference -> unchanged;
- **D4** hidden enemy finite-lifecycle remaining difference -> unchanged;
- **D5** hidden boarding passenger/relation difference -> unchanged;
- **D6** own suppression exact expiry -> legitimate lifecycle/capability change;
- **D7** own Guided recovery clears -> legitimate explicit opportunity/family
  evidence change when all other requirements hold;
- **D8** own Indirect planned cooldown clears -> BLOCKED to AVAILABLE in the
  explicit Engine capability record;
- **D9** legal contact appears/disappears -> observed-enemy collection and
  authorized target instances change;
- **D10** Human, classic Agent, realtime Agent and training share the same
  capability/action authority at one window;
- **D11** training mask exactly encodes the Engine frozen action catalog and
  contains no independent rule branches;
- **D12** forged/stale action remains rejected by Gateway/Engine even if a
  family summary is AVAILABLE;
- **D13** snapshot/restore -> same rebuilt Decision projection/hash;
- **D14** strict replay -> same rebuilt Decision projection/hash;
- **D15** own/enemy/capability/action collections have deterministic ordering;
- **D16** projection does not mutate World, observation, lifecycle, capability,
  timers or RNG;
- **D17** absent valid action cannot be normalized to BLOCKED or NOT_APPLICABLE
  without explicit producer evidence;
- **D18** Product natural-language fallback reasons never enter machine
  blocker fields;
- **D19** authoritative lifecycle/World hashes never enter RED/BLUE payloads;
- **D20** reward/winner/EightFactor remain outside policy input;
- **D21** mismatched side/operator/decision/frame/time references fail closed;
- **D22** new action/capability fields fail closed until explicitly mapped.

### Recommended implementation phases

1. Define frozen Decision core types, input bundle, schema/version/hash, and
   strict identity/window joins; no consumer integration.
2. Implement the Engine-evidence CapabilitySnapshot adapter plus separate
   ActionCatalog snapshot, preserving UNKNOWN and producer provenance.
3. Make Decision semantics the shared core inside/alongside `ControllerView`,
   then migrate Human and Agent readers without changing Engine authority.
4. Implement model-specific ObservationEncoder/ActionEncoder over the shared
   projection and action catalog.
5. Integrate `NativeTrainingEnv`/`NativeMultiSeatEnv`, preserving the separate
   TrainingSupervision channel.
6. Add LLM structured renderer/tool schema and Human-friendly rendering, both
   forbidden from accessing World or GREEN.

### Existing evidence exercised in this audit

The audit inspected the Engine action router/valid-action/capability producers,
GameSession/backend delegation, Agent frozen authorization, Gateway decision
window, Product preview/capability summaries, lifecycle perspective types,
training encoders/environments, side observation policy, and Replay boundary.

Read-only `/tmp`/stdin probes established:

- hidden enemy exact health, suppression, weapon/Guided/Indirect timer changes
  did not change RED side observation, Engine actions, or encoded mask in a
  state with a legal direct-fire target;
- valid finite-lifecycle remaining-time differences did not change the opposing
  observation, actions, or mask;
- a valid hidden boarded passenger versus an equivalent carrier without that
  passenger did not change the opposing observation, actions, or mask; and
- the training encoder's mask/action IDs exactly matched the current Engine
  action list.

No probe file was written to the repository.

## Phase 1 Implementation Verification

### Module and frozen schemas

Phase 1 is implemented in
`src/wargame_native_engine/projection/decision_semantic.py`.  It defines the
following independent contracts:

- Decision semantic projection:
  `native-decision-semantic-projection/1.0`;
- capability snapshot evidence:
  `native-decision-capability-snapshot/1.0`; and
- Decision-side action catalog reference:
  `native-decision-action-catalog/1.0`.

The implementation uses frozen, slotted dataclasses and stable string enums.
No value object retains `WorldState`, `OperatorState`, `NativeEngine`, an
observation object, RNG, callback, UI, Agent, or training reference.

### Authority metadata

`AuthorityMetadata` binds the Engine version, ruleset id/version/content hash,
Perspective Lifecycle schema, canonical Perspective policy id/version/hash,
CapabilitySnapshot schema, and ActionCatalog reference schema.  The factory
loads the existing perspective policy through
`rules.perspective.load_perspective_policy`; it does not create a parallel
policy identity or hashing algorithm.

The capability schema version identifies the evidence contract, not a claim
that all Engine capability producers share one runtime authority hash.  The
latter remains the recorded P2
`CAPABILITY_AUTHORITY_HAS_NO_SINGLE_VERSION_HASH`.

### Snapshot and side context

`DecisionSnapshotIdentity` freezes side, decision-window index, frame index,
exact authoritative `sim_time_ms`, stage, terminated, and truncated.  No wall,
deadline, or monotonic clock enters the value or hash.

`SideDecisionContext` contains only side-safe objectives, typed public terminal
mission rules, sorted controllable Operator references, and sorted current
observed-contact references.  Score, reward, outcome, winner, history, memory,
GREEN evidence, World hash, RNG, EightFactor data, and normalized model
features are absent.

### Lifecycle composition

OWN state is composed as exact `OwnOperatorLifecyclePayload` values and enemy
state as exact `ObservedEnemyOperatorLifecyclePayload` values.  Phase 1 does
not remap health, movement, suppression, cooldown, or enemy motion.  GREEN and
authoritative lifecycle payloads are rejected by type and are not serialized.

The Decision serializer explicitly names every v1 Perspective Lifecycle axis
and nested field.  It does not recursively call `dataclasses.asdict`.  Exact
payload type, nested type, and frozen field-shape checks fail closed if a
future Perspective field appears without a corresponding Decision schema
review/version bump.

### Capability evidence contract

The Phase 1 types are `DecisionCapabilitySnapshot`,
`OperatorCapabilitySummary`, `CapabilityFamilyStatus`, and
`CapabilityBlockerCode`.  They do not scan Engine state and do not constitute
the Phase 2 adapter.

The stable statuses are `AVAILABLE`, `BLOCKED`, `NOT_APPLICABLE`, `UNKNOWN`,
and `MISSING`.  Their evidence rules are enforced at construction:

- `AVAILABLE` requires an explicit Engine capability record or at least one
  Engine valid-action instance;
- `BLOCKED` requires an Engine capability record plus at least one stable,
  namespaced blocker code;
- `NOT_APPLICABLE` requires explicit applicability authority;
- `UNKNOWN` represents no conclusive Engine evidence; and
- `MISSING` represents an expected producer/reference whose evidence is
  absent.

No-action absence is never normalized to BLOCKED or NOT_APPLICABLE.  Blockers
contain only stable producer/code pairs; there is no presentation or natural
language reason field.

### Action catalog contract

`ActionCatalogReference` binds the action-catalog schema, side, decision
window, frame, simulation time, action count, supplied stable action ids,
supplied actor references, and canonical catalog hash.  It is a reference to
already Engine-produced action evidence, not an action generator.  Phase 1
does not enumerate destinations, paths, targets, target hexes, or previews.

Action ordering remains meaningful only within the bound decision window.
The catalog hash covers that ordered identity list.  An integer list offset is
therefore never represented as a cross-window action identity.

### Pure builder and fail-closed joins

`build_decision_semantic_projection` accepts only already perspective-safe,
detached lifecycle values and already Engine-derived immutable capability and
catalog evidence.  It has no `WorldState` or Engine parameter.

Construction requires exact equality of:

- side across snapshot, context, capability snapshot, and catalog;
- decision-window, frame, and `sim_time_ms` across snapshot/evidence;
- OWN lifecycle, controllable, and capability Operator references;
- enemy lifecycle and current observed-contact references; and
- catalog actor references as a subset of controllable OWN references.

OWN/enemy overlap, side disagreement, schema disagreement, duplicate
references, or any join mismatch raises `DecisionProjectionContractError`.
The builder never silently drops, backfills, chooses the newest record, or
returns a partial projection.

### Semantic hash and deterministic ordering

The projection semantic hash is SHA-256 over canonical JSON/UTF-8.  It covers
the Decision schema, authority metadata, snapshot identity, side-safe context,
Perspective Lifecycle payloads, capability evidence, and ActionCatalog
reference.  It excludes itself, authoritative lifecycle/GREEN/World hashes,
hidden state, RNG, wall clock, reward, winner, EightFactor evidence, renderer,
and tensor formatting.

Objectives, OWN Operators, observed contacts, capability Operators, capability
families, blocker codes, and reference sets are deterministically sorted.
Action ids retain the Engine catalog's window-local order because that order is
part of the current catalog identity.  Reordering set-like inputs therefore
does not change equality/hash; changing the catalog's ordered identities does.

### D1-D22 verification

`tests/test_decision_semantic_projection.py` provides Phase 1 formal coverage:

- D1: repeated safe inputs produce identical immutable values and hashes;
- D2-D5: hidden enemy health, cooldown/suppression, finite lifecycle, and
  boarding differences are not input channels and cannot affect output/hash;
- D6: OWN suppression expiry legitimately changes the composed Decision;
- D7-D8: Guided and Indirect lifecycle changes plus supplied explicit Engine
  evidence legitimately change Decision semantics;
- D9: current observed-contact appearance/removal changes the enemy collection;
- D10-D12: existing Human/Agent authority, training-mask authority, and forged
  action rejection remain existing-system regressions; Phase 1 does not migrate
  those consumers;
- D13: World snapshot/restore plus recomputation produces equal Decision/hash;
- D14: strict replay plus recomputation produces equal Decision/hash;
- D15: set-like input ordering is canonical;
- D16: construction is pure and has no Engine/Operator/Gateway/Training/Viewer
  import dependency;
- D17: inconclusive absence is UNKNOWN and cannot be forged as BLOCKED;
- D18: presentation reason text is absent from the machine contract;
- D19: authoritative lifecycle, GREEN, World, and hidden hashes are absent;
- D20: training supervision fields are structurally absent;
- D21: side/window/frame/reference mismatch fails closed; and
- D22: future/unrecognized lifecycle payload shape cannot auto-leak through
  generic serialization.

### Snapshot, replay, performance, and dependency direction

Decision values remain derived verification artifacts and are not stored in a
World snapshot or used to restore Engine state.  Snapshot/restore and strict
replay verification rebuild the complete safe-input chain before comparing
Decision equality/hash.

Construction is O(OWN Operators + observed contacts + supplied capability
records).  It performs no World scan, LOS, visibility, pathfinding, target or
destination enumeration, action preview, damage lookup, capability
enumeration, Engine step, RNG call, or timer advance.

The dependency remains:

```text
core/rules
  -> detached observation + Perspective Lifecycle + Engine evidence
  -> projection.decision_semantic
  -> future consumer adapters/encoders/renderers
```

Engine, Human, Agent, ControllerView, and Training remain unchanged and do not
import Phase 1.

### Remaining Phase 2 work

Phase 2 may build a versioned adapter from heterogeneous, explicit Engine
valid-action/capability evidence into `DecisionCapabilitySnapshot`, preserving
UNKNOWN and producer provenance.  It may also bind the existing frozen valid
actions into `ActionCatalogReference`.  It must not derive blockers from
lifecycle fields, turn no-action absence into BLOCKED, or move parameterized
query/preview logic into the cheap Decision state.

## Phase 2 Capability / Action Evidence Verification

### Modules and schemas

Phase 2 is implemented in
`src/wargame_native_engine/projection/decision_capability.py`, with the frozen
core contracts retained in `projection/decision_semantic.py`.

No schema was bumped:

- Decision: `native-decision-semantic-projection/1.0`;
- capability snapshot: `native-decision-capability-snapshot/1.0`; and
- action catalog: `native-decision-action-catalog/1.0`.

The implementation adds `ActionCatalogEntry`, `ActionCatalogSnapshot`, and
`ExplicitCapabilityEvidence`, plus pure adapters for the existing producer
records.  It does not add a capability authority, Engine query, consumer
integration, or legality rule.

### Producer inventory

| Producer | Current evidence shape | Decision-boundary interpretation |
|---|---|---|
| `NativeEngine.valid_actions` / frozen DecisionWindow values | concrete `ValidAction` instances | explicit AVAILABLE; exact actor/family instance count |
| Guided query | legal `GuidedFireCapability` opportunities only | tuple present -> explicit AVAILABLE; empty -> no evidence/UNKNOWN |
| Indirect query | weapon-scoped AVAILABLE/BLOCKED records | aggregate per shooter; any available weapon -> family AVAILABLE, otherwise family BLOCKED with stable blocker union |
| Board query | legal `BoardCapability` tuples only | tuple present -> explicit AVAILABLE; empty -> no evidence/UNKNOWN |
| Unload query | legal `UnloadCapability` tuples only | tuple present -> explicit AVAILABLE; empty -> no evidence/UNKNOWN |
| Concealment query | per-Operator `available` plus stable reason | AVAILABLE, explicit BLOCKED, or explicit NOT_APPLICABLE for the producer's unsupported-domain codes |
| MOVE planner / Indirect target query | on-demand parameter validation | excluded from cheap snapshot; retained as query/preview authority |
| Product unavailable reason | presentation text/fallback code | excluded; never capability evidence |

Runtime producers remain heterogeneous.  Their evidence is now
`NORMALIZED_AT_DECISION_BOUNDARY`; they are not falsely described as one
unified Engine capability producer.

### Explicit family mapping

Action family mapping is a closed, explicit v1 table.  DEPLOY,
END_DEPLOYMENT, MOVE, CHANGE_STATE, STOP_MOVE, BOARD, UNLOAD,
ENTER_CONCEALMENT, WEAPON_LOCK, WEAPON_UNFOLD, and WAIT map directly.  ATTACK
and SHOOT map to DIRECT_FIRE.  FIRE requires an exact `fire_mode`: GUIDED maps
to GUIDED_FIRE and INDIRECT maps to INDIRECT_FIRE.  Unknown FIRE modes or new
ActionType values fail closed; there is no substring or UI-name heuristic.

### Capability normalization and absence semantics

`build_decision_capability_snapshot` accepts one typed snapshot identity, the
exact OWN reference set, one real `ActionCatalogSnapshot`, and optional exact
explicit producer records.  It accepts no World, observation, lifecycle, UI,
or Engine object.

For each OWN actor and every frozen v1 family:

1. a concrete catalog instance is AVAILABLE and supplies the exact count;
2. a compatible explicit AVAILABLE record may coexist;
3. explicit AVAILABLE with no concrete entry remains AVAILABLE with count 0;
4. explicit BLOCKED requires namespaced stable blocker codes;
5. explicit NOT_APPLICABLE requires the producer's applicability evidence;
6. an explicitly missing required producer becomes MISSING; and
7. ordinary evidence absence is UNKNOWN.

Empty action lists never become BLOCKED or NOT_APPLICABLE.  UNKNOWN is a
normal, intentional v1 state.

### Evidence conflict handling

A concrete valid action conflicting with explicit BLOCKED, NOT_APPLICABLE, or
MISSING evidence raises `DecisionProjectionContractError`.  Duplicate explicit
records for one actor/family also fail closed rather than choosing a producer.

Indirect is normalized inside its producer adapter before cross-producer
merge: multiple weapon records for one shooter are a family opportunity set,
not conflicting family authorities.  At least one available weapon means the
family is available; only an all-blocked set creates a family blocker record.

### Capability snapshot hash

`DecisionCapabilitySnapshot.semantic_hash` is computed automatically as
SHA-256 over canonical JSON containing schema, side/window/frame/simulation
time, sorted Operator references, sorted families, status, evidence kind,
producer namespace, exact available-instance count, and sorted blocker codes.

This is a derived snapshot evidence hash.  It is not and does not pretend to be
a global Engine capability authority version/hash.  UI text, World/GREEN,
hidden state, RNG, wall clock, exception strings, and Python repr are excluded.

### ActionCatalog snapshot and hash

`build_action_catalog_snapshot` accepts only exact frozen `ValidAction` values.
It explicitly copies the EngineAction fields and canonicalizes its authorized
parameter mapping, so later mutation of a source mapping cannot mutate the
catalog.  It performs no action generation.

Each immutable entry contains action id, actor reference, family, exact action
type, target reference/position, canonical detached parameters, priority,
producer namespace, complete concrete-operation hash, and entry semantic hash.
The snapshot preserves Engine frozen order exactly.  Duplicate action ids and
duplicate concrete semantic actions fail closed.

The catalog hash binds schema, side, decision window, frame, exact simulation
time, ordered action ids, ordered entry hashes, and sorted actor references.
Entry hashes transitively cover every formal entry field.  Consequently index
`i` is meaningful only within `(side, decision_window_index, catalog_hash)`;
`action_id` remains the stable action identity.

### Hybrid parameterized action model

The catalog contains only actions already supplied by Engine evidence.  It
does not enumerate arbitrary MOVE destinations/paths or Indirect target hexes.
Explicit parameter-query capability may therefore be AVAILABLE with zero
catalog instances.  Destination and target validation remain in the existing
planner/query/preview and final Engine validation paths.

This split is now `EXPECTED_BY_DESIGN`, not an invitation for the adapter to
scan a map or duplicate legality.

### Decision binding and window atomicity

`build_decision_from_snapshots` accepts a real `ActionCatalogSnapshot` and
derives its `ActionCatalogReference` internally.  The Phase 2 path does not
accept a caller-authored reference.  It verifies exact side/window/frame/time
identity and that every concrete catalog actor/family count matches an
AVAILABLE capability status before delegating to the Phase 1 pure builder.

The reference carries ordered entry hashes, so its catalog hash is
self-validating.  A modified/forged catalog hash is rejected.  An old catalog
remains immutable for audit, but existing DecisionWindow/Gateway authorization
continues to reject an old action id in a later window.

### C1-C28 verification

`tests/test_decision_capability_snapshot.py` covers:

- C1-C5: detached catalog construction, Engine-order preservation, duplicate
  rejection, deterministic hash, and window-local catalog identity;
- C6-C7: stale-id Gateway rejection and exact NativeActionEncoder order/mask;
- C8-C10: concrete AVAILABLE counts and parameterized explicit AVAILABLE with
  zero instances;
- C11-C14: real Indirect BLOCKED, ordinary UNKNOWN, real Concealment
  NOT_APPLICABLE, and explicit MISSING;
- C15: contradictory AVAILABLE/BLOCKED evidence fails closed;
- C16-C17: presentation text exclusion and stable producer/blocker namespaces;
- C18-C20: exact side/window/frame/time/actor joins and OWN-only capability;
- C21: enemy health, suppression, cooldown, lifecycle, and boarding hidden
  changes leave catalog/capability/Decision equality and hashes unchanged;
- C22: legitimate OWN suppression, Guided recovery, and Indirect cooldown
  evidence changes remain visible;
- C23-C24: snapshot/restore and strict replay rebuild identical catalog,
  capability, and Decision artifacts;
- C25-C26: real reference binding and forged hash rejection; and
- C27-C28: unknown future action/evidence subclasses fail closed.

Additional tests freeze the explicit ActionType family table, real
Guided/Board/Unload/Concealment producer adapters, mutable-parameter
detachment, and the absence of World/Engine/lifecycle/consumer dependencies.
The Phase 1 D1-D22 suite remains intact.

### Purity, replay, and cost

Both adapters are deterministic value transforms.  They perform no World read
or mutation, observation/lifecycle/producer mutation, Engine call, visibility,
LOS, target enumeration, map scan, preview, RNG, or wall-clock access.
Construction cost is O(frozen actions + explicit records + OWN actors x frozen
family count).

Catalog, capability, and Decision values remain derived artifacts.  They do
not enter the authoritative World snapshot or replay manifest.  Snapshot and
strict replay verification reconstruct Engine evidence and then rebuild all
three artifacts.

### P2 reclassification and Phase 3 readiness

- `HETEROGENEOUS_RUNTIME_CAPABILITY_RECORDS` remains P2,
  `NORMALIZED_AT_PROJECTION_BOUNDARY`;
- `PRODUCT_UNAVAILABLE_REASON_IS_PRESENTATION_EVIDENCE` remains a documented
  P2 boundary but is `CONTAINED` outside machine evidence;
- `VALID_ACTIONS_AND_PARAMETER_QUERIES_ARE_SPLIT` is
  `EXPECTED_BY_DESIGN` under the HYBRID contract;
- `CAPABILITY_AUTHORITY_HAS_NO_SINGLE_VERSION_HASH` remains P2; the snapshot
  hash is not a substitute; and
- `ACTION_INDEX_IS_WINDOW_LOCAL` is `EXPECTED_BY_DESIGN` and explicitly bound
  by catalog identity.

Side score, ControllerView parallel representations, and the existing
lifecycle P2 items remain outside this phase.  With the adapter boundary
closed, ControllerView/Human/Agent integration is ready for a separate Phase 3
without changing Engine authority.

## Phase 3 ControllerView / Human / Agent Integration

### ControllerView construction chain

The normal RED/BLUE controller path now constructs one machine snapshot at the
shared Session/Gateway boundary:

`frozen SideObservation -> authoritative-derived OWN lifecycle + observed-enemy
lifecycle -> explicit Engine capability evidence + frozen window ValidAction
records -> DecisionCapabilitySnapshot + ActionCatalogSnapshot ->
SideDecisionSemanticProjection -> ControllerView`.

`gateway/controller_decision.py` owns this composition.  It receives no
Controller, Human, Agent, Training, Viewer, or renderer object.  It performs no
visibility calculation, target discovery, pathfinding, preview, or action
generation.  NativeEngine remains unaware of ControllerView and Decision
Projection.  NativeEngineBackend exposes only detached authoritative-derived
OWN lifecycle values and existing read-only capability producer results; it
never exposes WorldState to a controller.

`SessionManager.controller_view` is the single normal constructor.  The
realtime CommandGateway supplies the exact `DecisionWindow` action snapshot,
window index, fact frame, and simulation time instead of building a view and
then replacing its actions.  Terminal post-window views bind the completed
window identity to the current terminal fact frame/time and an empty catalog;
stale pre-terminal actions are not advertised after termination.

### Semantic core and legacy compatibility fields

`ControllerView` now carries:

- required-on-normal-path `decision_semantic: SideDecisionSemanticProjection`;
- required-on-normal-path `action_catalog: ActionCatalogSnapshot`;
- legacy detached `observation`, side-safe Source frame, and `valid_actions`;
  and
- legacy outcome/presentation fields.

The dataclass permits an explicitly legacy-only construction for old fixtures
and compatibility callers, but a partially populated core is rejected.  Every
normal SessionManager/Gateway view always has both core values.  Construction
validates exact side/window/frame/simulation-time identity, terminal flags,
the real catalog reference, action count, per-index action id, and full
concrete-operation hash.  Contract failures propagate; there is no silent
fallback to a core-less normal view.

Legacy `valid_actions` and catalog entries are derived from the same frozen
`ValidAction` sequence.  The catalog is never used to regenerate legacy
actions, and capability state is never used to invent actions.  Catalog
families with concrete entries must be AVAILABLE with the exact count;
parameter-query AVAILABLE with zero entries remains valid.

Legacy `ControllerView.to_record()` remains an explicit compatibility
serialization and deliberately does not auto-expand the new fields.  The new
`decision_record()` explicitly serializes only the Decision semantic and safe
catalog contracts.  Consequently `controller_view_record` and existing Viewer
DTOs did not accidentally acquire semantic hashes or new debug structure.

### Same-window atomicity and stale actions

The Gateway exposes a detached copy of its exact ordered `ValidAction` map via
`DecisionWindow.frozen_valid_actions`.  That one sequence feeds legacy action
records and the catalog.  The ControllerView validator binds the catalog and
Decision reference to the same side, decision window, frame, and exact
authoritative `sim_time_ms`.  Source `SideObservation.tick` is intentionally
not treated as a fact-frame identifier; they are distinct existing clocks.

Old ControllerView, catalog, capability, and Decision objects remain immutable
when a later window opens.  Human staging resolves the selected `action_id`
through the current catalog; Gateway/DecisionWindow still performs final
window authorization.  Classic DecisionAgent, product-seat Agent, realtime
Agent, and Human paths therefore cannot make an old catalog authorize a stale
action.  Existing stale-window Gateway tests continue to reject it.

RED and BLUE views issued for one joint window share the same fact frame and
simulation time.  Building the second view does not mutate or advance the
pre-state, so no later actor observes a staged same-window result.

### Human integration

`SideController.decision_core()` and
`ProductOperationService.decision_core()` provide detached machine-safe core
values.  Product status now derives side/frame/time/terminal semantics from
`DecisionSnapshotIdentity`.  Human capability output includes exact selected
Operator machine capability records from the Decision snapshot, while UI
messages remain presentation-only.

Concrete Human staging resolves `action_id` and actor/action-family display
identity from `ActionCatalogSnapshot`; submission and all parameterized
MOVE/Indirect/other previews still use the existing ProductOperation,
Gateway, and Engine validation path.  `UNKNOWN` is presented as incomplete
capability evidence rather than falsely translated to an authoritative
BLOCKED state.  Natural-language unavailable reasons never enter capability,
catalog, or Decision hashes.

Human capability construction can consume the already-frozen ControllerView.
Dual-control and realtime paths now pass that same view instead of rebuilding
a second semantic/capability snapshot.  Each explicit capability producer is
collected exactly once per ControllerView build.  Existing on-demand Indirect
parameter details remain a separate query as required by the HYBRID contract.

### Agent integration

Product-seat `AgentDecisionContext` now carries detached
`decision_semantic` and `action_catalog` values alongside its versioned legacy
observation/capability fields.  The product Agent runner explicitly requests
both from NativeDecisionSession, and realtime issuance populates both from the
exact ControllerView snapshot.  NativeDecisionSession's default Training-facing
context remains legacy-only.  RealtimeSeatView keeps
the paired core immutable across observation versions and offers an explicit
machine serialization.

`RandomProductSeatAgent` uses Decision capability availability and catalog
entries on the normal new path; its old fields are only a compatibility
fallback for legacy test/protocol instances.  Parameterized actions still
become `SeatOperation` intent and pass through formal preview/submission.

The classic Agent protocol gains a separate versioned `DecisionAgent`
`act_decision` path.  LocalAgentMatchRunner supplies the same shared builder's
detached semantic/catalog pair when an Agent implements that method; old
`Agent.act(observation, valid_actions, context)` remains supported unchanged.
Neither path receives WorldState, GREEN, authoritative lifecycle hashes, or
hidden enemy state.

### Noninterference, snapshot, replay, and training regression

`tests/test_controller_decision_integration.py` verifies:

- C1-C6: exact side/window/frame/time binding, legacy/catalog per-index
  identity, catalog/capability consistency, deterministic hashes, and old-view
  immutability;
- C7-C10: snapshot/restore rebuild equality, explicit serialization, no
  generic Viewer expansion, and fail-closed mismatch/new-field boundaries;
- H1-H8: Human core delivery, catalog action-id staging, machine capability
  equality, preview/final-authority preservation, presentation isolation, and
  no downstream World/GREEN dependency;
- A1-A10: detached product, classic, and realtime Agent core delivery,
  action/catalog binding, window-local identity, stale rejection, and shared
  Human/Agent machine authority;
- hidden enemy health, suppression, weapon/Guided/Indirect timers, limited
  lifecycle, movement task, and boarding relation changes leave Decision,
  capability, catalog, and hashes unchanged when legal side/action evidence is
  unchanged;
- snapshot restore rebuilds equal ControllerView core artifacts, while the
  existing Phase 1/2 D13-D14 and C23-C24 strict-replay tests rebuild equal
  lifecycle, catalog, capability, and Decision hashes from replayed World; and
- T1-T5 preserve the legacy Training observation/mask/action-id path and prove
  NativeActionEncoder order equals catalog order.  Training is not migrated in
  Phase 3.

Decision construction and repeated Human/Agent consumption do not mutate the
World, observation, lifecycle values, producer records, actions, RNG, or
simulation time.  No reward, winner, outcome, EightFactor, World hash, GREEN
payload, or authoritative lifecycle hash enters the machine Decision record.

### Performance and P2 reclassification

One ControllerView build is O(own Operators + visible contacts + frozen valid
actions + supplied capability records), with the fixed v1 family factor.  It
adds no LOS, visibility, map scan, pathfinding, target enumeration, preview, or
duplicate downstream capability evaluation.

- `CONTROLLERVIEW_PARALLEL_REPRESENTATIONS` is `MIGRATION_IN_PROGRESS /
  CONTAINED`: Decision semantic is the standard machine core; observation,
  Source frame, and valid_actions remain explicit compatibility surfaces for
  Training, Viewer, and older protocols.
- `HETEROGENEOUS_RUNTIME_CAPABILITY_RECORDS` remains
  `NORMALIZED_AT_PROJECTION_BOUNDARY`.
- `VALID_ACTIONS_AND_PARAMETER_QUERIES_ARE_SPLIT` remains
  `EXPECTED_BY_DESIGN`.
- `ACTION_INDEX_IS_WINDOW_LOCAL` remains `EXPECTED_BY_DESIGN`; Human and Agent
  select stable `action_id`, while the encoder index is catalog/window-local.
- `PRODUCT_UNAVAILABLE_REASON_IS_PRESENTATION_EVIDENCE` remains `CONTAINED`.
- `CAPABILITY_AUTHORITY_HAS_NO_SINGLE_VERSION_HASH` and
  `SIDE_SCORE_TYPED_SOURCE_GAP` remain P2.
- stale raw suppression compatibility, concealment raw/typed restoration,
  operator-global weapon cooldown ambiguity, and the missing typed
  CLOSE_COMBAT axis remain unchanged lifecycle P2 items.

The semantic core is now ready for a separate Phase 4 ObservationEncoder,
ActionEncoder/catalog, and Training integration.  No Training tensor, mask,
reward, renderer, prompt, or tool schema was migrated here.
