# Decision Training Encoder v1

## Status

`DECISION_OBSERVATION_ENCODER_V1_IMPLEMENTED`

`DECISION_ACTION_ENCODER_V1_IMPLEMENTED`

This contract is a Native simplified-rules training interface. It is not an
official or SDK-equivalent interface.

## Authority boundary

The policy-input path is:

```text
ControllerView.decision_semantic
    -> DecisionObservationEncoder
    -> numerical policy observation

ControllerView.action_catalog
    -> DecisionActionEncoder
    -> window-local mask/index
    -> catalog action_id
    -> Gateway / NativeEngine revalidation
```

The encoders accept no `WorldState`, `OperatorState`, GREEN frame, Engine,
visibility service, or capability producer. Reward, winner, outcome,
EightFactor, evaluation evidence, and hashes are excluded from policy
features. Training `info` remains an out-of-band supervision/debug channel.

## Versions and hashes

| Contract | Version | Contract hash |
|---|---|---|
| Observation encoder | `native-decision-observation-encoder/1.0` | `a477d7953c4f3e0a52f13aa2bc12ea08d1327fff7a8bdfe8d91c41a9e51ed750` |
| Action encoder | `native-decision-action-encoder/1.0` | `aa08ad27d84634f76d607d7890abe953a911b57b4043b311fdc85dd2813b83dc` |
| Decision semantic input | `native-decision-semantic-projection/1.0` | runtime semantic hash is not a feature |
| Action catalog input | `native-decision-action-catalog/1.0` | runtime catalog hash binds resolution but is not a feature |

The encoder hashes cover layout, enum mappings, padding, units, capacity, and
index semantics. They do not cover runtime values.

## Observation layout

The default configuration is 64 own Operators, 64 observed contacts, 32
objectives, and 256 catalog actions. Numerical dtype is Python `float`, frozen
as the v1 `float64` contract. Timers remain integer-millisecond semantics
converted exactly to floats; no ungrounded timer normalization is applied.
Only `health_ratio = health / max_health` is derived, with zero used when
`max_health <= 0`.

The flattened default shape is `(8302,)`:

| Flat index | Group | Row shape | Source | Missing/padding |
|---|---:|---:|---|---|
| 0–13 | snapshot | 14 | Decision snapshot/context | explicit presence bits for optional terminal rules |
| 14–205 | objectives | 32 × 6 | side-safe objectives | zero row + objective mask false |
| 206–4493 | own Operators | 64 × 67 | `OwnOperatorLifecyclePayload` | presence bits for optional values; zero row + entity mask false |
| 4494–5325 | observed enemies | 64 × 13 | `ObservedEnemyOperatorLifecyclePayload` | presence bits for source codes; zero row + entity mask false |
| 5326–8077 | capabilities | 64 × 43 | `DecisionCapabilitySnapshot` | fixed 14-family layout; zero row + mask false |
| 8078–8109 | objective mask | 32 | encoder | `False` padding |
| 8110–8173 | own mask | 64 | encoder | `False` padding |
| 8174–8237 | enemy mask | 64 | encoder | `False` padding |
| 8238–8301 | capability mask | 64 | encoder | `False` padding |

### Snapshot feature order

`side, stage, sim_time_ms, terminated, truncated, objective_count,
own_operator_count, observed_enemy_count, action_count,
terminal_elimination, objective_score_to_win_present,
objective_score_to_win, max_ticks_present, max_ticks`.

### Objective feature order

`objective_reference, x, y, radius, value, control_relation`.

Control relation is `0=uncontrolled`, `1=own`, `2=opponent`.

### Own Operator feature order

The 67 fields are explicitly listed by `OWN_OPERATOR_FEATURES` in
`training/decision_encoder.py`. They cover stable reference/domain/identity
status; authoritative own survival; deployment/current hex; typed movement;
transition; fatigue; suppression; concealment; boarding; weapon lifecycle;
Guided and Indirect recovery; and limited lifecycle. Optional numerics use a
separate presence feature. Internal integrity, authoritative hashes, raw
compatibility fields, paths, and debug evidence are absent.

### Observed enemy feature order

`operator_reference, domain, canonical_status, source_type_code_present,
source_type_code, source_subtype_code_present, source_subtype_code, x, y,
altitude, alive, destroyed, motion`.

No enemy health, suppression, cooldown, concealment, boarding, lifecycle,
movement task, or capability field exists in this row.

### Capability feature order

Each own-Operator row begins with `operator_reference`, followed by three
features for every family: `status, instance_count_present, instance_count`.
The fixed order is:

`DEPLOY, END_DEPLOYMENT, MOVE, STOP_MOVE, WAIT, DIRECT_FIRE, GUIDED_FIRE,
INDIRECT_FIRE, BOARD, UNLOAD, ENTER_CONCEALMENT, WEAPON_LOCK, WEAPON_UNFOLD,
CHANGE_STATE`.

Status mapping is `UNKNOWN=0`, `MISSING=1`, `NOT_APPLICABLE=2`, `BLOCKED=3`,
`AVAILABLE=4`. Blocker codes and presentation text are not v1 features.

## Enum mappings

All enum tables are explicit code dictionaries included in the observation
contract hash. Python `hash()` and implicit Enum ordinals are forbidden.
Unknown, missing, not-applicable, blocked, and available values remain
distinct. A new/unmapped value fails closed and requires an encoder-version
update.

## Padding and capacity

Padding rows contain `0.0` and their corresponding entity mask is `False`.
This distinguishes padding from a real all-zero row. Entity ordering is the
stable ordering already frozen by Decision Projection; the encoder does not
re-sort by distance, health, or capability.

Action mask capacity defaults to 256. For N catalog entries,
`mask[0:N] = True` and `mask[N:capacity] = False`. Overflow fails closed; valid
actions are never truncated. A zero-entry catalog produces an all-false mask
and no synthetic WAIT action.

An initial-catalog probe across 18 checked-in runnable example scenarios (36
side snapshots) measured: min 1, median 4, p95 56, max 72 entries. This is a
repository probe, not a proof that every future scenario fits capacity 256.

## Action resolution

An index is meaningful only for the exact `(side, decision_window_index,
frame_index, sim_time_ms, catalog_hash, encoder_contract_hash)` binding.
Resolution reads `catalog.entries[index].action_id`; it never reconstructs an
Engine action. Old-window indices cannot be rebound to a new catalog. Gateway
and Engine retain final stale/parameter legality authority.

`NativeMultiSeatEnv.step_action_indices` resolves RED and BLUE indices against
their respective same-pre-state catalogs, then submits both side-bound action
ID batches into one joint decision window. Structured `SeatDecision` remains a
legacy/parameter-query compatibility path.

## Training migration

`NativeTrainingEnv` defaults to `decision-v1`. Its emitted observation is the
explicit numerical `policy_record()` and its action space comes from the
ControllerView catalog. It stores the emitted Decision/Catalog binding and
fails closed if the window changes before action resolution.

The explicit selector `legacy-side-observation` retains the old
`SideObservationEncoder` and `NativeActionEncoder` for old checkpoints and
compatibility tests. It is not the default authority.

`NativeMultiSeatEnv` emits Decision-v1 observations and per-side encoded
catalog action spaces. `SingleSeatTrainingAdapter` forwards the selected
side's Decision-v1 observation/action space. Agent-facing
`NativeDecisionSession.decision_context` remains separate from the training
tensor contract.

## Legacy differential

| Feature | Legacy source | Decision-v1 source | Result |
|---|---|---|---|
| Identity | raw side-observation strings | domain/status and legal numeric source codes | intentional versioned numeric mapping |
| Position | friendly x/y plus contact x/y | own current hex; enemy legal x/y/altitude | intentional typed lifecycle composition |
| Health | friendly exact health; enemy already hidden | own lifecycle health and ratio; enemy absent | same authority, new numeric layout |
| Movement | mixed legacy state/mode fields | typed own lifecycle; coarse enemy motion | intentional semantic correction |
| Enemy info | side safe-list JSON | fixed 13-field observed-contact row | same permission, fixed shape |
| Suppression | friendly typed timer copied through observation | own lifecycle suppression | same authority, explicit ms fields |
| Cooldowns | friendly observation timers | weapon/Guided/Indirect own lifecycle | same authority, typed applicability/presence |
| Objectives | side observation JSON | side-safe Decision objectives | same authority, numeric row/mask |
| Capability | separate presentation/valid-actions surface | fixed DecisionCapabilitySnapshot matrix | intentional new semantic group |
| Mask | legacy valid-action tuple | exact ActionCatalog order | same Engine-authorized set/order, new binding |

Old observation shape/checkpoints are not binary-compatible with Decision-v1.
They must select the legacy interface or migrate with the encoder version/hash.

## Supervision isolation

Reward and terminal evaluation are calculated after Engine advancement and are
returned separately by training result objects. Winner/outcome may remain in
`info` or publication records but never enter `policy_record()`. EightFactor,
GREEN labels, value targets, advantages, episode return, and history are not
encoder inputs.

## Verification

Formal E1–E35 tests cover deterministic encoding; hidden-field
noninterference; visible sensitivity; explicit enum mappings; numerical
safety; padding; capacity overflow; catalog ordering; window-local index
resolution; stale rejection; snapshot/restore; strict replay; RED/BLUE
isolation; joint same-pre-state submission; supervision exclusion; absence of
World/Engine reads; space containment; terminal/zero-action behavior;
multi-environment isolation; explicit serializers; and future-field/family
fail-closed behavior.

## Remaining boundaries

Reward redesign, EightFactor integration, LLM rendering, capability-authority
version unification, side-score typing, raw suppression compatibility,
concealment import compatibility, operator-global weapon cooldown ambiguity,
and the missing typed CLOSE_COMBAT lifecycle axis remain out of scope.
