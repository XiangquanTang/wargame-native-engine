# DecisionWindow architecture and verification

## Scope and invariants

This implementation formalizes the existing local RED/BLUE barrier; it does
not add another environment advance path. `CommandGateway` owns one
`DecisionWindow` per managed Session, and `SimulationScheduler` can advance
facts only through `SessionManager.advance()`, which in turn calls the existing
`LiveAnalyzedSession -> SourceCompatibleEnvironment -> GameSession.step()`
chain.

The following boundaries remain mandatory:

- a controller selects frozen action IDs and cannot supply `EngineAction`
  parameters;
- GREEN is not returned by the side-control Gateway or dual-side HTTP page;
- client timestamps never enter adjudication;
- NativeEngine remains the source of movement, visibility, damage and terminal
  decisions;
- control audit is outside replay facts and is not an input to strict replay;
- benchmark `basic.json` files become adjudication input only after explicit,
  provenance-backed NativeMap conversion.

## Window and batch contract

`DecisionWindow` freezes detached per-side `action_id -> ValidAction` maps when
the window opens. Each side submits exactly one ordered tuple of IDs. An empty
tuple is the only NoOp representation. Duplicate IDs, two actions for one
actor, cross-side IDs and IDs absent from the frozen map are rejected before
advance.

Input order remains in the control audit. The engine batch uses the stable key:

```text
Native ACTION_PRIORITY
action type
actor ID
side (tie-breaker only)
target ID
target position
action ID
```

Thus network arrival order is irrelevant, and RED is not unconditionally
placed before BLUE. `request_id` caching, payload-conflict rejection and
expected-window checks execute under the same per-Session lock as pending
mutation, closure and advance.

Lifecycle values are `OPEN`, `READY`, `CLOSED`, `ADVANCING`, `COMPLETED` and
`EXPIRED`.

## Decision, frame and simulation time

The independent integer quantities are:

- `decision_window_index`: controller opportunity;
- `frame_index`: numbered GREEN fact frame;
- `sim_time_ms`: authoritative Native simulation time.

`SimulationSchedule` requires positive integer milliseconds and requires
`decision_interval_ms % frame_interval_ms == 0`. For a 10,000ms decision and a
1,000ms frame, a closed window creates ten calls to the sole step chain. The
canonical joint batch is present only on the first interval; nine empty action
batches allow persistent Native movement/state transitions to continue.
Terminal state stops the loop immediately, so no terminal suffix is generated.

## Deadline semantics

Synchronous mode never reads a clock and closes only after explicit RED and
BLUE submissions, including explicit empty batches. It is the TrainingEnv
mode.

Server deadline mode uses an injected monotonic server clock. Client time is
diagnostic only. A missing side receives an explicit empty timeout submission,
which is written to control audit. Network latency can decide whether a batch
is accepted before the deadline; it cannot affect canonical ordering after
acceptance.

## Evidence separation

`control_audit.native.jsonl` records identity-bound service events, retries,
deadline/timeout evidence and window closure. It may contain server monotonic
time and stays outside `replay.zip`.

`steps.jsonl` records only inputs actually delivered to NativeEngine, including
frame/window indices, frame interval offset, integer delta/sim time, ordered
actions and expected state hash. Numbered GREEN frames remain authoritative
facts. `frame_analysis.native.jsonl` remains independently reproducible derived
analysis.

Strict replay binds every step and every exact canonical numbered GREEN frame,
rejects tampered intermediate frames/timing and terminal suffixes, and does not
need the control audit file.

## Deployment lifecycle

The implemented first version is pre-game Scenario construction, not direct
WorldState mutation and not a second `DEPLOY` action system. A
`DeploymentSession` binds identities, accepts only complete side-owned plans,
validates explicit allowed positions and conflicts, and finalizes one canonical
combat-stage `NativeScenario`. Source Scenario hash, deployment hash and final
Scenario hash remain separate. Opposing deployment positions are not exposed
before finalization.

The Session lifecycle vocabulary is `Created`, `SeatBinding`, `Deployment`,
`Running`, `Terminated`, `Published`, `Closed`.

## Dual-side control

The loopback-only HTTP controller exposes separate RED and BLUE token-bound
pages. Both return only the bound Side Observation, frozen side valid actions,
map presentation facts and DecisionWindow status. `POST /api/batch` accepts an
exact JSON schema containing token, request ID, expected window and ordered
action IDs; arbitrary EngineAction JSON is rejected. HTTP retries cannot
duplicate advance. The service does not claim internet-grade authentication.

## NativeMap conversion

The offline converter reads one explicitly named `basic.json` without importing
the benchmark project. It validates complete odd-r geometry, reciprocal
neighbors, terrain/road/river codes, finite elevation, source position strings
and exact source hash. Source position strings such as `"0000"` remain in the
conversion provenance alongside numeric Native hex IDs.

Maps 123 and 221 contain their own `oddr`, 200m scale and elevation grade.
Maps 96 and 9601 omit coordinate-system and scale evidence; callers must
explicitly provide `odd-r` and 200m. No filename-based inference occurs. Their
asymmetric road/river edge facts are preserved and reported, not repaired.

Only these four unique maps used by the five acceptance replays were verified.
This is not evidence for all historical maps or 370,000 replays.

## Training semantics

One `NativeTrainingEnv.step_batch()` means one DecisionWindow and can generate
multiple GREEN fact frames. The compatibility `step(action_id)` method wraps a
zero/one-action batch. The opponent may expose `act_batch`; legacy `act` is
wrapped as zero/one action. Both sides still pass through the same frozen
Gateway maps.

`info` reports the processed and next decision-window indices, generated frame
range/count and simulation-time range. Observation remains controlled-side
only; GREEN and FrameAnalysis are not Agent input. `max_steps` counts decision
windows and never fabricates a Native terminal state.

## 100-operator measurement

One bounded 50-vs-50 smoke used map 221 converted from the explicit source.
It produced 337 valid actions per side and merged 100 WAIT actions. Frozen
authorization for 674 actions took about 89ms with 0.56MB peak `tracemalloc`;
batch validation/merge took about 1.8ms. A full Recorder+Analyzer frame took
about 2.0s on this host. Visibility accounted for about 47% of profiled internal
Python time.

This proves functional scale, not a 1Hz real-time service-level objective.
No cache or spatial-index optimization was introduced from one smoke run.
Visibility is the first measured candidate for a later optimization phase; any
cache must preserve ordering, replay bytes, snapshot/restore and Session
isolation.

## Reproduction

The bounded verification commands are:

```bash
PYTHONPATH=src python -m scripts.run_decision_window_acceptance \
  --replay-root <five-sample-replay-root> \
  --snapshot-root <five-sample-sdk-snapshot> \
  --output-root /tmp/wargame_decision_window_acceptance

PYTHONPATH=src python -m scripts.measure_decision_window_scaling \
  --map-config <map-221-basic.json> \
  --operator-count 100 \
  --output /tmp/wargame_decision_window_100_measurement.json
```

The machine-readable, repository-persisted evidence summary is
`docs/verification/decision_window_acceptance.json`. Large converted replay
outputs remain under `/tmp` and are not committed.
