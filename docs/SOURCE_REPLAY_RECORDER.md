# GREEN Source-frame Recorder

`SourceFrameRecorder` is an observer-only writer. It accepts detached GREEN
frames already produced by `SourceCompatibleEnvironment`; it does not call the
engine, calculate observation, create valid actions, advance time, consume RNG,
or mutate frames.

## Archive layout

```text
replay.zip
├── 0.json
├── 1.json
├── ...
├── steps.jsonl
└── replay_manifest.native.jsonl
```

Only numbered Source-compatible GREEN frames use `.json`, allowing historical
readers to enumerate frames without mistaking a manifest for state. RED and
BLUE are runtime observations and are never production replay members.

`0.json` is the state after `env.setup_views()`, with empty `actions` and
`judge_info`. `N.json` is the complete GREEN state after the Nth environment
step. Its actions are the actions received for that step; its judge information
contains only adjudication produced during that step and events becoming due in
that step. Pure movement does not create shooting judge information.

## Strict steps and manifest

`steps.jsonl` reuses `native-step/1.0` semantics: sequential step index,
integer `delta_time_ms`, ordered actions, action and event ordering metadata,
and expected WorldState hash. Empty-action timer/movement steps are retained.

The single-line manifest records scenario, terrain, episode, engine, ruleset,
adjudication table set, movement policy, perspective policy, seed, counts,
termination, winner, initial/final state hashes, steps SHA-256, individual frame
hashes, and a frame-collection root. UTC `started_at` is excluded from the
deterministic manifest-content hash.

The archive importer validates continuous numbering, one initial frame plus one
frame per step, every numbered-frame hash, steps hash, frame-collection hash,
deterministic manifest hash, and GREEN perspective identity. Strict source
replay then checks scenario/ruleset compatibility, replays ordered steps through
`NativeEngine.step()`, checks every state hash, and compares the final hash.

## Deterministic writing

JSON uses UTF-8, sorted keys and stable compact separators. Files are written to
a sibling temporary file, flushed, and atomically replaced. ZIP members have a
stable order, fixed 1980 timestamp, fixed compression method, and stable Unix
mode. Repackaging one finalized run is byte-identical. Different runs may have a
different `started_at`, but frame, steps, state, and deterministic-content hashes
remain comparable.

`SourceReplayArchiveImporter` reads both historical numbered-frame ZIP files
and Native ZIP files. It ignores JSONL sidecars as frames and emits no warning
merely because Native provenance is absent from a historical archive.

