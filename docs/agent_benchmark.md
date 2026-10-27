# 可复现 Agent 评测入口 / Reproducible Agent evaluation

This is a **model-independent evaluation harness**, not another training loop.
It composes the existing `NativeTrainingEnv`, frozen Decision-v1/v2 catalogs,
source replay publisher and strict replay verifier. No model downloads, API keys,
PyTorch or training jobs are required.

The bundled scenarios and trivial policies are **reference infrastructure**.
They do not establish task difficulty, competitive strength, generalization,
trustworthy AI, or readiness of a scientific leaderboard.

## 1. Run the smallest complete experiment

From a complete source checkout, with Python 3.10 or later:

```bash
python -m pip install -e .
python -m wargame_native_engine.evaluation \
  --manifest benchmarks/manifests/smoke.json \
  --output /tmp/my-new-agent-smoke \
  --policy noop
```

The output directory must not already exist. On Windows, select a new local
output path instead. `wargame-native-benchmark` is the equivalent installed CLI.
Exit code `0` means every episode completed the evaluation protocol and replay
verification. Exit `1` means at least one episode failed; exit `2` means invalid
configuration or output setup. An interrupted process retains a report whose
status remains `running`; it is not a successful completed experiment.

The smoke manifest fixes one tiny synthetic scenario, seeds `42, 43`, both
sides, three decisions per episode, Decision-v2, and 1,000 ms simulation advance
per decision. The opponent always submits no actions. Run `--policy first-legal`
to exercise a catalog-bound nonempty action rather than only idle transitions.
`first-legal` chooses the first current catalog entry, not a strategic heuristic.

A slightly broader, still small reference suite is available:

```bash
python -m wargame_native_engine.evaluation \
  --manifest benchmarks/manifests/reference.json \
  --output /tmp/my-new-reference-run \
  --policy first-legal
```

It runs the tiny movement fixture plus the public river-valley and close-contact
fixtures, seeds `0, 42, 100`, both sides, and three decisions: 18 episodes and
at most 54 policy calls. Both manifests use self-contained native scenarios;
this entry point does not prepare Map123's external runtime caches.

## 2. Frozen experiment contract

Each manifest contains:

- `schema`, `benchmark_id`: versioned experiment identity.
- `scenarios`: filesystem-safe unique `id`, path relative to the manifest,
  and mandatory SHA-256 of the exact scenario bytes. A changed file is rejected
  at manifest loading and checked again immediately before each episode.
- `seeds`, `sides`: ordered, unique seeds and red/blue perspectives.
- `training_interface`: `decision-v1` or `decision-v2`; existing APIs remain intact.
- `max_decisions`: positive decision horizon, not a claim of task completion.
- `decision_interval_ms`: one engine frame and decision advance of this duration.
- `decision_budget_ms`: optional positive **post-return** policy latency budget.
- `max_trace_bytes`: positive per-episode `decisions.jsonl` budget, default 16 MiB.
  Before calling a policy, the runner reserves space for its input and bounded
  result fields. It stops with `trace_budget_exceeded` rather than silently
  dropping evidence. This budget covers the decision trace, not scenario copies
  or the engine's separately produced replay/analysis archives.

Unknown fields, duplicate JSON object keys, duplicate cases/seeds/sides, invalid
hashes, booleans masquerading as integers, and nonpositive limits are rejected.
Paths are not part of semantic identity, so a copied checkout can reproduce the
same manifest identity. The fixed no-op opponent identity is recorded explicitly.

Each case × seed × side receives a **fresh environment and policy instance**.
Policy-local state and environment counters therefore do not leak across runs.
A failed episode remains in the denominator, and subsequent cases still run.

## 3. Plug in JEV, an LLM, or a Python policy

The public Python API is `load_manifest(...)` plus `run_benchmark(...)`. A policy
factory receives an immutable `EpisodeContext(case_id, seed, side,
training_interface)` and returns an object with:

```python
class MyPolicy:
    def decide(self, observation):
        # Return a current catalog action ID, or None for an empty decision.
        ...

    def close(self):
        # Release clients/resources owned by this episode.
        ...
```

`observation` is detached JSON with these top-level fields:

- `schema`, `side`, `decision_index`;
- `observation`: the controlled side's existing filtered observation, including
  its own operators and only enemies exposed by the engine's perspective rules;
- `encoded_observation`: existing numerical Decision-v1/v2 policy features;
- `action_space`: exact frozen action IDs, records, legal mask and contract hashes;
- `weapon_inventory`: the same binding's own-side inventory.

No authoritative world, session handle, opposite-side private observation,
reward labels, or future transition is passed to `decide`. Hashes and IDs are
out-of-band identity/correlation fields, not additional physical observations.
LLM callers can use readable records; JEV adapters can consume numerical
features and score the same candidate list. The host must keep this perspective
boundary when adapting its inputs.

The runner accepts only an exact string matching the current catalog, or `None`.
Indices, booleans, arbitrary JSON objects and stale action IDs fail the episode
without advancing the environment. Nonempty selections go through
`step_bound_action` with the exact host-held observation/catalog binding. An
empty decision is a synchronous no-op against that same unchanged environment.
Changing a supplied observation cannot mutate the engine or retained log.

For a complete runnable custom policy, see
[`examples/evaluate_custom_agent.py`](../examples/evaluate_custom_agent.py):

```bash
PYTHONPATH=src python examples/evaluate_custom_agent.py \
  --output /tmp/my-new-custom-policy-run
```

Its seeded uniform catalog sampling is an integration example only. Replace the
policy implementation, not the engine's legality checks. Supply a stable
`policy_id` and JSON `policy_provenance` identifying model revision, decoding
settings, adapter revision and any relevant model seed. Never put credentials
or session tokens in provenance or traces. External inference may remain
nondeterministic even with fixed scenario seeds; report that honestly.

This synchronous evaluation boundary is separate from the continuous real-time
[function-call adapter](game_tool_calling.md). That adapter supports richer
preview/submission/result interactions. The evaluation harness deliberately
starts with one catalog selection per decision, not an unimplemented claim of
fully equivalent realtime multi-tool or multi-action scheduling performance.

## 4. Timing, failure and resource boundaries

Policy latency uses a monotonic clock around `decide` only. Engine-step latency
measures the environment advance, not input encoding, file I/O or replay
publication. `p50` and `p95` use nearest-rank percentiles; empty samples are `null`.
There is no warmup, CPU pinning or cross-host normalization in these tiny runs.
Do not treat their latency as a rigorous engine performance benchmark; use the
separate profiling scripts for controlled before/after measurements.

A returned decision whose latency exceeds `decision_budget_ms` is recorded but
not applied. Equality with the budget is allowed. Crucially, this is **not a hard
wall-clock timeout**: a synchronous blocked Python call cannot be safely killed
here. Set network timeouts in model clients and use a process supervisor when
hard deadlines or untrusted policies matter. Factory/reset/close and replay work
are outside that decision budget. The harness executes trusted adapters with
the current process's permissions; side-filtered inputs are not an OS sandbox.

Invalid actions, policy exceptions, late decisions and trace limits stop that
episode; there is no hidden fallback, retry or reward for an invented action.
Returned valid selections and actually advanced decisions have separate
counters. Environment failures preserve the attempted input and selection plus
error phase/type. Exception messages are intentionally omitted because model
SDK exceptions may embed private data. `KeyboardInterrupt`/`SystemExit` propagate
rather than being relabeled as an ordinary failed episode.

## 5. Artifacts and interpretation

The root `result.json` contains the resolved manifest identity, policy provenance,
engine/rules/semantic versions, Python source content digest, configuration and
rule-document content digests, runtime information, every episode and a summary.
This provenance works in a delivered source ZIP without `.git`. Exact config and
rule assets matter: a package version string alone is not experiment identity.

Each episode directory contains:

- `scenario.json`: the exact verified source bytes used;
- `decisions.jsonl`: side-safe input, input hash, selected action ID (only valid
  IDs are retained), validity, policy and engine latencies, and transition facts;
- `episode/replay.zip` and existing engine analysis/audit artifacts;
- `result.json`: counters, return, terminal/truncation flags, winner/outcome when
  exposed by the environment, replay checksum and strict verification status.

`strict_replay_matches=true` means replay reconstruction matched the recorded
final world under the same authoritative semantics. `replay_verification_status`
distinguishes `not_run`, `matched`, `mismatched`, and verifier `error`; a false flag
alone must not be interpreted as proof of nondeterminism. Infrastructure errors
are retained in `errors`, even when a policy failure was the original stop cause.
The current adjudication semantics are `/1.3`, including ordinary direct-fire
out-of-range zero-effect attempts. Do not compare these action spaces blindly
with historical `/1.2` runs.

`semantic_fingerprint` includes manifest, policy/build identity and semantic
trajectory evidence, but excludes latency, private filesystem paths, random
session-binding IDs and replay ZIP byte differences. It is useful for checking
reproducibility, not a cryptographic proof against an adversarial policy. Changing
build content intentionally changes the experiment fingerprint. Episode
`trajectory_hash` and final-state hashes remain separately available for
controlled semantic comparisons across engine implementations.

Metrics are intentionally conservative:

- `completed_protocol` means a valid bounded run plus strict replay verification,
  **not success at a task**. A horizon-truncated no-op run may complete the protocol.
- `valid_selection_rate` counts syntactically valid current-catalog selections,
  including `None`; late but otherwise valid selections remain valid, not applied.
- `applied_decisions` means an environment step returned, **not that a selected
  command produced its intended effect**. Inspect replay action results for that.
- `return` is the environment's existing reward, not a newly invented success
  metric. `winner`, `outcome` and terminal/truncation source are reported separately.
- Small-sample percentiles and three-step horizons are plumbing checks, not
  statistically supported claims about Agent capability.

## 6. Turning the harness into a research benchmark

Before reporting scientific results, define and publish an independently
reviewable protocol: task distributions and held-out splits; success/failure
criteria grounded in engine evidence; fixed observation permissions; candidate
limits; model and prompt revisions; inference/tool-call/cost budgets; competing
baseline policies; sufficiently long episodes; multiple seeds and uncertainty
intervals. Avoid training on test scenarios or using privileged state in policy
inputs. Compare only compatible rule/encoder versions, and retain failures rather
than reporting successful runs alone.

This implementation provides the reusable execution/evidence layer for that
work. It does not claim that the bundled three fixtures or idle opponent already
constitute a validated benchmark of complex planning or trustworthy AI.

### Build and termination integrity

Keep source/configuration/rule assets unchanged during a run. The evaluator
rehashes them at the end; detected drift sets `build_unchanged=false` and
`status=invalidated_provenance_drift`, with a nonzero CLI exit. This detects
ordinary concurrent edits, not malicious edits that are reverted before the
final check. Authoritative `terminated`/`truncated` flags now also appear
additively in `NativeTrainingEnv.reset(...).info` and step info. The evaluator
uses those booleans, never a display stage label, to recognize an initially
ended episode. Existing `TrainingStep.terminated`/`.truncated` semantics are
unchanged.

### Configurable native opponent and Source-batch baseline

The no-op default is unchanged. Python callers may additionally pass
`opponent_factory(context)`, a required explicit `opponent_id`, and optional JSON
`opponent_provenance` to `run_benchmark`. The opponent receives its own side in
`EpisodeContext`, is freshly constructed for every episode, and is reset/closed
by `NativeTrainingEnv`. Its effective identity is included in the manifest hash.
The learner remains a single catalog selection per decision.

For the separately labelled full-side rule baseline, runtime cache requirements,
compatibility differences, and CLI, see [Miaosuan Native adapter](miaosuan_baseline.md).
Do not compare that Source-batch track as if it had the catalog track's action budget.
