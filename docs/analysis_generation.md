# Independent Source and Native analysis

The analysis model is an explicit choice. Container format is a separate concern:
`--source-format native` means a Native replay archive, **not** the Native factor
conversion model. Neither the filename nor a failed conversion selects a model.

## Commands

Run from the repository root after installation (or prefix with `PYTHONPATH=src`):

```sh
python -m wargame_native_engine.cli.generate_analysis \
  --input /path/runs/original/replay.zip \
  --output-directory /path/runs/source-analysis \
  --analysis-model source

python -m wargame_native_engine.cli.generate_analysis \
  --input /path/runs/original/replay.zip \
  --output-directory /path/runs/native-analysis \
  --analysis-model native
```

Installed entrypoint: `wargame-native-analyze` with the same arguments.
`--analysis-model` is required. `--source-format auto|native|historical` optionally
controls the existing container reader and defaults to `auto`. Native analysis
requires a validated Native manifest; an unmarked historical archive is rejected,
not guessed or silently treated as Native.

The output directory must not exist and must be outside the original replay
folder. No overwrite option is offered. Existing replay files, analysis sidecars,
and embedded frame hashes are never rewritten. A failure removes only the new
output directory created by that invocation.

For Native containers, the result contains `frame_analysis.native.jsonl` and a
byte-identical copy of the original `replay.zip`. This bundle can be passed to the
existing replay viewer. Directory-based Native input is packed without changing
member bytes. Historical input retains the existing importer behavior and emits
an analysis sidecar only; it is not silently converted to a viewer-ready archive.
External map configuration remains a separate viewer input, as before.

## Contracts

- `source` preserves the original Source factor equations, input contract, model
  identity, serialization, and missing-data handling. It can also be selected for
  a Native archive to reproduce the original strict Source-contract analysis.
- `native` uses the separate versioned Native conversion contract and a distinct
  model identity. It shares the factor mathematics, not assumed Source field
  meanings. Native `lose_control` means current fire suppression: its active
  state applies the existing loss-of-control multiplier once. The unresolved
  legacy `keep` term is excluded by the explicit Native policy, so the same
  suppression is not counted twice. Native fatigue and close-combat factors
  retain their own current-state evidence. Absent authoritative evidence remains
  UNKNOWN; old recordings missing suppression facts are not assumed unsuppressed.
- Provenance binds each result to the original frame hash, scenario hash, and
  selected model ID/version/hash. A viewer may load either consistent model but
  must not mix models within a single run.
- Conversion does not mutate input records or backfill historical facts. A new
  export contract cannot retroactively prove missing state in an old recording.

## Python API

```python
from wargame_native_engine.analysis import (
    generate_source_analysis,
    generate_native_analysis,
)

source = generate_source_analysis("runs/original/replay.zip", "runs/source-analysis")
native = generate_native_analysis("runs/original/replay.zip", "runs/native-analysis")
```

Both return `GeneratedReplayAnalysis` with the selected model, output directory,
existing `ReplayAnalysisResult`, and optional copied replay path. They use one
shared orchestration pipeline; the original `analyze_replay_to_jsonl` API remains
unchanged for existing consumers. Neither generator alters simulation rules,
training interfaces, or Agent commands.


## Live Native generation

Append `--analysis-model native` to the existing realtime launch command:

```sh
python -m scripts.run_realtime_dual_control \
  --scenario-bundle examples/source_movement_scenario.json \
  --output-dir local_data/runs/new-native-run \
  --run-namespace new-native-run \
  --analysis-model native
```

Keep the existing `--map-runtime-data` and other launch arguments when required
by the map installation. The default `--analysis-model source` preserves the old
Source projection contract 1.1 and analysis path. Explicit Native mode selects
projection contract 1.2 and records current versioned analysis facts, including
movement lock, fire suppression, fatigue and boarding state. It changes neither
combat adjudication nor movement permissions. Only friendly views receive these
additional facts; enemy observation restrictions remain in force.

For Python integrations, set `analysis_model="native"` on
`ScenarioMatchFactory`, `ScenarioMatchFactory.from_path`, or
`WargameFacade.create_match`. Session publication recomputes the selected model
from its recorded facts and verifies exact agreement before publishing.
