# EightFactor Reward Shaping Contract Audit v1

## Executive Summary

This is a read-only contract audit. It does not implement reward shaping,
change the frozen base reward, or modify Engine, Decision, encoder, catalog,
policy-observation, replay-authority, Human, or Agent behavior. The only
repository change made by this audit is this document.

The current `EightFactorAnalyzer` is a read-only, deterministic projector over
one detached GREEN frame. Its unit score is:

```text
E_i = V_i * H_i * W_i * A_i * R_i * M_i * I_i * S_i
```

Valid unit powers are summed per side. Its existing RED-oriented relative
advantage is:

```text
Phi_RED(s) = 0                                      if E_RED + E_BLUE <= 0
             (E_RED - E_BLUE) / (E_RED + E_BLUE)  otherwise
```

and is unavailable if either side aggregate is unavailable. Because factor
values and effective powers are non-negative, this potential is finite and
bounded in `[-1, 1]`. It is deterministic, depends only on the current
committed GREEN frame, and has safe explicit behavior for one-side-zero and
both-sides-zero states. It is the recommended potential. BLUE must be defined
canonically as `Phi_BLUE = -Phi_RED`, not independently recomputed.

The recommended shaping component is potential based:

```text
F_RED(s_t, s_t+1) = lambda * (
    gamma * Phi_RED(s_t+1) - Phi_RED(s_t)
)
F_BLUE = -F_RED
```

It must be an optional `shaping:EIGHT_FACTOR_POTENTIAL` component composed with,
not inserted into or substituted for, the frozen
`base:TERMINAL_OUTCOME` component. With shaping disabled, every base record and
hash must remain exactly as it is under
`native-sparse-terminal-zero-sum/1.0.0`.

The shaping contract is not yet implementation-ready for two reasons. First,
the repository contains no PPO/learner discount authority, so a numeric gamma
and its step/time basis cannot be aligned with the learner. Second, analysis
records have schema `native-eight-factor-frame-analysis/1.0` and model
provenance fields, but the default analyzer uses `model_hash=None`; there is no
canonical hash that binds the factor extraction, formulas, weapon/mobility
tables, aggregation, and advantage formula. Both gaps must close before a
shaping policy can claim replay-stable semantics.

Lambda is intentionally not frozen by this audit. It is a research
hyperparameter and must be selected using calibration data only, before the
disjoint validation partition is evaluated. A small sweep including zero is
recommended. This is a required policy freeze, not evidence that the current
base reward is incomplete.

```text
EIGHT_FACTOR_POTENTIAL_CONTRACT_READINESS: READY_AFTER_POLICY_FREEZE
ANALYZER_VERSIONING_READINESS: READY_AFTER_FIX
EIGHT_FACTOR_SHAPING_IMPLEMENTATION_READINESS: READY_AFTER_FIX
AUXILIARY_LABEL_IMPLEMENTATION_READINESS: READY_AFTER_FIX
```

## Scope and Non-goals

This audit freezes or recommends:

- the current Analyzer authority and formulas;
- the potential source and side symmetry;
- the potential-based shaping equation and its zero-sum projection;
- gamma, lambda, terminal, truncation, missing-data, and coverage boundaries;
- GREEN privileged-supervision isolation;
- transition/frame alignment, replay, snapshot, parallelism, and performance;
- a non-implemented shaping-policy and composite-reward schema;
- implementation gates and required tests.

It does not:

- modify `native-sparse-terminal-zero-sum/1.0.0`;
- implement a shaping policy, calculator, component, or training integration;
- introduce dense damage, objective, time, WAIT, or action-cost reward;
- modify `TrainingRewardRecord` or `JointTrainingRewardRecord`;
- change Engine adjudication, terminal authority, World, replay authority, RNG,
  Decision, Perspective, lifecycle, capability, catalog, or encoder semantics;
- implement learner normalization, clipping, PPO, MARL, central critic, an
  auxiliary target, an offline dataset, or an LLM renderer.

All formulas are historical-replay-compatible Native analysis candidates, not
official rules or SDK-equivalent behavior.

## Analyzer Authority

The actual authority chain is:

```text
detached current GREEN frame
  -> explicit eight-factor evidence extraction
  -> demo-compatible factor functions
  -> left-associative unit effective-power product
  -> stable per-side aggregation
  -> GREEN relative advantage
  -> immutable FrameAnalysis / AnalysisTimeline
  -> evaluation sidecar today
  -> optional training supervision in a future version
```

`EightFactorAnalyzer` is a frozen, stateless orchestrator. It receives
`GreenFrameAnalysisInput`, calls the existing single-frame builder, and retains
no World, Engine, RNG, history, or caller frame. It is non-adjudicative: none of
its results feeds hit resolution, action validation, termination, or winner
selection. Input order is authoritative for timelines, while units and sides
are projected in stable order.

The Analyzer must remain on the supervision path:

```text
World / detached GREEN endpoint frames
  -> EightFactorAnalyzer
  -> shaping projector
  -> RewardRecord component
  -> learner
```

It must never move onto the policy path or become an Engine authority.

## Analyzer Outputs

The factor ranges below describe the current code. `V` and therefore `E_i`
and side power have no finite configured upper bound. Approximate product
ranges are stated only where the code's individual multipliers establish one;
they are not calibrated operational ranges.

| Field | Current formula/source | Mathematical range | Unit | Zero allowed | Missing behavior | RED/BLUE symmetry | Deterministic | Candidate for Phi |
|---|---|---:|---|---|---|---|---|---|
| `V` base value | `max(1, float(value))` | `[1, +inf)` | source value weight | No | missing/`None` -> unknown | Same function | Yes | No; unbounded component |
| `H` health | destroyed -> 0; else `0.25 + 0.75 * clamp(blood/max_blood)`; special nonpositive max handling | `[0, 1]` | ratio | Yes | missing/`None` -> unknown | Same function | Yes | No |
| `W` weapon | max configured carried-weapon weight; empty `0.6`; unknown id `1.0` | currently `[0.6, 2.7]` | multiplier | No | absent field makes evidence unknown even though function has fallback | Same function | Yes | No |
| `A` ammunition | non-map `0.7`; empty/zero `0.3`; else `min(1.25, 0.55 + log1p(total)/5)` | `[0.3, 1.25]` | multiplier | No | absent field makes evidence unknown | Same function | Yes | No |
| `R` readiness | cooldown discount with `0.55` floor; unfold `0 -> *0.9`, `1 -> *1.05` | approximately `[0.495, 1.05]` | multiplier | No | either source field missing -> unknown | Same function | Yes | No |
| `M` mobility | move-state table plus stop `*0.95`, cannot-move `*0.85`, on-board `*0.9` | approximately `[0.654, 1.15]` | multiplier | No | any required source field missing -> unknown | Same function | Yes | No |
| `I` information | `min(1.25, 1 + 0.05 * visible_enemy_count)` | `[1, 1.25]` | multiplier | No | absent source field -> unknown | Same function over GREEN records | Yes | No |
| `S` status | suppression, fatigue, control, close-combat multiplicative modifiers | approximately `[0.0975, 1.05]` | multiplier | No | any required source field missing -> unknown | Same function | Yes | No |
| Unit effective power `E_i` | `V*H*W*A*R*M*I*S`, left associative in fixed factor order | `[0, +inf)` when valid | effective-force weight | Yes | any unknown factor -> `None` | Same formula | Yes | No; unbounded |
| Side effective force | sum of valid `E_i` in operator-id order | `[0, +inf)` or `None` | effective-force weight | Yes | nonempty side with no valid units -> `None`; partial side sums valid units and warns | Same aggregation | Yes | Input to Phi |
| Coverage | valid-unit count / total-unit count | `[0,1]`, or `None` for empty side | ratio | Yes | empty side -> `None` | Same formula | Yes | Gate only, not multiplier |
| Confidence | not implemented | `None` | none | N/A | always missing | Symmetric absence | Yes | No in v1 |
| Raw advantage | `E_RED - E_BLUE` | `(-inf,+inf)` | effective-force weight | Yes | either side unavailable -> `None` | Antisymmetric | Yes | Rejected: unbounded |
| Normalized advantage | `(E_RED-E_BLUE)/(E_RED+E_BLUE)`; total `<=0 -> 0` | `[-1,1]` | dimensionless | Yes | either side unavailable -> `None` | Antisymmetric | Yes | **Recommended** |
| Timeline delta | adjacent normalized value difference | `[-2,2]`, or `None` | dimensionless delta | Yes | either endpoint missing -> `None` | Antisymmetric | Yes | Not authoritative for shaping; recompute from aligned endpoints with gamma |

All numeric analysis stages reject non-finite values. A valid destroyed unit may
have `E_i=0`; zero must not be confused with missing analysis.

## Current Relative Advantage

The implementation uses formula id
`demo-compatible.green-relative-advantage.v1`:

```text
raw = E_RED - E_BLUE
scale = E_RED + E_BLUE

if either side power is unavailable:
    normalized = MISSING
elif scale <= 0:
    normalized = 0.0
else:
    normalized = raw / scale
```

Given current non-negative factor formulas, normalized advantage is bounded in
`[-1, 1]`. Swapping RED and BLUE negates it mathematically. For the reward
contract, only canonical RED potential is calculated and BLUE is assigned its
negation, which makes zero-sum identity structural and avoids independent
floating-point paths.

The value uses only one supplied frame. It does not use winner, future frames,
maximum future advantage, trajectory length, or timeline statistics.

It is recommended as Phi, subject to complete-analysis gating and a versioned
Analyzer identity.

## Candidate Potentials

| Candidate | Bounded | Antisymmetric | Zero handling | Scale | Replay determinism | Principal risk | Verdict |
|---|---|---|---|---|---|---|---|
| A. `E_RED-E_BLUE` | No | Yes | Safe at zero | Depends on unit values/count | Yes | Can dwarf terminal `+/-1`; scenario-size and roster-sensitive | Reject |
| B. `(E_RED-E_BLUE)/(E_RED+E_BLUE+epsilon)` | Approximately | Yes if one shared epsilon | Avoids division by zero | Sensitive to epsilon near zero | Yes only if epsilon is versioned | Invents a second formula/epsilon and differs from Analyzer | Reject in favor of existing formula |
| C. `log((E_RED+epsilon)/(E_BLUE+epsilon))` | No | Yes if epsilon is symmetric | Requires epsilon | Large near zero | Yes only if epsilon is versioned | Unbounded, highly sensitive to tiny remaining force | Reject |
| D. `tanh(k * log ratio)` | Yes | Yes with shared epsilon/k | Requires epsilon | Saturating; depends on `k` | Yes only if both constants are versioned | Adds two unsupported transforms and can erase magnitude distinctions | Do not use in v1 |
| E. Existing normalized advantage | Yes, `[-1,1]` | Yes | Explicit `(0,0)->0`, one-zero -> `+/-1` | Dimensionless | Yes | Partial-coverage semantics require a gate | **Recommend** |

No epsilon is required by candidate E. If a future policy selects B, C, or D,
epsilon and `k` must be first-class policy fields and hash inputs; they may not
appear as hidden constants.

## Recommended Potential

```text
potential_source = ANALYZER_NORMALIZED_RELATIVE_ADVANTAGE
potential_transform = IDENTITY

Phi_RED(s) = FrameAnalysis.advantage.normalized_advantage
Phi_BLUE(s) = -Phi_RED(s)
```

Validity requirements for v1:

- the analysis corresponds exactly to the committed endpoint GREEN frame;
- normalized advantage is finite and present;
- each nonempty side has `coverage == 1.0`;
- an empty side is valid with effective force `0.0` and coverage `None`;
- Analyzer schema, model id/version/hash, frame hash, scenario hash, and shaping
  policy identity match the transition contract.

Partial aggregation remains useful for evaluation, but should not silently
become reward authority.

## Zero-sum Symmetry

One joint shaping calculation owns one RED potential pair:

```text
phi_before = Phi_RED(committed_pre_frame)
phi_after  = Phi_RED(committed_post_frame)
red_shape  = lambda * (gamma * phi_after - phi_before)
blue_shape = -red_shape
```

This guarantees:

```text
red_shape + blue_shape == 0
base_red + base_blue == 0
composite_red + composite_blue == 0
```

The two sides must not invoke separate analyzers or independently derive BLUE
potential and merely hope their outputs agree.

## Potential-based Formula

The v1 candidate is:

```text
F(s_t, s_t+1) = lambda * (
    gamma * Phi(s_t+1) - Phi(s_t)
)
```

Neither `reward=Phi(s_t)` nor `reward=Phi(s_t+1)` is recommended. Absolute
advantage rewards the same held advantage repeatedly, biases episode length,
encourages WAIT or delay while ahead, and can swamp sparse terminal reward.
The potential difference telescopes under the same discount used by the
learner and therefore avoids repeatedly paying for the same state.

The existing `AnalysisTimeline.delta_advantages` is not a drop-in shaping
authority: it computes `Phi_next-Phi_before` with an implicit gamma of one and
uses timeline adjacency rather than `RewardTransitionIdentity`. Shaping must
use explicitly transition-aligned endpoints and the resolved shaping gamma.

## Gamma Authority

Repository search found no PPO/learner gamma, discount-factor, or GAE-lambda
configuration. The current repository provides training environments and
interfaces, but no in-repository learner discount authority.

Options:

| Authority | Benefit | Risk | Verdict |
|---|---|---|---|
| Learner config only | Matches a particular learner | Reward re-derivation depends on opaque external config | Insufficient alone |
| Shared training config | One resolved discount for learner, shaping, replay, and publication | Requires a new versioned config boundary | **Recommended** |
| Fixed shaping-policy gamma | Self-contained reward policy | Can diverge from learner gamma and invalidate invariance | Reject unless learner is contractually bound to it |
| `gamma=1` | Simple endpoint difference | Mismatches discounted learners; truncation/finite-horizon bias | Do not assume |

Recommended authority:

```text
SHAPING_DISCOUNT_AUTHORITY = SHARED_TRAINING_DISCOUNT_CONFIG
resolved_shaping_gamma == resolved_learner_gamma
```

The config must also state whether gamma is per decision transition or derived
from elapsed simulation time. Current decision interfaces can carry exact
milliseconds, so variable-duration training must not silently treat a
time-based and step-based discount as interchangeable. The resolved gamma and
discount-config identity/hash must enter shaping and replay provenance.

No numeric gamma is frozen by this audit.

## Lambda

Lambda belongs to the shaping-policy config and hash. There is no repository
experiment that justifies a single value, so:

```text
HYPERPARAMETER_NOT_FROZEN
```

Recommended selection strategy:

- include `lambda=0` as the exact base-reward ablation;
- start with small calibration-only candidates such as `0.01`, `0.03`, and
  `0.1`, not as normative defaults but as a bounded engineering probe;
- select the candidate before evaluating the disjoint validation partition;
- report small-sample and evidence limitations;
- retain base-only and base-plus-shaping benchmark results;
- never tune from validation results.

For `gamma in [0,1]` and `Phi in [-1,1]`, `lambda <= 0.1` bounds one shaping
component by `0.2`, helping keep the terminal magnitude `1.0` salient. This is
not a substitute for calibration.

## Scale

A read-only probe over the existing
`local_data/replay_viewer_demo/frame_analysis.native.jsonl` sidecar produced
the following 1,801-frame distribution. It was used only to assess scale and
did not change replay data.

| Value | Min | Median | P95 | Max |
|---|---:|---:|---:|---:|
| RED effective force | `0.0000` | `8.0692` | `60.7282` | `65.6714` |
| BLUE effective force | `39.4375` | `56.5713` | `59.7379` | `62.7391` |
| Existing normalized Phi | `-1.0000` | `-0.7580` | `0.0524` | `0.0638` |
| Raw force difference | `-52.5968` | `-40.1220` | `6.0510` | `7.8753` |
| Log ratio, probe epsilon `1e-8` | `-22.3833` | `-1.9829` | `0.1050` | `0.1277` |
| `tanh(log ratio)`, probe only | `-1.0000` | `-0.9628` | `0.1046` | `0.1271` |

The epsilon values above are comparison probes, not proposed policy values.
They demonstrate the log candidate's near-zero sensitivity. The one-replay
sample is not representative calibration evidence and must not be used to
select lambda.

For the recommended potential:

```text
abs(F_side) <= lambda * (1 + gamma)
```

No clipping or normalization belongs in the shaping authority. Learner-side
reward normalization, if ever used, is an optimization-layer contract.

## Terminal Handling

Three choices were evaluated:

| Choice | Effect | Verdict |
|---|---|---|
| Analyze terminal frame normally | Leaves a terminal potential residue and can double-count outcome with base `+/-1` | Reject for episodic v1 |
| Force absorbing `Phi_terminal=0` | Gives the episodic telescoping boundary; outcome remains solely in base reward | **Recommend** |
| Set potential from winner | Re-encodes Engine terminal outcome and duplicates base authority | Reject |

For every `terminated=True` transition, including elimination, objective win,
objective draw, and mutual elimination:

```text
Phi_next_for_shaping = 0
F_RED = -lambda * Phi_RED(s_t)
F_BLUE = -F_RED
```

The actual terminal frame may still be analyzed and stored as evaluation or
auxiliary evidence, but its advantage is not the absorbing shaping endpoint.
This policy must be explicit and hashed. The frozen base terminal mapping is
unchanged.

## Truncation

Native `max_ticks` and training `max_steps` / `max_decision_windows` are
truncations, not absorbing rule terminals. They must not automatically force
`Phi_next=0`.

For the last committed transition before either truncation source:

```text
F = lambda * (gamma * Phi(committed_post_state) - Phi(pre_state))
```

The base truncation component remains zero. Learner bootstrapping from a
truncated observation is an algorithm-layer choice, but it must use semantics
compatible with the shared discount/truncation config for policy-invariance
claims. If training truncation occurs without a committed Engine transition,
the existing `NO_COMMITTED_TRANSITION -> NO_REWARD_RECORD` rule still applies.

## Draw

A rule draw is `terminated=True`, so the absorbing terminal potential rule
applies. Base reward stays `(0,0)`, and shaping is the zero-sum terminal
boundary `-lambda * Phi_before`. A draw must not use future winner evidence or
receive a synthetic winner potential.

## Zero-force

Existing normalized advantage already defines all required cases without an
epsilon:

| RED force | BLUE force | Phi_RED | Safe |
|---:|---:|---:|---|
| `0` | `>0` | `-1` | Yes |
| `>0` | `0` | `+1` | Yes |
| `0` | `0` | `0` | Yes |

Missing is distinct from zero. A nonempty side with no valid analyzed units has
effective force `None` and must trigger the missing-analysis policy. It may not
be treated as zero force.

## Coverage and Confidence

Coverage is implemented; confidence is not. Partial side coverage currently
sums only valid units and adds a warning. That behavior is acceptable for
evaluation but can cause privileged reward to jump when an unrelated record
becomes complete.

Recommended simple v1 policy:

```text
coverage_policy   = REQUIRE_COMPLETE_NONEMPTY_SIDES
confidence_policy = IGNORE_UNAVAILABLE_CONFIDENCE_V1
```

This means nonempty sides require coverage `1.0`; empty sides are valid; no
confidence weighting, threshold, or multiplier is introduced. If confidence
is later defined, using it in reward requires a new shaping-policy version.

## Missing Analyzer

When shaping is disabled, Analyzer availability is irrelevant and the Analyzer
must not be invoked. When shaping is enabled, missing Analyzer, failed
analysis, absent advantage, incomplete required coverage, frame mismatch, or
provenance mismatch must fail closed at the supervision boundary.

The implementation must not do:

```python
try:
    shaping = analyze(...)
except Exception:
    shaping = 0.0
```

An explicit-zero fallback silently changes the reward sequence according to
runtime failures and breaks replay reproducibility. Training robustness should
come from validating Analyzer availability before episode start and surfacing
a typed contract error.

## Privileged Supervision

Eight Factor analysis consumes the complete GREEN frame. Future shaping must
therefore record:

```text
privileged_supervision_used = true
supervision_source = GREEN_EIGHT_FACTOR_ANALYZER
```

This is legitimate training supervision. It is not policy-observation
leakage. The boundary is:

```text
allowed:  GREEN -> Phi -> scalar shaping component -> learner reward
forbidden: GREEN/Phi/effective force/coverage/shaping details
           -> Decision, observation tensor, action mask, capability,
              action catalog, Human/Agent state, or LLM execution input
```

Reward calculation must not mutate World, GREEN frame, Decision, observation,
catalog, Engine, RNG, or next observation. Debug/component data in `info` must
remain explicitly training-supervision metadata and must not be consumed by
`DecisionObservationEncoder`.

## Reward Hacking Analysis

| Risk | Absolute-advantage reward | Potential difference | Remaining control |
|---|---|---|---|
| WAIT while ahead | Repeats positive reward each step | Same-state contribution telescopes under matched gamma | Match learner gamma; correct truncation bootstrap |
| Advantage oscillation | Can collect repeated positive periods | Discounted potential terms cancel under exact conditions | Transition alignment; no clipping |
| Short-term advantage for eventual loss | Dense signal can dominate terminal | Optimal-policy invariance condition limits policy change | Small lambda; terminal absorbing convention |
| Farming low-value units | Repeated state reward compounds | Pays only potential change | Factor calibration and ablation |
| Large pre-terminal swing | May swamp base indefinitely | Single bounded endpoint contribution | Bounded Phi; small lambda |
| Episode-length bias | Strong | Removed by discounted telescope under conditions | Shared discount/truncation contract |
| Both forces near zero | Raw/log candidates unstable | Existing normalized Phi is bounded but may jump to `+/-1` | Zero tests, small lambda, analysis provenance |

For `gamma<1`, an unchanged nonzero potential yields an immediate
`lambda*(gamma-1)*Phi`, not literal zero. Policy invariance concerns discounted
return telescoping, not each step being zero. A gamma mismatch or forced-zero
truncation breaks that protection.

## Policy Invariance

Status:

```text
CONDITIONALLY_SUPPORTED
```

Potential-based shaping preserves optimal policies under the standard result
when all of the following hold:

- shaping and learner use the same discount;
- Phi is a fixed Markov function of the current state;
- the reward is exactly `gamma*Phi(s')-Phi(s)` up to a fixed lambda;
- terminal states use the agreed absorbing boundary;
- truncation and learner bootstrapping are aligned;
- there is no clipping, hidden fallback, future information, or stateful
  Analyzer behavior;
- reward and observation use the same committed transition endpoints.

The repository cannot claim `FORMALLY_GUARANTEED` today because learner gamma
is absent, Analyzer semantic hashing is incomplete, and truncation bootstrap
behavior belongs to an external learner contract. Finite horizons and wrapper
truncations make those details material.

## Analyzer Versioning

Existing provenance is useful but insufficient for reward authority:

- frame record schema: `native-eight-factor-frame-analysis/1.0`;
- default model id: `native-eight-factor`;
- default model version: `0.1.0-contract`;
- factor and advantage formula ids are present in records/code;
- frame/scenario hashes can be recorded;
- default `model_hash` is `None`;
- no canonical content bundle binds factor field mapping, factor order,
  formulas, weapon weights, mobility table, side aggregation, and advantage
  formula.

The source comment hash in `demo_compat_factors.py` identifies a historical
source artifact. It is not a semantic hash of the current composed Analyzer.

Finding:

```text
ANALYZER_VERSIONING_GAP
```

Before implementation, define a canonical read-only Analyzer contract artifact
and SHA-256 hash covering at least:

- analysis schema and model id/version;
- factor order and required source paths;
- all eight formula ids and numeric constants;
- weapon/mobility tables and unknown/default behavior;
- effective-power multiplication order;
- side aggregation, missing/coverage behavior;
- advantage formula and zero-force behavior.

The resolved hash must populate `model_hash` and be required by the shaping
policy. Changing any covered semantic requires a new Analyzer version/hash and
normally a shaping-policy version bump.

The Analyzer is frozen/stateless and uses no RNG or wall clock. It is
thread/process safe provided module-level formula tables are treated as
read-only. Future workers should instantiate from immutable canonical config;
they must not share mutable analysis caches.

## Frame and Transition Alignment

For each committed joint transition:

```text
RewardTransitionIdentity.frame_before
  <-> detached GREEN pre-state endpoint
  -> FrameAnalysis_before

RewardTransitionIdentity.frame_after
  <-> detached GREEN committed post-state endpoint
  -> FrameAnalysis_after

same joint_transition_id
  -> one canonical (Phi_before, Phi_after)
  -> RED/BLUE side projections
```

If one decision transition spans multiple fact frames, shaping uses the exact
pre/post endpoints, not a maximum, minimum, average, or every intermediate
frame. No staged RED/BLUE intermediate state may be analyzed. Any frame hash,
frame index, scenario hash, time, or transition mismatch fails closed.

Online shaping must not access future winner, future frames, maximum-advantage
frame, episode return, or whole-trajectory statistics. Those remain evaluation
or offline-label inputs.

## Replay and Snapshot/Restore

Future strict replay must rederive, not trust, shaping:

```text
replayed endpoint GREEN frames
  + canonical Analyzer identity/hash
  + shaping policy identity/hash
  + shared discount identity/hash
  -> Phi_before / Phi_after
  -> shaping components
  -> composite totals and semantic hashes
```

Verification must compare frame identity/hash, potential values, side sign,
component amount, total, policy provenance, and reward hashes. Reward and
analysis artifacts remain derived training sidecars and must not enter World
snapshot or World/replay authority hashes.

Snapshot/restore before a transition must reproduce identical endpoint frames,
analysis, potential, shaping components, composite totals, and hashes.
Analyzer failure after an Engine commit must surface as a training-supervision
failure; it cannot mutate or roll back Engine authority and cannot silently
emit zero shaping.

Current analysis sidecar tests already establish deterministic online/offline
frame analysis and replay-input preservation. Shaping-specific endpoint and
reward-record rederivation remain future tests.

## Performance

The current single-frame analyzer is approximately:

```text
O(units + carried weapons + ammunition entries + visible-id entries)
```

It performs no LOS, pathfinding, Engine step, history scan, network request, or
LLM call. A read-only 250-frame probe of the existing replay produced:

| Analyzer time | Min | Median | P95 | Max |
|---|---:|---:|---:|---:|
| milliseconds/frame | `0.230` | `0.262` | `0.305` | `0.601` |

These local measurements are illustrative, not a benchmark guarantee.

Future shaping may cache the verified `Phi_after` of transition `t` as
`Phi_before` for transition `t+1`, so normal steps calculate only one new
frame. The cache is a derived optimization, not authority: its frame/model
identity must match, and replay must be able to recompute without it. Each
parallel/Ray environment must own independent immutable Analyzer config and
derived cache.

## Composite Reward

Do not change the frozen base policy. The future architecture should be:

```text
BaseRewardPolicy
  native-sparse-terminal-zero-sum/1.0.0
        +
EightFactorShapingPolicy
  native-eight-factor-reward-shaping/1.0.0
        =
CompositeRewardPolicy identity/hash
```

The composite hash covers both child identities, enabled flags, resolved
discount provenance, and composition order. It is not the base policy hash,
Analyzer hash, or runtime RewardRecord hash.

Shaping disabled must not invoke the Analyzer and must preserve the exact base
record, values, components, and hash. If compatibility requires a composite
record wrapper, its separate composite hash must not be misrepresented as the
unchanged base record hash.

## Proposed Shaping Policy (NON-IMPLEMENTED)

The following is a contract sketch, not a repository config and not approved
runtime authority:

```json
{
  "schema_version": "native-eight-factor-reward-shaping-policy/1.0",
  "policy_id": "native-eight-factor-reward-shaping",
  "policy_version": "1.0.0",
  "enabled": true,
  "component": {
    "namespace": "shaping",
    "code": "EIGHT_FACTOR_POTENTIAL",
    "units": "dimensionless"
  },
  "potential": {
    "source": "ANALYZER_NORMALIZED_RELATIVE_ADVANTAGE",
    "canonical_side": "red",
    "blue_projection": "NEGATE_CANONICAL_RED",
    "transform": "IDENTITY",
    "range": [-1.0, 1.0]
  },
  "lambda": null,
  "discount": {
    "source": "SHARED_TRAINING_DISCOUNT_CONFIG",
    "gamma": null,
    "basis": null,
    "config_id": null,
    "config_version": null,
    "config_hash": null
  },
  "formula": "LAMBDA_TIMES_GAMMA_PHI_NEXT_MINUS_PHI_BEFORE",
  "terminal_handling": "ABSORBING_PHI_NEXT_ZERO",
  "truncation_handling": "ANALYZE_COMMITTED_POST_STATE",
  "zero_force_policy": "NORMALIZED_EXPLICIT_ZERO_TOTAL",
  "missing_analysis_policy": "FAIL_CLOSED",
  "coverage_policy": "REQUIRE_COMPLETE_NONEMPTY_SIDES",
  "confidence_policy": "IGNORE_UNAVAILABLE_CONFIDENCE_V1",
  "analyzer": {
    "analysis_schema": "native-eight-factor-frame-analysis/1.0",
    "model_id": "native-eight-factor",
    "model_version": null,
    "model_hash": null
  },
  "privileged_supervision_used": true,
  "supervision_source": "GREEN_EIGHT_FACTOR_ANALYZER",
  "future_information": "FORBIDDEN",
  "reward_clipping": "NONE",
  "reward_normalization": "NONE"
}
```

Every `null` above is an unresolved freeze or versioning gap. A loader must
reject such a policy when `enabled=true`; it must not supply code defaults.
Unknown fields should fail schema validation so typos cannot alter authority.

## RewardRecord Integration

The existing base component remains untouched:

```text
base:TERMINAL_OUTCOME
```

Future enabled shaping adds exactly one independent component:

```text
shaping:EIGHT_FACTOR_POTENTIAL
```

For each side:

```text
total_reward =
    base:TERMINAL_OUTCOME.amount
  + shaping:EIGHT_FACTOR_POTENTIAL.amount
```

The shaping component should carry stable producer/policy identities and
reference the pre/post analysis hashes or frame-analysis provenance. It must
not embed GREEN frames, raw unit factors, natural-language explanations, or
future metrics in the normal reward machine record. Component sum validation,
finite-number validation, side identity, shared joint-transition identity, and
zero-sum validation remain mandatory.

## Auxiliary Label and Central Critic Boundary

Eight Factor can also serve roles other than shaping, but those roles require
separate contracts:

- `EightFactorAuxiliaryTarget`: a frame- or transition-aligned supervised
  target for an auxiliary head;
- privileged central-critic input under CTDE;
- evaluation metrics and maximum-advantage/winner-alignment reports.

None may reuse the shaping-policy identity. An auxiliary target may contain
more detail than a scalar shaping component, but it remains privileged training
data and must not enter the decentralized policy observation. Enabling or
disabling evaluation or an auxiliary target must leave base reward and shaping
reward unchanged.

Current recommended role ordering is:

1. keep Eight Factor as evaluation sidecar;
2. close Analyzer identity and shaping policy;
3. experiment with optional potential shaping via calibration/ablation;
4. design auxiliary/central-critic contracts independently.

Eight Factor is not recommended as primary reward because it is a derived,
historical-replay-compatible proxy rather than task terminal authority.

## Findings

### P0

NONE.

No Eight Factor data currently enters Decision, policy observation, action
mask, Engine adjudication, or base reward.

### P1

1. **ANALYZER_SEMANTIC_HASH_MISSING** — default `model_hash=None`; shaping
   replay cannot bind the exact Analyzer formulas/tables. This blocks enabled
   shaping implementation, not current evaluation or base reward.
2. **SHARED_DISCOUNT_AUTHORITY_MISSING** — there is no in-repository learner
   gamma or discount-basis contract. A shaping gamma cannot yet be proven equal
   to learner gamma. This blocks a policy-invariance claim and enabled shaping.

### P2

1. **SHAPING_POLICY_FREEZE_MISSING** — lambda and numeric gamma are not frozen.
2. **PARTIAL_COVERAGE_REWARD_POLICY_MISSING** — current analysis permits partial
   aggregation; v1 should require complete nonempty-side coverage.
3. **CONFIDENCE_UNIMPLEMENTED** — confidence is always `None`; v1 should ignore
   it explicitly rather than invent weighting.
4. **MODULE_TABLE_IMMUTABILITY_INFORMAL** — factor lookup dicts are treated as
   constants but are not deep-frozen canonical config objects.
5. **SHAPING_REPLAY_TESTS_MISSING** — frame analysis replay is verified, but no
   shaping record exists to rederive yet.
6. **SINGLE_REPLAY_SCALE_SAMPLE** — the scale probe is useful engineering
   evidence, not calibration or validation evidence.

### P3

1. The `demo-compatible` provenance naming is accurate but should remain
   visibly distinct from Native Engine rule authority in future policy/docs.
2. A typed shaping-specific contract error and machine codes will be needed;
   UI text and exception repr must remain outside hashes.

## Required User Freeze

| Field | Options | Evidence | Recommended default | Tradeoff / freeze status |
|---|---|---|---|---|
| Potential source | raw difference; normalized; log; tanh-log; existing normalized | Existing value is bounded, zero-safe, deterministic, replayed | Existing normalized advantage | Strong recommendation; policy freeze required |
| Potential transform | identity; tanh/log transform | Existing normalized value already lies in `[-1,1]` | `IDENTITY` | Extra transform adds unsupported constants |
| Gamma source | learner-only; shared training config; fixed shaping; one | No learner gamma exists in repository | Shared versioned training discount config | Must be identical to learner; source and basis need implementation/freeze |
| Numeric gamma | research choice | No experiment/config authority found | Equal resolved learner gamma | **HYPERPARAMETER_NOT_FROZEN** |
| Lambda | fixed research value; sweep; zero | Base terminal magnitude is `1`; bounded shaping has known maximum | Calibration-only small sweep including `0`, initially no more than `0.1` as a probe | **HYPERPARAMETER_NOT_FROZEN**; do not tune on validation |
| Terminal handling | analyze terminal; zero absorbing potential; winner potential | Episodic PBRS and base outcome separation favor zero | `ABSORBING_PHI_NEXT_ZERO` | Strong recommendation; policy freeze required |
| Truncation handling | normal committed post-state; forced zero | Truncation is not absorbing termination | `ANALYZE_COMMITTED_POST_STATE` | Must align learner bootstrap semantics |
| Missing analysis | fail closed; explicit zero | Silent zero breaks reproducibility | `FAIL_CLOSED` | Strong recommendation |
| Coverage | accept partial; weight; zero; fail closed | Partial aggregate skips unknown units | `REQUIRE_COMPLETE_NONEMPTY_SIDES` | Robustness vs episode availability |
| Confidence | ignore; weight; threshold; fail because missing | Confidence has no current formula | `IGNORE_UNAVAILABLE_CONFIDENCE_V1` | Any future use requires a new version |

Only numeric gamma, its discount basis, and lambda require genuine researcher
selection. The other rows have repository-evidence-backed defaults and should
be confirmed as policy fields rather than re-opened as arbitrary tuning knobs.

## Readiness

```text
EIGHT_FACTOR_POTENTIAL_CONTRACT_READINESS: READY_AFTER_POLICY_FREEZE
ANALYZER_VERSIONING_READINESS: READY_AFTER_FIX
EIGHT_FACTOR_SHAPING_IMPLEMENTATION_READINESS: READY_AFTER_FIX
AUXILIARY_LABEL_IMPLEMENTATION_READINESS: READY_AFTER_FIX
```

Implementation may begin only after:

1. Analyzer canonical identity/hash is implemented and populated;
2. shared learner/shaping discount authority and basis are versioned;
3. numeric gamma and lambda are frozen in a validated policy artifact;
4. terminal, truncation, coverage, missing-analysis, and confidence choices are
   confirmed in that policy.

## Required Tests

- **S1 Same frames:** same endpoint frames, Analyzer identity, discount, and
  policy produce identical potentials, shaping values, records, and hashes.
- **S2 Potential antisymmetry:** `Phi_BLUE == -Phi_RED`, including signed-zero
  canonicalization.
- **S3 Shaping zero-sum:** RED and BLUE shaping sum exactly to canonical zero.
- **S4 Disabled shaping:** base components, totals, record hashes, replay
  sequence, Decision, tensor, and mask are exactly unchanged.
- **S5 Improving state:** an otherwise comparable RED improvement gives the
  expected RED shaping sign under the configured gamma.
- **S6 Deteriorating state:** the corresponding deterioration gives the
  opposite side signs.
- **S7 Unchanged state:** verify exact configured formula, including the
  `(gamma-1)*Phi` case when gamma is not one.
- **S8 Terminal win:** absorbing terminal potential, unchanged base win/loss,
  zero-sum composite, aligned reason and transition.
- **S9 Terminal loss:** side-swapped terminal behavior.
- **S10 Draw:** zero base, absorbing terminal potential, zero-sum shaping.
- **S11 Native truncation:** normal committed post-state potential; base remains
  zero; provenance says native max ticks.
- **S12 Training truncation:** same non-absorbing rule with training truncation
  provenance and learner-bootstrap contract.
- **S13 RED zero force:** finite `Phi_RED=-1` when BLUE force is positive.
- **S14 BLUE zero force:** finite `Phi_RED=+1` when RED force is positive.
- **S15 Both zero:** finite `Phi_RED=0` and zero-safe shaping.
- **S16 Analyzer missing/failure:** enabled shaping fails closed before emitting
  a reward record; disabled shaping never invokes Analyzer.
- **S17 Coverage/confidence:** partial/no-valid coverage follows the frozen
  fail-closed rule; absent confidence is ignored exactly as versioned.
- **S18 Snapshot/restore:** identical analysis, potentials, shaping, composite
  reward, and hashes after restore and re-execution.
- **S19 Strict replay:** rederive the full shaping sequence from endpoint GREEN
  frames and compare identities, values, and hashes.
- **S20 Joint frame:** both sides use the same joint pre/post endpoint frame and
  never a staged intermediate state.
- **S21 Policy-input leakage:** GREEN, factors, force, coverage, potential, and
  shaping do not enter Decision, observation tensor, catalog, or mask.
- **S22 Purity:** no mutation of World, frames, Decision, catalog, Engine, RNG,
  reward base record, or next observation.
- **S23 Analyzer provenance:** schema/model/version/hash mismatch fails closed;
  formula/table changes alter Analyzer and shaping policy identity.
- **S24 Lambda zero:** enabled policy with `lambda=0` produces a zero shaping
  component and base-equivalent totals without Analyzer leakage.
- **S25 Side swap:** swapping RED/BLUE inputs negates canonical potential and
  shaping and preserves zero-sum composite reward.
- **S26 Parallel determinism:** isolated env/Ray workers with identical inputs
  produce identical values/hashes and share no mutable cache.
- **S27 Boundedness:** all valid potentials are in `[-1,1]` and shaping obeys
  `abs(F)<=lambda*(1+gamma)`.
- **S28 Numeric safety:** NaN, infinity, bool-as-number, overflow, and malformed
  numeric fields fail before a record is emitted.
- **S29 No future evidence:** winner, future frames, max advantage, episode
  return, and timeline-wide statistics are not read online.
- **S30 Evaluation isolation:** analysis sidecar off/on leaves base and shaping
  results unchanged for the same canonical Analyzer inputs.
- **S31 Auxiliary isolation:** auxiliary target off/on leaves base and shaping
  components/hashes unchanged.
- **S32 Policy hash:** canonical JSON/hash is stable across input key order and
  processes; every semantic field change changes the policy/composite hash.

## Existing Tests and Probes

The read-only targeted regression covered Analyzer orchestration, factor
contract, GREEN frame projection, effective power, side aggregation,
advantage, timeline, serialization/deserialization, JSONL, live analysis,
replay analysis processor/sidecar, and base reward v1:

```text
464 passed in 10.11s
```

Existing tests establish current-frame determinism, immutability, unknown and
zero behavior, non-finite rejection, stable ordering, timeline ordering,
canonical sidecar serialization, offline/live equality, and preservation of
source replay files. No shaping implementation tests exist because this audit
does not implement shaping.

## Recommended Implementation Phases

1. **Analyzer Authority Fix:** canonical Analyzer schema/config artifact,
   immutable tables, semantic hash, loader, and replay provenance.
2. **Policy Freeze:** shared learner/shaping discount config; select gamma basis
   and lambda; validate the proposed shaping-policy schema.
3. **Pure Potential Projector:** transition-aligned pre/post GREEN analyses,
   canonical RED potential, BLUE negation, terminal/truncation and fail-closed
   handling; no TrainingEnv integration yet.
4. **Composite Reward Integration:** add the independent shaping component to
   `TrainingRewardRecord`/joint records while leaving base v1 untouched.
5. **Training and Replay Verification:** SingleSeat/MultiSeat wiring,
   snapshot/strict-replay rederivation, parallel determinism, leakage tests,
   and publication metadata.
6. **Optional Research Channels:** only after ablation, separately version an
   auxiliary target or CTDE critic input; never conflate either with shaping.

## Prerequisite Closure Verification

Phase 5A closes the two implementation prerequisites identified by this audit.
It does not calculate shaping, add a reward component, change total reward, or
connect EightFactor to a training environment.

### Analyzer semantic identity

The canonical semantic authority is the typed exporter in
`analysis/semantic_identity.py`:

```text
semantic schema: native-eight-factor-analyzer-semantics/1.0
analysis schema: native-eight-factor-frame-analysis/1.0
model id:        native-eight-factor
model version:   0.1.0-contract
model hash:      77e0113c896c813033528d43bc6c132e296cb91eacf9a153d436cb9fe20bc577
```

The model hash is canonical JSON encoded as UTF-8 and hashed with SHA-256. It
covers factor order/source fields, every formula id and numeric constant,
weapon and mobility tables, missing/default behavior, left-associative unit
aggregation, side aggregation/coverage, relative advantage and zero-force
semantics. It excludes Python source bytes, file paths, comments, formatting,
imports and presentation text.

The code-authority/exporter option was selected instead of a hand-maintained
JSON analyzer config. Analyzer functions and the semantic exporter import the
same typed constants and immutable tables, so there is no second weapon,
mobility or formula authority. Runtime mappings use `MappingProxyType`; the
exported semantic document is a detached copy. Attempts to mutate the source
tables fail.

Default `EightFactorAnalyzer` and direct default frame analysis now populate
`FrameAnalysis.provenance.model_hash` with the canonical hash. Explicit legacy
or custom analysis records may still deserialize their historical nullable
model hash, but they cannot satisfy the new shaping prerequisite identity.
`require_analyzer_authority` provides a fail-closed runtime/replay identity
check.

### Shared discount authority

The versioned shared contract is:

```text
schema: native-training-discount-config/1.0
typed value: SharedTrainingDiscountConfig
identity: config_id + config_version + config_hash
bases:
  PER_DECISION_TRANSITION
  PER_SIMULATION_TIME_REFERENCE
```

Gamma is an explicit finite non-bool number in `[0,1]`. A simulation-time
config must also define a positive exact `reference_interval_ms`, with:

```text
effective_gamma(delta_time_ms)
= gamma ** (delta_time_ms / reference_interval_ms)
```

A per-decision config forbids a reference interval and resolves the same gamma
for every committed policy transition. Canonical hashing covers schema, id,
version, gamma, basis and reference interval; it excludes path, comments, wall
clock and runtime episode state. `require_discount_identity` fails closed when
learner/shaping references do not match.

There is no PPO/learner implementation in this repository, so no fictional
consumer was added. The future learner and shaping integration points must
both receive the same `SharedTrainingDiscountConfig`; neither may copy gamma
into another authority.

### Decision-delta probe and basis recommendation

A read-only probe used nine existing training configurations and one committed
decision from each:

```text
samples: 1000, 1000, 1000, 1000, 2000, 3000, 10000, 10000, 20000 ms
min:      1000 ms
median:   2000 ms
p95:      20000 ms
max:      20000 ms
unique:   1000, 2000, 3000, 10000, 20000 ms
common:   1000 ms (4/9)
```

The delta is configured and normally constant within one nonterminal training
environment, but it is not globally constant across supported configurations;
terminal scheduling can also end a window early. Because the current RL API
defines one action per decision transition, the v1 recommendation is
`PER_DECISION_TRANSITION`. This matches conventional step-based learner
semantics and avoids making action discount depend on renderer/fact-frame
cadence. Its tradeoff is that the same gamma represents different amounts of
simulation time in configurations with different decision intervals.

`PER_SIMULATION_TIME_REFERENCE` remains an explicitly supported alternative
when research intent is a real-time horizon. It requires the learner to use
the same per-transition exponentiation. Basis and numeric gamma therefore
remain an explicit research freeze; the repository does not silently select
either.

### Shaping policy prerequisite contract

The schema
`native-eight-factor-reward-shaping-policy/1.0` freezes all non-research v1
semantics:

```text
potential source:       ANALYZER_NORMALIZED_RELATIVE_ADVANTAGE
canonical side:         RED
BLUE projection:        NEGATE_CANONICAL_RED
transform:              IDENTITY
potential range:        [-1, 1]
formula:                LAMBDA_TIMES_GAMMA_PHI_NEXT_MINUS_PHI_BEFORE
terminal:               ABSORBING_PHI_NEXT_ZERO
truncation:             ANALYZE_COMMITTED_POST_STATE
zero force:             NORMALIZED_EXPLICIT_ZERO_TOTAL
missing analysis:       FAIL_CLOSED
coverage:               REQUIRE_COMPLETE_NONEMPTY_SIDES
confidence:             IGNORE_UNAVAILABLE_CONFIDENCE_V1
future information:     FORBIDDEN
clipping:               NONE
normalization:          NONE
privileged supervision: GREEN_EIGHT_FACTOR_ANALYZER
```

The policy references Analyzer and discount identities; gamma is not copied
into the shaping policy. Lambda is finite, non-bool and nonnegative, has no
unjustified upper bound, and is included in the policy hash. The repository
contains a schema and typed builder but deliberately has no enabled production
policy config while research parameters are unresolved. Test values such as
`gamma=0.99` and `lambda=0.03` are fixtures, not production defaults.

For the recommended bounded potential:

```text
abs(F) <= lambda * (1 + effective_gamma)
```

For a research candidate gamma near `0.99`:

| Lambda | Maximum one-step shaping magnitude |
|---:|---:|
| `0` | `0` |
| `0.01` | `0.0199` |
| `0.03` | `0.0597` |
| `0.1` | `0.199` |

These values are calibration candidates only. Base terminal magnitude remains
one and the base reward config/hash is unchanged.

### Tests and compatibility

New prerequisite tests cover AS1-AS15, D1-D10 and P1-P15, including canonical
ordering, every factor axis, tables, aggregation, advantage, provenance,
immutability, gamma/basis/reference identity, invalid numerics, fixed policy
semantics and mismatch failures.

Targeted and related regression at this checkpoint:

```text
new prerequisite tests: 41 passed
Analyzer/Reward/Training/Decision/Replay related: 733 passed
full suite: 1966 passed, 10 skipped, 0 failed in 294.41s
base policy hash:
  5653b7653eb46d3f091d5d0c5c04ab7dd5ce380236a4d2455214ab735a39b0cf
```

No shaping calculator, component, RewardRecord integration, learner consumer,
Engine change, Decision change, encoder change, or catalog change was added.
Default analysis numeric values remain unchanged; only derived default
FrameAnalysis provenance gains the canonical model hash. Historical World and
Engine hashes remain outside analysis identity.

### P1 closure and next readiness

```text
ANALYZER_SEMANTIC_HASH_MISSING: CLOSED
SHARED_DISCOUNT_AUTHORITY_MISSING: CLOSED

SHAPING_REPLAY_PREREQUISITE_READINESS: READY
EIGHT_FACTOR_SHAPING_IMPLEMENTATION_READINESS: READY_AFTER_USER_FREEZE
AUXILIARY_LABEL_IMPLEMENTATION_READINESS: READY
```

The only remaining research freeze is:

1. numeric gamma;
2. discount basis (`PER_DECISION_TRANSITION` recommended for v1);
3. lambda.

Once one complete shared discount config and one shaping policy are selected
using calibration-only evidence, the next phase may implement shaping. It must
still leave `native-sparse-terminal-zero-sum/1.0.0` and
`base:TERMINAL_OUTCOME` unchanged.

## Shaping v1 Implementation Verification

### Frozen production authorities

The research freeze selected a per-decision discount and a small bounded
potential coefficient. The canonical machine sources are:

```text
config/training/native_training_discount_v1.json
  schema:  native-training-discount-config/1.0
  id:      native-training-discount
  version: 1.0.0
  gamma:   0.99
  basis:   PER_DECISION_TRANSITION
  hash:    f35a748f9d0232506135b7e2438fa64f5bcc601a7aeb1fc631b182f396e4f06e

config/training/native_eight_factor_reward_shaping_v1.json
  schema:  native-eight-factor-reward-shaping-policy/1.0
  id:      native-eight-factor-reward-shaping
  version: 1.0.0
  lambda:  0.03
  hash:    bc21e92be00f9f9243b436741321f7d326f51a8bb734386a6de880dad37d7e81
```

The shaping document references the discount id/version/hash and does not
copy gamma. It also binds Analyzer model hash
`77e0113c896c813033528d43bc6c132e296cb91eacf9a153d436cb9fe20bc577`.
Typed builders remain available for lambda `0`, `0.01`, `0.03`, and `0.1`
experiments; every distinct policy gets a distinct canonical hash. These
builders do not create alternate base reward policies.

`CompositeRewardPolicy` binds the unchanged base identity and the shaping
identity under `native-training-composite-reward-policy/1.0`. The production
composite hash is:

```text
46c7743809ad7511d49c79a7c97a5bd246eb0a4d79bbe9813cbfee7f923225a4
```

The base policy hash remains exactly:

```text
5653b7653eb46d3f091d5d0c5c04ab7dd5ce380236a4d2455214ab735a39b0cf
```

### Potential and transition calculator

`training/eight_factor_shaping.py` implements immutable potential, shaping,
and composite policy records. A potential accepts only a detached committed
`FrameAnalysis` carrying the exact Analyzer identity. It requires exactly RED
and BLUE, nonempty sides, coverage `1.0`, all units valid, present finite side
effective force, and a present finite normalized advantage. Missing analysis,
partial coverage, provenance mismatch, Analyzer mismatch, or newly introduced
confidence semantics fail closed.

Only canonical RED is calculated:

```text
phi_red  = FrameAnalysis.advantage.normalized_advantage
phi_blue = -phi_red

F_red  = lambda * (effective_gamma * phi_after - phi_before)
F_blue = -F_red
```

For `terminated=True`, effective `phi_after` is the absorbing value `0`; the
post-frame Analyzer value is not used for shaping. For native or training
truncation, the committed post-frame value is analyzed normally. In
particular, unchanged potential with gamma below one is not incorrectly
forced to zero: it equals `lambda * (gamma - 1) * phi`.

With production values and `phi in [-1,1]`:

```text
abs(F) <= 0.03 * (1 + 0.99) = 0.0597
```

No clipping, normalization, WAIT/action penalty, damage reward, objective
progress reward, or future trajectory information is used.

### RewardRecord and environment integration

`native-training-reward/1.0` was retained. Its existing component extension
point is sufficient: base-only serialization omits all new optional composite
fields and therefore preserves historical base hashes. Enabled shaping adds:

```text
base:TERMINAL_OUTCOME
shaping:EIGHT_FACTOR_POTENTIAL
```

The shaping component records shaping policy identity, a transition evidence
hash, privileged-supervision classification, Analyzer identity, discount
identity, endpoint potential hashes/scalars, terminal override, and effective
gamma in the immutable shaping evidence record. `total_reward` is the exact
component sum. RED is computed once and BLUE is its exact negation, preserving
joint zero sum.

`NativeTrainingEnv` and `NativeMultiSeatEnv` expose an explicit
`enable_eight_factor_shaping` integration switch. It remains `False` by
default for backward-compatible base-only callers and historical scenarios
whose raw EightFactor coverage is incomplete. Selecting `True` loads the
production policy and fails closed on incomplete privileged evidence. This is
an explicit mode selection, not a silent zero-shaping fallback. Enabled
`lambda=0` still validates Analyzer provenance; disabled shaping does not enter
the shaping calculator. `SingleSeatTrainingAdapter` only projects its side of
the already composed joint record.

The integration reads detached analyses by exact committed frame index from
the existing live GREEN analysis sidecar. It does not read mutable World,
does not construct GREEN from `SideObservation`, and does not recalculate
separate RED/BLUE analyses. Policy Decision tensors and action masks remain
unchanged.

### Replay and publication

`verify_composite_reward_sequence` discards stored shaping scalars and
re-derives base records plus potential components from replay-derived endpoint
`FrameAnalysis` records, the exact Analyzer identity, shared discount config,
and shaping/composite policy identities. It compares transition identity,
potential evidence, side amounts, totals, record hashes, and sequence hash.
Training publication selects this verifier whenever stored reward records
carry a composite identity; base-only publication continues to use the
unchanged base verifier. Reward shaping remains a derived training artifact
and is absent from World snapshots and World/replay authority hashes.

### Ablation and performance

Supported contract modes are base-only and enabled typed shaping policies for
lambda `0`, `0.01`, `0.03`, and `0.1`. The production source is only the
`0.03` document; experiment policies are generated through the typed builder.
The implementation performs O(1) reward composition plus O(operators)
analysis already produced by the live sidecar. No history scan, replay scan,
LOS, pathfinding, network call, LLM, or mutable cross-environment cache is
used. Reusing a validated sidecar analysis does not make that cache an
authority.

### S1-S32 and integration verification

The shaping tests cover deterministic records/hashes, potential
antisymmetry, exact shaping/composite zero sum, base-only identity, improving
and deteriorating signs, gamma-discounted unchanged potential, terminal
win/loss/draw semantics, native/training truncation, all zero-force cases,
missing/coverage/confidence failures, snapshot-equivalent re-derivation,
strict reward-sequence re-derivation, joint endpoint alignment, policy-input
isolation, purity, Analyzer provenance, lambda zero, side swap, parallel-safe
stateless determinism, the analytic bound, finite validation, exclusion of
future winner/maximum advantage, evaluation-sidecar independence, absence of
auxiliary inputs, and policy/composite hash determinism (S1-S32).

Integration assertions I1-I12 cover unchanged base hash, stable discount,
shaping and composite hashes, lambda/gamma identity separation, Analyzer
provenance mismatch, unchanged Engine terminal authority, no record on
pre-commit rejection, no WAIT penalty, single-seat joint projection, and
exact RED/BLUE zero sum.

Final Phase 5B verification:

```text
reward/shaping targeted: 68 passed
Analyzer/Reward/Training/Decision/Replay related: 300 passed
full suite: 1978 passed, 10 skipped, 0 failed in 293.54s
git diff --check: PASS
```

### P2 reclassification and readiness

```text
EIGHT_FACTOR_SHAPING_CONTRACT_MISSING: CLOSED
SHAPING_REPLAY_TESTS_MISSING: CLOSED
SINGLE_REPLAY_SCALE_SAMPLE: CONTAINED_BY_ENDPOINT_REPLAY_AND_BOUND_TESTS
PARTIAL_COVERAGE_REWARD_POLICY_MISSING: EXPECTED_BY_V1_FAIL_CLOSED
CONFIDENCE_UNIMPLEMENTED: EXPECTED_BY_V1
MODULE_TABLE_IMMUTABILITY_INFORMAL: CLOSED
numeric lambda tuning: RESEARCH_HYPERPARAMETER
gamma tuning: RESEARCH_HYPERPARAMETER

AUXILIARY_LABEL_IMPLEMENTATION_READINESS: READY
```

EightFactor auxiliary labels and centralized critic inputs remain separate,
non-implemented contracts. They do not affect base or shaping reward.
