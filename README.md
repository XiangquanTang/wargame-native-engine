# wargame_native_engine

`wargame_native_engine` 0.4.0 is an independent, transparent and deterministic
experiment engine. All adjudication is explicitly **Native simplified rules**;
it does not import or claim compatibility with an official SDK, authentication
configuration, binary package, or hidden rules.

## GitHub source release quick start

This release is intended to run from a cloned source tree; it is not yet a
PyPI-stable SDK distribution.

```bash
git clone <repository-url>
cd wargame_native_engine
python -m pip install -e .
PYTHONDONTWRITEBYTECODE=1 python examples/run_source_release.py \
  --output /tmp/wargame-native-source-example --seed 42
```

The example loads the committed synthetic Source-compatible scenario, obtains
detached RED and BLUE observations, selects one legal command per side,
advances one joint authoritative tick, writes a GREEN replay, and verifies it
by strict rule re-execution. It needs no private map, old SDK, credentials, or
external service.

Recommended application entry points are `WargameFacade` for product control,
`NativeMultiSeatEnv` for synchronous deterministic Agent training, and
`RealtimeAgentSession` for fixed-tick asynchronous command collection.
`GameSession.step` and `SessionManager.advance_ms` are trusted core boundaries,
not the first integration tutorial for untrusted clients.

## Engine semantic contract

One authoritative `WorldState` advances as:

```text
Collect -> Seal -> same-pre-state adjudication -> joint effect resolution
        -> working-state evolution -> atomic commit
```

All actions in one sealed batch are qualified against the same tick-start
state. Direct-fire outcomes are staged, same-target losses are aggregated, and
the working tick is committed only after action handling, ongoing movement,
visibility, and termination all succeed. A core-tick exception preserves the
prior World, NativeRNG state, logical time, and action/event sequences.

Real time is scheduling, not a per-tick chess turn: once MOVE or another
long-running action begins, the Engine advances its duration and interruption
state on later ticks without requiring the controller to resend "continue".
Manual and background clocks call the same simulation transition.

RED and BLUE are detached limited-information projections of the single
authoritative state. GREEN is the complete referee/recorder projection; it is
not passed to side Agents. The renderer and analysis paths are observer-only.

Native replay supports both recorded state frames and deterministic strict
replay. Strict replay re-executes accepted actions through this repository's
Native simplified adjudication and rejects a mismatched adjudication-semantics
version before the first step.

## Run with a versioned ruleset

The default JSON ruleset expands its operator, weapon and terrain profiles into
one canonical ruleset hash. No YAML dependency is required.

```bash
python -m wargame_native_engine.cli.run_game \
  --scenario examples/minimal_scenario.json \
  --ruleset config/rulesets/native_default_v1.json \
  --red-agent first-valid \
  --blue-agent first-valid \
  --seed 42 \
  --output local_data/runs/minimal_v2
```

Run the migrated 3v3 fixture with independent Agent seeds:

```bash
python -m wargame_native_engine.cli.run_game \
  --scenario examples/migrated_scenario_small.json \
  --ruleset config/rulesets/native_default_v1.json \
  --red-agent random-legal \
  --blue-agent random-legal \
  --seed 42 \
  --max-ticks 500 \
  --output local_data/runs/migrated_small
```

A max-tick result is `truncated: true`; it is never reported as a natural win.

## Migrate the reduced legacy-shaped fixture

```bash
python -m wargame_native_engine.cli.import_scenario \
  --source examples/legacy_scenario_reduced_fixture.json \
  --source-identifier legacy-field-structure-small/sanitized-reduced-fixture \
  --ruleset config/rulesets/native_default_v1.json \
  --output local_data/migrations/migrated_small
```

The command emits the native scenario plus manifest, field mapping, unmapped
field list and warnings. It reads legacy JSON as data and never imports the old
project at runtime.

## Evaluate procedural fairness

```bash
python -m wargame_native_engine.cli.evaluate_fairness \
  --scenario examples/migrated_scenario_small.json \
  --ruleset config/rulesets/native_default_v1.json \
  --red-agent random-legal \
  --blue-agent random-legal \
  --seeds 0:99 \
  --swap-sides \
  --mirror \
  --output local_data/fairness/migrated_small
```

Inspect `summary.json` for per-variant results, paired winner consistency,
side-swap outcome reversal, mirror consistency, score/survival/damage
differences, and replay/state mismatch counts. Overall red/blue win rate alone
is not treated as proof of fairness.

## Strict replay

```python
from wargame_native_engine.io.replay import replay_actions
from wargame_native_engine.io.scenario import load_scenario

scenario = load_scenario("examples/minimal_scenario.json")
verification = replay_actions(
    scenario,
    "local_data/runs/minimal_v2",
    ruleset="config/rulesets/native_default_v1.json",
    strict=True,
    seed=42,
)
assert verification.matches
```

Strict replay checks scenario hash, ruleset ID/version/hash and seed before
execution. `strict=False` is explicit and emits a warning on mismatch.

See `docs/RULESET_CONFIGURATION.md`, `docs/SCENARIO_MIGRATION.md`,
`docs/FAIRNESS_VALIDATION.md` and `docs/REPLAY_FORMAT.md`.

## Import and align reduced historical evidence

Only already parsed, legally obtained data is accepted. The committed example
is a small sanitized fixture, not an original official replay.

```bash
python -m wargame_native_engine.cli.import_historical_replay \
  --input examples/historical_replay_reduced_fixture/smoke-state-01 \
  --output local_data/historical_replays/smoke

python -m wargame_native_engine.cli.align_replay \
  --historical-replay local_data/historical_replays/smoke/historical_replay.json \
  --ruleset config/rulesets/native_default_v1.json \
  --output local_data/alignment/smoke
```

The importer labels evidence as `ACTION_LEVEL`, `STATE_TRAJECTORY_LEVEL`,
`EVENT_LEVEL`, or `SUMMARY_ONLY`. Only action-level evidence can drive Native
adjudication. Unknown Action Echo never becomes an inferred action.

## Calibrate bounded Native parameters

```bash
python -m wargame_native_engine.cli.calibrate_rules \
  --split config/calibration/replay_split_v1.json \
  --baseline-ruleset config/rulesets/native_default_v1.json \
  --parameter-space config/calibration/parameter_space_v1.json \
  --output local_data/calibration/native_v1

python -m wargame_native_engine.cli.evaluate_calibrated_rules \
  --split config/calibration/replay_split_v1.json \
  --ruleset local_data/calibration/native_v1/calibrated_ruleset.json \
  --partition validation \
  --output local_data/calibration/native_v1/validation
```

Search uses only the calibration partition. Validation is loaded after a fixed
candidate is selected. The baseline is never overwritten, and small samples
produce an explicit evidence limitation rather than an official-equivalence
claim. See `docs/HISTORICAL_REPLAY_IMPORT.md`, `docs/REPLAY_ALIGNMENT.md`, and
`docs/RULE_CALIBRATION.md`.

## Source-frame compatibility and direct fire

The standard frame is a data compatibility contract, not a hidden rule source:

```bash
python -m wargame_native_engine.cli.inspect_source_frame \
  --input local_data/source_frames/96.json \
  --output local_data/source_frames/96_report
```

The only executable direct-fire source is
`config/adjudication/source_tables_v1/native_adjudication_tables_final.json`.
Its schema, revision, file hash, and canonical content hash are verified before
lookup. No PDF is read at runtime and no interpolation is available.

```bash
python -m wargame_native_engine.cli.resolve_direct_fire \
  --scenario examples/direct_fire_scenario.json \
  --attacker 101 --target 201 --weapon 9101 --seed 42 \
  --output local_data/adjudication/direct_fire_smoke
```

The output is a strict `native-direct-fire-replay/1.0` bundle. Target `202`
exercises the vehicle and armor-correction path.

```bash
python -m wargame_native_engine.cli.run_source_compatible_game \
  --frame local_data/source_frames/96.json \
  --ruleset config/rulesets/native_default_v1.json \
  --table-set config/adjudication/source_tables_v1/manifest.json \
  --max-events 20 \
  --output local_data/runs/source_frame_smoke
```

See `docs/SOURCE_FRAME_COMPATIBILITY.md`, `docs/SOURCE_VISIBILITY.md`, and
`docs/DIRECT_FIRE_ADJUDICATION.md`.

## Source-compatible movement

Phase 4A.2 routes ordered Move, Shoot, StopMove and transition actions through
the same `GameSession -> NativeEngine.step` call. Integer delta-time advances
explicit hex-path segments, timers, observation and valid actions. The small
fixture is `examples/source_movement_scenario.json`; semantics and field units
are documented in `docs/SOURCE_MOVEMENT.md`.

## RED, BLUE, GREEN and production Source replay

`setup_views()` and `step_views()` return RED and BLUE isolated controller
frames plus the complete GREEN referee frame. Production recording stores only
numbered GREEN frames and the shared strict step records.

```bash
python scripts/run_minimal_source_replay.py \
  --output local_data/acceptance/source_perspectives_smoke \
  --seed 42
```

The smoke moves, changes visibility, resolves direct fire, packages
`replay.zip`, imports it through the historical-compatible reader, strictly
replays its steps, and compares final hashes. See
`docs/SOURCE_PERSPECTIVES.md`, `docs/SOURCE_REPLAY_RECORDER.md`, and
`docs/MINIMAL_END_TO_END_ACCEPTANCE.md`.
