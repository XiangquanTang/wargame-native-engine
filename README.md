# wargame_native_engine

`wargame_native_engine` 0.3.0 is an independent, transparent and deterministic
experiment engine. All adjudication is explicitly **Native simplified rules**;
it does not import or claim compatibility with an official SDK, authentication
configuration, binary package, or hidden rules.

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
