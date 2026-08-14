# Native runtime interface contracts

## Scope

This document closes four runtime-interface gaps without adding a parallel
engine path. The behavior remains Native simplified adjudication; it is not an
SDK-equivalence claim. Historical inputs remain read-only.

The authoritative advance chain remains:

```text
CommandGateway / SimulationScheduler
-> SessionManager.advance_ms
-> LiveAnalyzedSession.step(delta_time_ms=...)
-> SourceCompatibleEnvironment.step_views_ms
-> GameSession.step(delta_time_ms=...)
-> NativeEngine.step(delta_time_ms=...)
```

`GameSession.step` and `NativeEngine.step` remain the observable public step
boundary. The `step_ms` helpers are exact-millisecond convenience adapters, not
another adjudication implementation.

## 1. Exact simulation-time contract

`core/time.py` defines the shared validation and conversion contract:

- authoritative internal deltas are positive exact Python `int` milliseconds;
- `bool` is rejected even though it is an `int` subclass;
- callers may provide either seconds or milliseconds, never both;
- the compatibility seconds adapter uses decimal conversion and rejects values
  that cannot be represented as an exact number of milliseconds;
- schedulers and strict replay pass recorded integer milliseconds directly and
  no longer make a float seconds round trip.

The three independent counters retain their existing meanings:

```text
decision_window_index  controller opportunity
frame_index            numbered GREEN fact frame
sim_time_ms            authoritative Native simulation time
```

The exact contract and rejection matrix are covered by
`tests/test_exact_time_contract.py`; multi-frame scheduling is covered by
`tests/test_simulation_scheduler.py`.

## 2. Visibility prefilter boundary

`within_maximum_observation_distance` is a conservative necessary-condition
check. It compares odd-r hex distance only with the observer profile's maximum
range. It cannot make a pair visible. A pair that passes the prefilter still
goes through the existing terrain, concealment, elevation and line-of-sight
rules.

This is safe because the current Native modifiers only reduce the base maximum
observation distance. The exhaustive regression verifies that no pair judged
observable by the full existing rule is rejected by the prefilter over all
current observation domains and tested distances.

The historical Map 123 `123see.npz` was not integrated. It is a boolean array
with shape `(10, 100, 100, 100, 100)`, but the first-axis semantics are not
defined by a current Native contract. Treating it as authoritative without
that evidence would silently import an unknown old-platform visibility rule.
Its read-only source SHA-256 is
`019bee4a66cbf8c21ea1c010bc9449706c38d1c1ce57c89e5d9ebc38a9c33bb0`.

## 3. Movement cost and arrival contract

Path planning and movement execution continue to use the same
`MovementPlanner.segment_cost` result. No route inference was added: Engine
actions still contain explicitly submitted adjacent cells, and execution
revalidates every segment.

`tests/test_source_movement.py` now locks the authoritative matrix for:

- normal, forest, settlement, small river and soft/large river cells;
- elevation grades and compound forest/slope cost;
- continuous roads overriding applicable terrain/slope cost;
- personnel Normal/Charge1/Charge2 and vehicle Marching road levels;
- non-integral speed conversion using deterministic integer-ceiling duration;
- exact arrival only after the final required millisecond.

The tests compare independently stated expected costs, speeds and durations;
they do not calculate expected values by calling the tested cost function.

## 4. Replay prefix and long-run evidence

A published non-terminal truncated replay represents a complete, declared fact
prefix. `truncated=true` is then publication metadata, not a rule-state result;
the re-executed WorldState correctly remains active at the end of the prefix.
Strict replay still consumes every recorded step and validates every numbered
canonical GREEN frame, action batch, exact delta, simulation time and state
hash. For terminal archives, terminal/truncated/winner continue to be checked
against the Native result.

`scripts/run_engine_contract_acceptance.py` runs two independently created
managed sessions with reversed RED/BLUE submission arrival order. Both use the
same Scenario, seed, frozen WAIT batches and exact frame delta, then compare:

- exact replay ZIP bytes;
- exact online analysis JSONL bytes;
- offline-recomputed analysis bytes;
- strict replay;
- frame, step and simulation-time alignment.

The acceptance is deliberately WAIT-only: it validates scheduling, evidence,
determinism and scale, not combat quality. Existing real Map 123 MOVE/SHOOT and
terminal evidence remains covered by `scripts/run_source_native_match.py` and
the real-scenario integration tests.

## Performance interpretation

On the recorded single host, the conservative visibility prefilter reduced the
profiled 100-operator empty-frame visibility share from about 46.81% to 0.40%.
The raw 100-action frame decreased from about 1678ms to 755ms. A full managed
frame with projection, Recorder and Analyzer remained around 2.9-3.6 seconds,
depending on the workload.

These measurements prove a safe bottleneck reduction and functional
100-operator progress. They do not establish a 1Hz service-level objective.
Further optimization requires separate measurements of projection, scene
serialization, Recorder and Analyzer, without changing replay bytes or final
hashes.
