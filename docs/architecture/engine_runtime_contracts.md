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

## Joint tick and transaction boundary

The source-compatible core implements the following fixed semantic contract:

```text
sealed ordered action batch
-> common validation against immutable tick-start WorldState
-> stage direct-fire intents/outcomes against that same pre-state
-> aggregate same-target effects deterministically
-> apply effects and advance ongoing runtime on a detached working WorldState
-> refresh visibility and evaluate termination
-> atomically commit the complete working runtime
```

Canonical ordering remains evidence ordering and deterministic RNG ordering; it
does not give the first actor a chance to invalidate another actor's already
submitted same-tick fire. The configured `simultaneous_damage`,
`destroyed_actor_executes_submitted_action`, and `aggregate_then_apply`
contracts are therefore effective runtime semantics.

`NativeEngine._step_ms` clones WorldState and NativeRNG plus action/error
counters, event state and valid-action cache before dispatch. Success commits
the complete working runtime. Any exception in action staging, movement,
visibility, objective or termination processing restores the exact tick-start
runtime. Recorder and analysis remain downstream observers and cannot create a
partially committed simulation tick.

Replay manifests and snapshots record
`adjudication_semantics_version=native-joint-prestate/1.1`. Strict replay checks
that field before constructing or stepping the replay engine; legacy ordered
sequential archives require an explicit compatible executor and are not
silently interpreted under the joint semantics.

Version 1.1 also makes `ENTER_CONCEALMENT` an explicit posture action and
derives exposure from accepted MOVE, DIRECT-fire shooter, and GUIDED-fire
shooter inputs. Earlier archives are therefore never silently reinterpreted
under this posture transition contract.

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

The historical Map 123 `123see.npz` is supported through the explicit offline
`scripts.convert_map_runtime_data` boundary. Read-only archived documentation
establishes its five axes as
`(mode, point1_row, point1_column, point2_row, point2_column)` and identifies
the ten modes as ground/20m/200m/500m endpoint-height combinations. It is a
static line-of-sight lookup, not a final unit-observation result.

The compressed file is 22,335,408 bytes, but its boolean shape
`(10, 100, 100, 100, 100)` expands to 1,000,000,000 data bytes (about 953.7
MiB). The converter therefore extracts a validated bool NPY once; runtime uses
a shared read-only `mmap` plus a bounded result cache and never expands the
matrix into Python objects or copies it per Session. The adapter validates the
five-dimensional shape, map identity, source/payload hashes and explicit mode
selection. Its read-only source SHA-256 is
`019bee4a66cbf8c21ea1c010bc9449706c38d1c1ce57c89e5d9ebc38a9c33bb0`.

When no canonical map-runtime directory is selected, the algorithmic Native
LOS remains authoritative. It applies the
knowledge-base high-endpoint bonus to whichever endpoint is higher, independent
of query direction. Explicit canonical `DRONE` and `LOITERING_MUNITION`
platforms use the drone/loitering observation-distance profile in both the
cheap distance gate and the full observation/direct-fire query. Unknown AIR
platforms are not guessed into that category. When matching runtime data is
selected, the static matrix supplies LOS only; Native observation distance,
forest/settlement and concealment modifiers still decide final observation.
Every lookup used by direct-fire evidence records its mode and both source and
canonical payload hashes.

## 3. Movement cost and arrival contract

Path planning and movement execution continue to use the same
`MovementPlanner.segment_cost` result. No route inference was added: Engine
actions still contain explicitly submitted adjacent cells, and execution
revalidates every segment.

The legacy `cost.pickle` is likewise accepted only by the explicit converter.
A builtins-only unpickler rejects globals, reducers and persistent references;
the decoded value must be exactly four rectangular mode matrices whose keys are
real adjacent cells and whose finite positive factors have the documented
`maximum_speed / actual_speed` meaning. Runtime reads deterministic canonical
JSON, never pickle. When configured, this factor becomes the authoritative
per-edge Native simplified movement input and is recorded in every segment's
evidence. Without it, the existing policy formula below remains the fallback.
The Map 123 source SHA-256 is
`3a305afa0fc9a6cf49788453597bb02b556b987b5b67b63a13d2b9088b7abb6b`.

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
