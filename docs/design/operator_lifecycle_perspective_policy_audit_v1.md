# Operator Lifecycle Perspective Projection Policy Freeze Audit v1

## 1. Executive Summary

This audit is read-only. It does not implement a perspective projector, change
`SideObservation`, or connect the authoritative lifecycle View to any Human,
Agent, Viewer, Replay, or training API.

The internal lifecycle source is ready: `AuthoritativeOperatorLifecycleView`
is deterministic, immutable, and not currently reachable from RED/BLUE APIs.
GREEN policy is therefore ready. The side policy is not closed:

- current contact membership is correctly decided by Visibility/Observation;
- a lost contact is absent rather than silently updated from GREEN;
- raw enemy attributes, Native evidence, weapons, ammunition, and relationship
  identifiers are deliberately removed;
- stable contact ID, side, exact current position/altitude, legacy `state`, and
  `alive` have explicit current observation/config evidence;
- exact observed equipment/model identity is allowed by the previously frozen
  project policy, although the Source perspective JSON has not been updated to
  state that policy consistently;
- exact enemy `health/max_health`, `heading`, and `deployed` are present in
  `SideObservation`; exact health and identity are also copied into Source
  `extra.observation`, even though the configured enemy safe-list contains none
  of those fields; and
- no authority currently permits continuous enemy lifecycle telemetry such as
  remaining suppression, weapon, Guided, Indirect, or limited-lifecycle time.

The exact-health mismatch is an existing **P1 observation-policy drift**. It is
not created by the lifecycle View: Human, Agent, training, and browser control
already receive the richer `SideObservation`. Perspective projection should
not be implemented on top of this unresolved boundary.

```text
GREEN_PERSPECTIVE_POLICY_READINESS: READY
OWN_PERSPECTIVE_POLICY_READINESS: READY_AFTER_POLICY_FREEZE
OBSERVED_ENEMY_PERSPECTIVE_POLICY_READINESS: READY_AFTER_POLICY_FREEZE
PERSPECTIVE_NONINTERFERENCE_TEST_READINESS: PARTIAL
PERSPECTIVE_PROJECTION_IMPLEMENTATION_READINESS: READY_AFTER_P1_FIX
```

## 2. Scope and Non-goals

The scope is the policy boundary from the existing internal lifecycle View to
GREEN, OWN, and OBSERVED_ENEMY lifecycle representations. The audit freezes
evidence sources, safe fields, hidden-state behavior, version/hash rules, and
future test contracts.

It does not implement a projector, visibility, contact memory, action masks,
capability projection, Decision Semantic Projection, encoders, reward,
training state, UI rendering, or a new rule. It makes no World/Operator field
authoritative and does not change Engine legality.

## 3. Existing Observation Authority

The current authoritative flow is:

```text
WorldState
  -> Native Visibility / per-observer contact refresh
  -> NativeEngine.observe(side)
  -> detached SideObservation
       friendly_operators: full OperatorObservation + copied raw attributes
       visible_enemies:    OperatorObservation with raw_attributes={}
  -> GameSession / Gateway
       observation: SideObservation.to_dict()
       source_frame: RED or BLUE Source-compatible projection
       valid_actions: Engine-generated own actions
```

Evidence:

- `core/engine.py:384-405` constructs friendlies and visible enemies after the
  visibility authority has selected contact membership.
- `core/observation.py:11-43` defines the actual contact payload: stable real
  ID, side, type/subtype, position, altitude, heading, exact health, legacy
  state, alive, deployed, and raw attributes.
- enemy raw attributes are explicitly omitted at `core/engine.py:389-391`.
- `source_perspective_policy_v1.json:10-21` requires safe-field rebuilding,
  GREEN-only Native evidence, and fail-closed unknown fields.
- its configured enemy safe-list is only `obj_id`, `color`, `cur_hex`,
  `altitude`, `state`, and `alive` (`:44-51`).
- `observations/perspective.py:232-262` rebuilds the Source contact from
  `SideObservation`; its public fields mask type/blood/weapon/relation data,
  but `extra.observation` adds exact health/type/subtype.
- the rules state that a target satisfying LOS and observation range is shown
  in the situation (`docs/rules/推演规则_完整纯文本_Final.md:103-121`). They
  do not define exact health, cooldown, recovery, boarding, or lifecycle
  telemetry.

Contact identity is currently the stable native `operator_id`/Source `obj_id`.
The repository has no perceived-contact-ID layer. A lifecycle projector must
not invent one. Contact memory is also absent: when a target leaves current
visibility, it leaves `visible_enemies` and the side frame. Any future
last-known memory belongs to Observation/Agent memory, not lifecycle projection.

## 4. Current RED/BLUE/GREEN Data Flow

### GREEN

GREEN is the complete referee Source frame and may recompute the complete
authoritative lifecycle View. It remains derived: the View does not become
World authority and is not restored into the World.

### RED and BLUE

RED and BLUE are symmetric. Each `ControllerView` contains two parallel side
representations (`gateway/session_manager.py:565-589`):

1. `observation`: the rich detached `SideObservation` described above;
2. `source_frame`: the Source-compatible RED/BLUE projection.

Classic Agents receive a detached `SideObservation` and frozen native valid
actions. Realtime/Human consumers receive the detached `ControllerView`. The
training observation encoder clones `ControllerView.observation`; it does not
read GREEN, but it inherits every enemy field in `SideObservation`.

The lifecycle perspective flow must therefore be observation-first for an
enemy:

```text
World / Authoritative LifecycleView
          +
legal SideObservation evidence
          +
explicit versioned allow-list policy
          -> observed-enemy lifecycle payload
```

It must not be `full lifecycle View -> delete a few fields`. Visibility, LOS,
terrain, range, and detection remain exclusively owned by Observation.

## 5. Consumer Drift

| Consumer | Enemy fields actually reachable | Source | Drift |
|---|---|---|---|
| Classic Agent | full `OperatorObservation` contact fields, including exact health/type/subtype/heading/deployed; no enemy raw | detached `SideObservation` | exact health/heading/deployed exceed configured safe-list |
| Realtime Agent | same side observation plus frozen valid actions | `agent_step_input` / `ControllerView` | same drift; no GREEN object is supplied |
| Human controller/browser | both rich observation and side Source frame | `ControllerView.to_record()` | public Source record masks fields, but the same response still contains rich observation |
| Viewer Source units | public type/blood/weapon/relation fields masked; exact health/type/subtype remain in `extra.observation` | `PerspectiveProjector._visible_enemy()` | code/config safe-list mismatch |
| Training encoder | a clone of `ControllerView.observation` | `training/observation_encoder.py` | no GREEN channel, but inherits rich enemy fields |
| Production Source replay recorder | numbered GREEN frames only by repository contract | recorder boundary | not a side observation API |

`CONSUMER_DRIFT: FAIL`.

Human and Agent are not differently authorized; both receive the same rich
contact type. The serious drift is between the declared safe-list, the public
Source-shaped contact, and the additional `SideObservation`/`extra.observation`
fields. Display code choosing not to render a value does not remove it from the
transport contract.

## 6. Field-by-field Policy Matrix

Statuses use only `FULL`, `COARSE`, `HIDDEN`, `NOT_OBSERVED`, and
`POLICY_UNDEFINED`. `FULL` means full for the named semantic, not access to raw
Operator data.

| Field | GREEN | OWN | OBSERVED_ENEMY | Source evidence | Projection form / leak risk | Policy status |
|---|---|---|---|---|---|---|
| operator reference | FULL | FULL | FULL | `operator_id`/`obj_id` is stable in both side schemas | observed stable ID only; no new memory | FULL |
| side | FULL | FULL | FULL | `side`/`color` in current contact | stable enum/value | FULL |
| canonical identity | FULL | FULL | FULL | current `OperatorObservation`; prior user-frozen policy permits exact observed equipment/model | must come from observation evidence, never full View fallback | FULL; config synchronization required |
| source type/subtype | FULL | FULL | FULL | same frozen identity policy and current observation | exact observed identity; no raw fallback | FULL; config synchronization required |
| domain | FULL | FULL | FULL | deterministic mapping from the legally observed identity | derived only from disclosed identity | FULL |
| alive | FULL | FULL | FULL | configured safe field and current observation | boolean | FULL |
| destroyed | FULL | FULL | FULL | derived solely from disclosed `alive` | boolean; not blood-derived | FULL |
| exact health/max health | FULL | FULL | POLICY_UNDEFINED | currently emitted, but absent from configured safe-list and rule docs | exact telemetry; current P1 drift | POLICY_UNDEFINED |
| blood/max blood | FULL | FULL | HIDDEN | Source enemy record explicitly sets both to `None` | omit from enemy schema | HIDDEN |
| deployed | FULL | FULL | POLICY_UNDEFINED | current rich observation exposes it; safe-list does not | can disclose internal deployment/boarding state | POLICY_UNDEFINED |
| spatial status (on-map/on-board/off-map) | FULL | FULL | HIDDEN | no enemy lifecycle evidence; hidden passengers are tested | omit | HIDDEN |
| current position/hex | FULL | FULL | FULL while observed; otherwise NOT_OBSERVED | configured `cur_hex`; observation position | exact current contact only; no last-known updates | FULL / NOT_OBSERVED |
| altitude | FULL | FULL | FULL while observed | configured safe field | exact observed altitude | FULL |
| heading | FULL | FULL | POLICY_UNDEFINED | rich observation only; absent from safe-list | exact orientation may reveal intent | POLICY_UNDEFINED |
| movement state | FULL | FULL | COARSE | safe-listed legacy `state`, but no typed movement evidence | only an explicitly frozen observed-contact category; never typed PAUSED detail | COARSE mapping requires freeze below |
| movement mode | FULL | FULL | HIDDEN | no enemy safe field | omit NORMAL/MARCHING/CHARGE | HIDDEN |
| active segment | FULL | FULL | HIDDEN | no observation evidence | reveals execution internals | HIDDEN |
| task existence | FULL | FULL | HIDDEN | no observation evidence | reveals future intent | HIDDEN |
| task length | FULL | FULL | HIDDEN | full path already excluded from authoritative lifecycle API; no enemy evidence | omit | HIDDEN |
| pause pending | FULL | FULL | HIDDEN | no observation evidence | reveals impending interruption | HIDDEN |
| pause reason | FULL | FULL | HIDDEN | no observation evidence | reveals suppression/internal blocker | HIDDEN |
| operator/stop transition kind | FULL | FULL | POLICY_UNDEFINED | legacy `state=TRANSITIONING` is safe-listed, typed kind is not | at most coarse transition presence | POLICY_UNDEFINED |
| transition remaining ms | FULL | POLICY_UNDEFINED | HIDDEN | own Source has selected transition seconds, but no unified typed-view contract | exact timing telemetry | OWN policy undefined; ENEMY hidden |
| fatigue level/state | FULL | FULL | HIDDEN | no enemy evidence | omit | HIDDEN |
| fatigue recovery elapsed/remaining | FULL | POLICY_UNDEFINED | HIDDEN | no formal side lifecycle contract | exact internal timer | POLICY_UNDEFINED for OWN; HIDDEN enemy |
| suppression active | FULL | FULL | COARSE or POLICY_UNDEFINED | legacy `state=SUPPRESSED` can be public; typed timer is absent | disclose only if policy accepts public state as observation evidence | requires freeze |
| suppression remaining ms | FULL | POLICY_UNDEFINED | HIDDEN | typed value not in current observation; raw seconds may be stale | exact timer never from raw | OWN undefined; ENEMY hidden |
| concealment posture | FULL | FULL | HIDDEN | enemy raw removed; concealment audit says detected does not disclose posture | contact existence must not expose CONCEALED/EXPOSED | HIDDEN |
| boarding state/role | FULL | FULL | HIDDEN | hidden passenger/carrier relations are explicitly cleaned | omit state and shape | HIDDEN |
| boarding transition | FULL | FULL | HIDDEN | no enemy evidence | omit kind and active flag | HIDDEN |
| boarding relation | FULL internally/separate relation view | FULL subject to relation policy | HIDDEN | configured relation-ID cleaning and tests | never emit hidden IDs | HIDDEN |
| boarding remaining ms | FULL | POLICY_UNDEFINED | HIDDEN | no lifecycle side contract | exact timer | OWN undefined; ENEMY hidden |
| weapon posture | FULL | FULL | HIDDEN | enemy Source fields explicitly `None` | firing evidence is not continuous posture telemetry | HIDDEN |
| weapon cooldown active/clear | FULL | FULL | HIDDEN | enemy Source fields explicitly `None` | do not infer from hidden exact state | HIDDEN |
| weapon cooldown remaining ms | FULL | FULL (existing own Source time) | HIDDEN | own-unit Source-full policy; enemy field null | exact own only | HIDDEN enemy |
| Guided recovery active/ready | FULL | POLICY_UNDEFINED | HIDDEN | no side observation field | readiness is internal capability recovery | OWN freeze required; ENEMY hidden |
| Guided recovery remaining ms | FULL | POLICY_UNDEFINED | HIDDEN | no observation evidence | exact 75 s telemetry | OWN freeze required; ENEMY hidden |
| Indirect planning cooldown active/ready | FULL | POLICY_UNDEFINED | HIDDEN | no side observation field | shooter planning availability | OWN freeze required; ENEMY hidden |
| Indirect cooldown remaining ms | FULL | POLICY_UNDEFINED | HIDDEN | no observation evidence | exact 300 s telemetry | OWN freeze required; ENEMY hidden |
| limited lifecycle enabled/applicability | FULL | POLICY_UNDEFINED | HIDDEN | no side disclosure rule | field shape can identify Loitering Munition | OWN freeze required; ENEMY hidden |
| limited lifecycle state | FULL | POLICY_UNDEFINED | HIDDEN | no observation evidence | do not expose active/expired applicability | OWN freeze required; ENEMY hidden |
| limited lifecycle remaining ms | FULL | POLICY_UNDEFINED | HIDDEN | no observation evidence | exact 1,200 s resource telemetry | OWN freeze required; ENEMY hidden |
| integrity status | FULL | HIDDEN by normal API | HIDDEN | debug-derived, not observation evidence | GREEN/debug only | HIDDEN outside GREEN |
| integrity codes | FULL | HIDDEN by normal API | HIDDEN | may reveal identity/relation/profile internals | GREEN/debug only | HIDDEN outside GREEN |
| semantic hash | FULL authoritative hash | perspective-safe hash or omitted | perspective-safe hash or omitted | no current side lifecycle hash | authoritative hash is a hidden-state side channel | separate hashes only |

## 7. Identity Policy

`IDENTITY_REFERENCE_POLICY` is the existing stable real operator reference:
the observed enemy record uses `operator_id` and Source `obj_id`. Projection
does not invent contact aliases or maintain identity across lost contact.

Exact observed equipment/model identity (including canonical identity and
source type/subtype) is **FULL** under the previously frozen project policy.
The input must still be the legal observed-contact record. The projector may
resolve a canonical name from the observed type/subtype using the versioned
resolver, but must never consult a hidden World operator when observation
identity is absent or unknown.

The current JSON safe-list and top-level Source record still mask exact type,
while `SideObservation` and `extra.observation` carry it. This is documentation
and policy-enforcement drift, not a reason to revoke the user-frozen identity
rule. It must be synchronized in the repair/policy-freeze round.

## 8. Health Policy

`alive` and the derived `destroyed = not alive` are FULL for an observed
contact. Exact `blood/max_blood` is HIDDEN by the current Source projection.

Exact `health/max_health` is unresolved and currently unsafe to treat as
frozen. It is present in `OperatorObservation` and Source `extra.observation`,
but excluded from `visible_enemy_safe_fields`, and no rule document authorizes
exact enemy HP telemetry. This is
`P1 EXISTING_ENEMY_EXACT_HEALTH_POLICY_DRIFT`.

The recommended default is HIDDEN exact health. If the user wants observable
damage, freeze a separate evidence-backed coarse band. A coarse band must not
be computed from hidden exact health unless the policy itself explicitly grants
that derived disclosure.

## 9. Movement Policy

Exact current position and altitude are FULL only while a current contact
exists. Lost contact produces no lifecycle record; projection does not continue
to query WorldState or maintain last-known state.

Enemy execution intent is HIDDEN: movement mode, segment/progress, remaining
task, task length, pause pending/reason, and exact transition timers never cross
the boundary. The current safe-list permits legacy `state`, which can support a
coarse externally observable category, but the mapping must be frozen before
implementation:

```text
MOVING-like observed evidence       -> MOVING
STOPPED-like observed evidence      -> STATIONARY
TRANSITIONING observed evidence     -> TRANSITIONING
other/unknown legacy values         -> UNKNOWN
```

It must never map an enemy to typed `PAUSED_BY_SUPPRESSION`, expose a remaining
task, or infer future motion. `state` is observation evidence here, not the
authoritative lifecycle axis.

## 10. Suppression Policy

The typed `suppression_remaining_ms` remains internal authority. Exact enemy
remaining time is HIDDEN. A public `state=SUPPRESSED` currently supplies coarse
event/contact evidence, but policy has not explicitly said that the lifecycle
projection should expose `suppression.active`. This choice requires freeze.

Even if coarse active suppression is allowed, it does not authorize:

- exact remaining milliseconds;
- suppression movement pause reason;
- remaining movement task;
- continued telemetry after loss of contact; or
- reading stale raw `suppression_remaining_seconds`.

A publicly delivered suppression event would be historical evidence, not
continuous timer authority.

## 11. Concealment Policy

Enemy concealment posture is HIDDEN. The concealment audit already establishes
that concealment changes detectability but that detection does not change or
disclose posture (`concealment_targetability_audit_v1.md:321-324`). Enemy raw
attributes are removed. Therefore `contact exists` must not be transformed into
`posture=EXPOSED`, and a detected concealed target must not receive a public
`CONCEALED` marker without a new explicit policy.

## 12. Boarding Policy

Enemy boarding role, state, transition, remaining time, passenger/carrier ID,
and passenger type are HIDDEN. The existing relation policy lists passenger,
carrier, launcher, target, owner, and observation IDs for cleaning, and tests
confirm that a visible carrier does not reveal a hidden passenger.

If an unloaded passenger becomes independently visible, it is a new current
contact. That does not authorize retrospective or continuous access to the
carrier's hidden passenger relation.

## 13. Weapon, Guided, and Indirect Policy

Enemy weapon posture, weapon cooldown, Guided recovery, and Indirect planning
cooldown are HIDDEN. A visible FIRE fact, if lawfully delivered, can say that a
fire event happened; it does not expose continuous cooldown/recovery telemetry.
The Source enemy record currently sets weapon cooldown/unfold/guidance fields to
`None`, which is direct projection evidence for hiding them.

OWN exact weapon cooldown is already represented under the own-unit Source-full
policy. OWN Guided recovery and Indirect planning cooldown are native typed
state not present in the formal side observation schema and require an explicit
policy freeze. Recommended default: expose exact own operational lifecycle
timers, while keeping Engine capability and action legality separate.

Indirect Mission FLYING/ACTIVE/RESOLVED state remains outside Operator
Lifecycle Perspective and belongs to a future Mission perspective contract.

## 14. Limited Lifecycle Policy

For an observed enemy, limited-lifecycle applicability, enabled/state, profile,
and remaining milliseconds are all HIDDEN. Omitting only the remaining value is
insufficient: returning `NOT_APPLICABLE` for Drone but `HIDDEN` for a Loitering
Munition leaks type/capability through field shape.

The observed-enemy schema should therefore omit the entire limited-lifecycle
axis (or encode one uniform HIDDEN envelope) for every enemy contact. It must
not return `-1`, infinity, a very large value, or authoritative applicability.

OWN limited lifecycle state/remaining time requires user freeze. Recommended
default: FULL for own controllable Operators because it is decision-relevant
resource time, without treating it as action legality.

## 15. Integrity and Hash Policy

Authoritative integrity status/codes are GREEN/debug-only. Codes such as
unknown canonical identity, non-reciprocal boarding, or missing profiles reveal
hidden structure and are not tactical observation evidence. Ordinary OWN and
OBSERVED_ENEMY payloads omit them; an explicitly authorized own-debug API can
be designed separately.

The authoritative lifecycle `semantic_hash` must never be returned to RED or
BLUE. Hidden timer changes would change that hash even when every visible field
is unchanged, creating a direct side channel.

Future hashes are distinct:

- GREEN may use the authoritative lifecycle hash;
- OWN may use a hash of only the OWN-safe payload, or omit it;
- OBSERVED_ENEMY may use a hash of only the enemy-safe payload, or omit it.

`HASH_SIDE_CHANNEL: PASS` for the proposed contract, but there is no current
perspective lifecycle hash implementation to validate.

## 16. HIDDEN, UNKNOWN, MISSING, NOT_OBSERVED, NOT_APPLICABLE

- **HIDDEN**: policy denies this Perspective the value and its applicability.
- **UNKNOWN**: legal observation evidence exists, but cannot be classified by
  the versioned mapping.
- **MISSING**: policy says evidence should be present for the projected field,
  but the supplied observation evidence is absent or malformed.
- **NOT_OBSERVED**: no current legal contact evidence exists; no current enemy
  lifecycle payload is emitted. It does not mean destroyed or off-map.
- **NOT_APPLICABLE**: the semantic truly does not apply and this Perspective is
  authorized to know that fact.

Enemy hidden applicability must never be encoded as `NOT_APPLICABLE`. A plain
`null` is also insufficient unless its enclosing schema unambiguously assigns
one of these meanings.

## 17. Hidden-state Side Channels

The following channels must be prohibited:

1. forwarding the authoritative lifecycle hash to a side;
2. preserving different field shapes for hidden applicability;
3. changing enemy output order or presence due only to a hidden timer;
4. deriving a coarse field from hidden exact state without disclosure authority;
5. continuing to update a lost contact by reading WorldState;
6. leaking relationship/profile details through integrity codes;
7. exposing full View equality, debug diffs, or exception text to a side; and
8. default-exporting a newly added authoritative lifecycle field.

Enemy projection must be an explicit allow-list. An unconfigured new lifecycle
field is HIDDEN. OWN also requires explicit policy; GREEN alone defaults to
complete internal projection.

## 18. Noninterference Contract

For perspective `P`, observation evidence `O`, policy identity `K`, and
projection function `L`:

```text
O(WorldA, P) == O(WorldB, P)
K(WorldA) == K(WorldB)
differences(WorldA, WorldB) are policy-hidden lifecycle fields

implies

L(WorldA, O, P, K) == L(WorldB, O, P, K)
perspective_semantic_hash_A == perspective_semantic_hash_B
```

The function must consume the frozen observation evidence supplied for the
decision/frame. It may not rerun visibility or inspect hidden World fields to
fill an enemy payload. If the same evidence cannot support a field in both
Worlds, that field is HIDDEN/absent rather than guessed.

`PERSPECTIVE_NONINTERFERENCE_TEST_READINESS: PARTIAL`: the mechanism and hidden
timer/relation cases are ready, but health and coarse legacy-state policies
must be frozen before their equality assertions are final.

## 19. Hidden-field Test Matrix

Each test creates two authoritative Worlds with identical legal side
observation evidence, changes only the named hidden state, and asserts identical
enemy payload and perspective-safe hash.

| Test | Hidden difference | Required result |
|---|---|---|
| E1 | exact health/max health (after HIDDEN policy is frozen) | identical output/hash |
| E2 | suppression remaining ms | identical output/hash; coarse observed state unchanged |
| E3 | weapon posture/cooldown | identical output/hash |
| E4 | Guided recovery | identical output/hash |
| E5 | Indirect planning cooldown | identical output/hash |
| E6 | limited-lifecycle applicability/remaining | identical output/hash and shape |
| E7 | movement task/path/progress/pause reason | identical output/hash |
| E8 | carrier/passenger relation and boarding timer | identical output/hash |

Additional required cases: authoritative integrity/hash differences do not
change side output; raw suppression/concealment metadata does not change it;
new unknown authoritative fields default hidden.

## 20. Visible-field Test Matrix

| Test | Legal evidence change | Required result |
|---|---|---|
| V1 | contact enters visibility | one observed-enemy payload appears |
| V2 | contact leaves visibility | current payload disappears; no World-backed updates |
| V3 | stable observed ID/side changes only by a different legal contact | reference changes deterministically |
| V4 | observed position/hex changes | position changes |
| V5 | observed altitude changes | altitude changes |
| V6 | observed alive becomes false while evidence remains available | alive/destroyed changes |
| V7 | allowed exact identity changes | identity/domain changes from evidence |
| V8 | frozen coarse contact-state category changes | only the coarse field changes |

Side swap must be RED/BLUE symmetric. Repeated projection of the same evidence
must be byte-semantically equal after canonical serialization.

## 21. Schema Recommendation

Two designs were considered:

### A. One schema with visibility states

This is superficially convenient, but stable field presence can reveal
applicability, every consumer must correctly interpret HIDDEN versus
NOT_APPLICABLE, and a new internal field risks automatic exposure.

### B. Separate GREEN, OWN, and OBSERVED_ENEMY payload schemas

This gives compile-time/type-review pressure, lets the enemy schema be a small
positive allow-list, avoids shape side channels, and permits policy versions to
evolve independently. The extra types are justified at a security boundary.

**Recommendation: B**, implemented as a versioned discriminated envelope with
separate payload types:

```text
GreenOperatorLifecyclePayload          -> full authoritative-derived semantics
OwnOperatorLifecyclePayload            -> explicitly frozen own-safe semantics
ObservedEnemyOperatorLifecyclePayload  -> observation-evidence allow-list only
```

GREEN can wrap/reference the authoritative value contract internally. RED/BLUE
must never receive that object. An unobserved enemy produces no enemy payload;
an optional query API may return a separate `NOT_OBSERVED` result.

## 22. Version and Policy Hash Contract

Recommended independent identities:

```text
schema_version = native-operator-lifecycle-perspective/1.0
policy_version = native-operator-lifecycle-perspective-policy/1.0
policy_hash    = SHA-256(canonical policy document)
```

The policy document must canonically include field allow-lists, coarse mapping
tables, hidden/default behavior, Perspective classes, schema/policy versions,
and hash rules. Canonicalization uses sorted object keys, stable enum strings,
ordered arrays, UTF-8 JSON, no timestamps, and no renderer text.

The per-operator perspective semantic hash, if implemented, covers only the
perspective-safe payload plus its schema/policy identity. It excludes the
authoritative hash, hidden values, World/RNG hashes, wall clock, recorder
metadata, and GREEN integrity. Policy identity may be recorded in Replay and
Benchmark metadata for verification, but never participates in adjudication or
World restoration.

## 23. Existing Event Evidence

Current side Source frames expose own accepted actions only. `judge_info` is
retained only when every referenced operator is friendly or currently visible,
and Native/table evidence is stripped (`observations/perspective.py:321-358`).
This can be legitimate historical fact evidence, but is not continuous state
telemetry.

Consequently:

- `SUPPRESSION_APPLIED` evidence does not grant exact remaining time;
- observed FIRE evidence does not grant weapon/Guided cooldown telemetry;
- an observed unloaded passenger does not reveal prior carrier relations; and
- a destroyed fact can support `alive/destroyed`, not hidden blood/health
  history unless separately authorized.

Lifecycle perspective v1 should consume current `SideObservation` evidence.
Event-derived memory belongs to a later explicit observation-memory contract.

## 24. Required User Policy Freeze

### Enemy exact health

- **Field:** `health/max_health` and any derived damage band.
- **Options:** HIDDEN exact/no band; COARSE evidence-backed band; FULL exact.
- **Current evidence:** runtime exposes exact values; configured safe-list and
  rule docs do not authorize them.
- **Recommended default:** HIDDEN exact, no coarse band until its evidence and
  thresholds are explicitly frozen.
- **Risk:** exact tactical damage telemetry and current observation-hash leak.

### Enemy heading and deployed

- **Field:** exact heading and deployed flag.
- **Options:** FULL; COARSE; HIDDEN.
- **Current evidence:** present in rich observation, absent from safe-list.
- **Recommended default:** HIDDEN; position/alive already provide the currently
  authorized contact facts.
- **Risk:** intent and boarding/deployment inference.

### Enemy coarse movement/transition state

- **Field:** observed MOVING/STATIONARY/TRANSITIONING category.
- **Options:** pass legacy state; versioned coarse mapping; HIDDEN.
- **Current evidence:** legacy `state` is configured safe, but is a mixed
  compatibility field and not the typed lifecycle authority.
- **Recommended default:** versioned coarse mapping with UNKNOWN fail-closed;
  never expose PAUSED, task state, pause reason, or timers.
- **Risk:** copying legacy state can expose internal suppression/transition
  semantics or drift when state strings change.

### Enemy coarse suppression state

- **Field:** `suppression.active` only.
- **Options:** COARSE from explicitly allowed observed state/event; HIDDEN.
- **Current evidence:** `state=SUPPRESSED` can be delivered; no typed timer is
  observation evidence.
- **Recommended default:** HIDDEN until `SUPPRESSED` is explicitly frozen as a
  public contact category; always hide remaining ms.
- **Risk:** continuous internal blocker disclosure.

### OWN native lifecycle details

- **Field:** exact suppression/transition/fatigue recovery, Guided recovery,
  Indirect planning cooldown, boarding time, and finite-lifecycle state/time.
- **Options:** FULL own operational state; selected fields only; hidden.
- **Current evidence:** own Source policy is broadly full, but the formal side
  observation does not carry all newer typed native fields.
- **Recommended default:** FULL own operational semantics and exact ms timers,
  excluding raw, integrity/debug, hidden relation IDs, capability, and action
  legality.
- **Risk:** without freeze, consumers either lack necessary self-state or start
  reading raw/Source compatibility as a second authority.

### OWN integrity

- **Field:** `integrity.status/codes`.
- **Options:** GREEN_ONLY; separate OWN_DEBUG_ONLY endpoint.
- **Current evidence:** integrity is diagnostic, not decision authority.
- **Recommended default:** GREEN_ONLY.
- **Risk:** internal profile/relation details and unstable agent inputs.

Exact observed identity is **not** a freeze item: prior user authority already
allows it. The Source policy/config and tests must be aligned during the repair
round so the policy is explicit rather than hidden in `extra.observation`.

## 25. Findings

### P0

NONE. The authoritative lifecycle View is not currently connected to side APIs;
no RED/BLUE consumer receives its object or authoritative semantic hash, and
projection does not mutate World state.

### P1

1. **EXISTING_ENEMY_EXACT_HEALTH_POLICY_DRIFT.** Exact enemy health/max health
   is transported in `SideObservation` and Source `extra.observation` despite
   exclusion from the configured visible-enemy safe-list and absence of rule
   authority. It changes the side observation hash. Human, Agent, training, and
   browser controller transports inherit the value.
2. **PARALLEL_SIDE_REPRESENTATION_POLICY_DRIFT.** One `ControllerView` exposes a
   masked Source enemy record and a richer observation record; the Source
   record itself also hides fields publicly then re-adds some in `extra`. Policy
   enforcement is therefore not a single auditable boundary. This must be
   reconciled before lifecycle perspective integration.

### P2

1. The exact observed identity policy is user-frozen but not reflected in the
   configured enemy safe-list/top-level Source contact.
2. Legacy `state` is safe-listed but mixes movement, suppression, transition,
   boarding, and other semantics; its coarse lifecycle mapping is not frozen.
3. OWN policy does not explicitly cover the newer typed native lifecycle
   timers even though the broad Source-own policy says full.
4. The side Source `extra.native` allow-list includes policy ID/version but not
   policy hash; future lifecycle policy metadata needs an explicit contract.
5. Existing tests protect raw/relation/NATIVE-evidence removal and no-GREEN
   transport, but do not assert exact per-field enemy safe-list conformance or
   hidden-state noninterference.

### P3

1. `operator_type`, Source type/subtype, canonical identity, and domain need
   distinct names in the future schema to avoid accidental equivalence.
2. `NOT_OBSERVED` is best represented at lookup/collection level, not as a
   populated enemy lifecycle object.

## 26. Readiness

```text
GREEN_PERSPECTIVE_POLICY_READINESS: READY
OWN_PERSPECTIVE_POLICY_READINESS: READY_AFTER_POLICY_FREEZE
OBSERVED_ENEMY_PERSPECTIVE_POLICY_READINESS: READY_AFTER_POLICY_FREEZE
PERSPECTIVE_NONINTERFERENCE_TEST_READINESS: PARTIAL
PERSPECTIVE_PROJECTION_IMPLEMENTATION_READINESS: READY_AFTER_P1_FIX
```

The P1 repair and policy freeze should be one narrow observation-boundary round:
make the policy document authoritative, align both side representations with
it, then add the perspective lifecycle projector. No Engine, Visibility, or
Operator rule change is required.

## 27. Recommended Module and Dependency Boundary

Recommended future module:

`src/wargame_native_engine/projection/operator_lifecycle_perspective.py`

Dependency direction:

```text
core / rules
  -> authoritative operator_lifecycle projection
  -> existing observation authority and detached evidence
  -> lifecycle perspective policy/projector
  -> future decision projection
  -> Human / Agent / training
```

The projector must not call `NativeEngine.observe`, rerun Visibility, enumerate
capabilities/targets/actions, or retain mutable World/Observation references.
Core/Engine must not import it.

## 28. Required Implementation Tests

1. GREEN output is semantically complete and equals recomputation from the
   authoritative lifecycle View.
2. OWN output follows only the frozen own allow-list and excludes raw,
   integrity/debug, adjudication, RNG, and relation secrets.
3. OBSERVED_ENEMY output is built only for IDs in the frozen observation.
4. Lost contact yields no current payload and does not continue updating.
5. Stable contact ID, side, exact allowed identity/domain, position, altitude,
   and alive are deterministic and RED/BLUE symmetric.
6. HIDDEN/UNKNOWN/MISSING/NOT_OBSERVED/NOT_APPLICABLE never collapse to an
   ambiguous null.
7. E1-E8 hidden-field noninterference tests pass.
8. V1-V8 visible-field sensitivity tests pass.
9. Authoritative semantic hash is absent from RED/BLUE.
10. Perspective-safe hash is unchanged by every hidden-field change.
11. Different field applicability cannot leak through enemy payload shape.
12. A newly added authoritative lifecycle field defaults HIDDEN for enemy and
    requires explicit OWN policy.
13. Projection is immutable, deterministic, side-effect free, and does not
    advance timers or use RNG/wall clock.
14. Snapshot/restore and strict replay recompute identical projection from the
    same observation evidence and policy hash.
15. Projector never calls LOS, pathfinding, target enumeration, capability
    enumeration, or Engine legality.
16. Human, classic Agent, realtime Agent, browser, and training receive the same
    policy-safe enemy lifecycle semantics.
17. Current raw/relation/NATIVE-evidence isolation regressions remain passing.
18. Exact-health repair test asserts both rich observation and Source extra
    comply with the frozen policy.

## 29. Existing Tests and Read-only Probe

Existing tests passed during this audit:

```text
116 passed
  engine, source perspectives/visibility, gateway, live control,
  realtime Agent session, training, Human capabilities, Agents

192 passed
  authoritative lifecycle View, boarding, concealment, Guided, Indirect,
  suppression movement, Loitering Munition, Drone, Replay/recorder

71 passed, 3 skipped
  Viewer renderer/replay, analysis, Boarding UI, Guided UI
  skips: unavailable read-only five-sample fixture (2), Node.js absent (1)
```

A `/tmp`/stdin-only probe created a legal RED contact, then changed only BLUE
authoritative fields:

- the observed enemy record contained exact type/subtype, position, heading,
  health/max health, state, alive, and deployed, with empty raw attributes;
- Source public fields masked type/subtype/blood but `extra.observation` carried
  exact health/type/subtype;
- changing hidden Guided recovery from 0 to 50,000 ms changed the World hash but
  left RED observation and its hash identical;
- changing exact enemy health from 100 to 67 changed RED observation and its
  hash; and
- changing enemy raw suppression seconds did not change RED observation.

This confirms both the useful basis for timer noninterference and the current
exact-health exposure.

## 30. Final Audit Statement

The internal authoritative lifecycle View remains ready and correctly isolated.
GREEN policy is closed. OWN and observed-enemy policies require the explicit
freeze items above, and the existing exact-health/parallel-schema P1 must be
repaired before implementation.

```text
OPERATOR_LIFECYCLE_PERSPECTIVE_POLICY_REQUIRES_FREEZE
PERSPECTIVE_PROJECTION_REQUIRES_POLICY_FREEZE
PERSPECTIVE_PROJECTION_REQUIRES_REPAIR
```

## Policy Freeze

This section is the authoritative policy resolution that supersedes the open
items recorded by the read-only audit above. It does not introduce an
Operator Lifecycle Perspective payload or projector.

| Semantic | GREEN | OWN | OBSERVED_ENEMY |
|---|---|---|---|
| legally observed stable ID, side, exact identity/type/subtype/domain | FULL | FULL | FULL, from detached observation evidence only |
| exact current position and altitude | FULL | FULL | FULL while currently observed |
| alive/destroyed | FULL | FULL | FULL |
| exact health/max health/blood/max blood | FULL | FULL | HIDDEN |
| heading and deployed | FULL | FULL | HIDDEN |
| movement | FULL | FULL | COARSE: `MOVING`, `STATIONARY`, or fail-closed `UNKNOWN` |
| movement mode, segment, task, path length, pause/block/transition detail | FULL | FULL | HIDDEN |
| suppression and suppression timer/reason | FULL | FULL | HIDDEN |
| fatigue and recovery | FULL | FULL | HIDDEN |
| concealment posture | FULL | FULL | HIDDEN |
| boarding state/relation/timer | FULL | FULL | HIDDEN |
| weapon posture/cooldown | FULL | FULL | HIDDEN |
| Guided recovery | FULL | FULL | HIDDEN |
| Indirect planning cooldown | FULL | FULL | HIDDEN |
| limited-lifecycle applicability/state/time | FULL | FULL | HIDDEN |
| lifecycle integrity status/codes | FULL | HIDDEN | HIDDEN |
| authoritative LifecycleView semantic hash | internal GREEN diagnostic | HIDDEN | HIDDEN |

`HIDDEN` is omission at the normal side boundary, not `None` and not
`NOT_APPLICABLE`. Current-contact absence is `NOT_OBSERVED` and produces no
enemy payload. Unknown observed identity or coarse motion maps to `UNKNOWN`;
missing required evidence is omitted/fail-closed and is never backfilled from
GREEN or `WorldState`.

The canonical policy is
`config/source_compat/source_perspective_policy_v1.json`, version `1.1.0`,
schema `source-perspective-policy/1.1`. Its canonical SHA-256 policy hash is
`fe797540222c8d8749670eac8b52283a0dfdd2ef685c531df64977f2f69c0d28`.
Unknown future fields are hidden from enemy output unless they are explicitly
added to the positive allow-list.

## Repair Round Verification

### EXISTING_ENEMY_EXACT_HEALTH_POLICY_DRIFT

**Before.** `SideObservation.visible_enemies` reused the rich friendly
`OperatorObservation`, so exact enemy `health`, `max_health`, `heading`, and
`deployed` crossed the side boundary. Source public fields masked health but
reintroduced it through `extra.observation`. Human, Agent, training, and normal
browser control therefore inherited different versions of the leak.

**After.** `ObservedEnemyOperatorObservation` is an immutable positive
allow-list contact object. It contains no exact health/blood, heading,
deployment, raw compatibility, timer, relationship, path/task, integrity, or
authoritative hash field. `SideObservation.to_dict()` sanitizes even a legacy
rich object supplied as an enemy. Source `extra.observation` is rebuilt only
from that same safe contact and cannot re-add exact health. OWN exact health is
unchanged; GREEN remains complete.

Result: **CLOSED**.

### PARALLEL_SIDE_REPRESENTATION_POLICY_DRIFT

**Before.** Source public records, rich SideObservation,
`extra.observation`, and Controller consumers maintained overlapping but
different enemy masks. Removing a field in one representation did not prevent
another representation from exposing it.

**After.** `rules/perspective.py` loads and validates the single canonical
field policy. `NativeEngine.observe()` creates the detached safe enemy contact;
`SideObservation.to_dict()` enforces the same boundary; Source-compatible
records and Controller/Human/Agent/training/Viewer representations consume the
safe object rather than reconstructing permissions. Friendly observation uses
typed `observed_enemy_ids` and typed lifecycle fields rather than exposing the
raw compatibility dictionary. Source contact classification no longer reads
GREEN boarding relations. Identity is mapped only from type/subtype evidence
already present in the contact; there is no GREEN identity backfill.

Result: **CLOSED**.

### Coarse movement

Version 1.1 maps only externally supplied contact state:

- `MOVING` -> `MOVING`;
- `STOPPED` -> `STATIONARY`;
- every other or unrecognised value -> `UNKNOWN`.

The mapping never reads a future path, active segment, task length, pause
reason, suppression, transition subtype, or other hidden lifecycle state.

### Noninterference and sensitivity

Formal contract: if two worlds produce identical legal observation evidence
for a side and differ only in fields hidden by policy 1.1, then every normal
side representation derived from that evidence is identical, including its
serialized observation hash and extras.

Regression coverage verifies hidden differences in exact health,
suppression, weapon cooldown, Guided recovery, Indirect cooldown, limited
lifecycle/applicability, movement task/path/pause, and boarding relationship
(E1-E8). It also verifies sensitivity to contact appearance/disappearance,
observed identity/side, position, altitude, alive/destroyed, and coarse motion
(V1-V8), plus RED/BLUE symmetry, lost-contact removal, future-field
fail-closure, OWN timer preservation, and GREEN completeness.

No authoritative LifecycleView hash is present in RED/BLUE. The side
observation hash is computed from the safe payload, so hidden-field changes do
not create a hash side channel.

### Replay and compatibility

The repair adds no World/Operator state and does not change adjudication,
ordered actions, RNG, or World hashes. Replay `steps_sha256` remains
`977a3510e9371c77916e923f42dc0003984aba0c77277942abd4be187f245dd4`
and the verified final state hash remains
`f64334d960e7c0ab0e9d5ee780315a941a9bc2ad2071e8e2fb4816bdefcbf21d`.
The deterministic archive content hash intentionally changes because the
manifest now records the versioned side policy hash; this is an explicit
observation-contract version change, not a World semantic change. Strict
re-execution continues to pass.

The side observation boundary and the two P1 findings are closed. The next
phase may implement separate GREEN/OWN/OBSERVED_ENEMY lifecycle payloads on
top of this policy; no such payload is implemented in this round.

```text
PERSPECTIVE_POLICY_V1_FROZEN
SIDE_OBSERVATION_POLICY_BOUNDARY_CLOSED
PERSPECTIVE_NONINTERFERENCE_CONTRACT_READY
PERSPECTIVE_PROJECTION_IMPLEMENTATION_READY
```

## Perspective Projection Implementation Verification

Phase 2 implements the frozen policy as a projection-only library. It does not
change `SideObservation`, Engine legality, Visibility, Human/Agent/training
contracts, or any authoritative World/Operator schema.

### Module and dependency boundary

Implementation module:

`src/wargame_native_engine/projection/operator_perspective.py`

Schema:

`native-operator-lifecycle-perspective/1.0`

Dependency direction remains:

```text
core/rules
  -> AuthoritativeOperatorLifecycleView
  -> detached observation evidence + canonical perspective policy
  -> perspective lifecycle projection
```

Neither `NativeEngine` nor adjudication imports the perspective projector. The
projector imports no Engine, WorldState, OperatorState, RNG, visibility,
pathfinding, capability, action-generation, replay-I/O, or wall-clock API.

### Envelope and payload schemas

Every result is an immutable `OperatorLifecyclePerspectiveEnvelope` carrying:

- perspective schema version;
- canonical policy ID/version/hash;
- discriminated perspective kind;
- exactly one immutable payload type; and
- SHA-256 `semantic_hash` over only the perspective-safe envelope content.

The payload types are structurally separate:

1. `GreenOperatorLifecyclePayload` includes every authoritative lifecycle axis,
   integrity, authoritative lifecycle schema, and authoritative semantic hash.
2. `OwnOperatorLifecyclePayload` includes every operational lifecycle axis and
   timer, but its Python schema has no integrity or authoritative hash field.
3. `ObservedEnemyOperatorLifecyclePayload` contains only operator reference,
   side, observed identity, observed position/altitude, alive/destroyed, and
   the independent coarse motion enum `MOVING/STATIONARY/UNKNOWN`.

The enemy schema has no health, heading, deployment, movement task, transition,
fatigue, suppression, concealment, boarding, weapon, Guided, Indirect,
limited-lifecycle, applicability, integrity, or authoritative hash field. This
is structural omission, not `None`, `HIDDEN`, or `NOT_APPLICABLE`.

### Observation-first enemy source chain

`project_observed_enemy_operator_lifecycle()` accepts only
`ObservedEnemyOperatorObservation`. It has no World argument and rejects an
authoritative lifecycle View or arbitrary rich object. Identity and coarse
motion are consumed from the already policy-normalized safe evidence. Missing
or unmapped canonical identity remains `UNKNOWN`; no GREEN/World lookup exists.

Batch side projection consumes an existing `SideObservation` plus explicitly
supplied OWN authoritative lifecycle values. It never rebuilds contact or runs
Visibility. Lost contacts therefore create no enemy payload. Results are sorted
by stable operator reference and perspective kind.

### Policy metadata and semantic hash

The projector reuses `rules/perspective.py` and the canonical policy hash:

`fe797540222c8d8749670eac8b52283a0dfdd2ef685c531df64977f2f69c0d28`

It does not define a second allow-list or hash algorithm. Envelope semantic hash
uses canonical JSON and SHA-256 over schema version, policy identity, policy
hash, perspective kind, and safe payload. The authoritative lifecycle hash is
present only in GREEN. OWN and enemy hashes are independent namespaces and do
not change when their hidden inputs change.

### Noninterference and shape safety

Formal E1-E8 tests reconstruct enemy evidence after changing only exact health,
suppression, weapon cooldown, Guided recovery, Indirect cooldown, finite
lifecycle/applicability, movement task/path/pause, or boarding relation. In all
cases both enemy payload and perspective semantic hash remain identical.

Drone and Loitering Munition contacts use the same fixed enemy payload shape.
Their legally public identities may differ, but lifecycle applicability cannot
leak through field presence. An architecture test passes an authoritative View
to the enemy API and verifies fail-closed rejection; future authoritative fields
therefore cannot enter enemy output through generic dataclass serialization.

### Snapshot, replay, purity, and performance

Snapshot/restore recomputes identical GREEN and OWN envelopes. Strict replay
re-executes the scenario/actions/exact delta and recomputes the same perspective
payload and hash; no payload is restored as World authority. Existing replay
manifest policy metadata remains sufficient, so Phase 2 introduces no archive
or World-hash change.

Projection is side-effect free: construction leaves World and detached
observation hashes unchanged and advances no timer. Single projection is O(1);
GREEN and side batches are O(number of supplied lifecycle values + current
safe contacts). No World scan, LOS, pathfinding, target/capability enumeration,
or mutable cache is used.

### Verification

Formal implementation tests cover P1-P22, E1-E8, immutable payloads, full
GREEN/OWN semantics, enemy schema shape, unknown identity/motion,
no-GREEN-backfill, lost contact, RED/BLUE symmetry, stable ordering, purity,
deterministic namespaced hashes, snapshot/restore, strict replay, shape
noninterference, future-field fail-closure, and canonical policy reuse.

```text
OPERATOR_LIFECYCLE_PERSPECTIVE_V1_IMPLEMENTED
PERSPECTIVE_PROJECTION_CONTRACT_CLOSED
PERSPECTIVE_NONINTERFERENCE_VERIFIED
DECISION_SEMANTIC_PROJECTION_READY
```
