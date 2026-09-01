# Native replay format

Schema `native-replay/2.0` is a directory:

```text
manifest.json
initial_state.json
actions.jsonl
events.jsonl
steps.jsonl
snapshots/step-XXXXXXXX.json
episode_summary.json
checksums.sha256
```

`step-XXXXXXXX` is a replay-local, monotonic storage sequence. It is not an
Engine clock and does not replace `world.tick` or `elapsed_time_ms` inside the
snapshot. This keeps every recorded state unique when several steps occur in
the same integer second. The reader continues to accept legacy archives whose
snapshot files use `tick-XXXXXXXX.json`.

An action line records submitted intent and acceptance. An event line records
Native adjudication. The two collections have independent schema identifiers;
acceptance is never represented as an adjudication event.

The manifest records `engine_version`, `adjudication_semantics_version`,
`ruleset_id`, `ruleset_version`, the expanded `ruleset_hash`, scenario/map IDs,
`scenario_hash`, seed, Agents, effective max ticks, project commit and
collection hashes. Snapshot schema `native-snapshot/2.0` repeats scenario,
ruleset and adjudication-semantics identity and refuses a mismatched restore.

Strict replay is the default. Before replaying it checks:

- scenario hash;
- ruleset ID, semantic version and expanded-content hash;
- adjudication semantics before constructing or stepping the replay engine;
- manifest seed against the initial snapshot and an optional caller seed;
- every replay collection checksum and summary hash.

Any mismatch raises `ValueError`. `strict=False` must be selected explicitly
and emits `RuntimeWarning`; it never silently substitutes another ruleset.
Accepted actions are grouped by tick and replayed through `NativeEngine`, then
the final world hash is compared with the recorded result.

`native-joint-prestate/1.1` identifies sealed-batch validation against one
tick-start state plus deterministic aggregate-then-apply effects and the
explicit concealment-posture action/exposure contract. A replay
without that identity, or with the former ordered-sequential meaning, fails
strict compatibility at step zero. This release does not provide a legacy
semantics executor.

```text
same NativeScenario + same expanded Ruleset + same action intents + same seed
= same ordered events, snapshots and final WorldState hash
```

Historical replay archives are deliberately separate from Native replay
bundles. Normalized historical import schema `historical-replay/1.0` retains
source hash, evidence level, source time/coordinate units, raw fields, actions,
events, frames and summary. It does not provide Native strict-replay guarantees
and does not turn Action Echo into an accepted action. Replay-driven alignment
passes mapped action intents through the normal `NativeEngine.step()` API and
reports unsupported mappings separately from adjudication events.

## Direct-fire replay

Schema `native-direct-fire-replay/1.0` contains `manifest.json`,
`request.json`, `events.jsonl`, `resolution.json`, and `final_state.json`.
The manifest records engine version, seed, action/adjudication IDs, final
table-set identity and hashes, collection checksums, resolution hash, and final
state hash.

Strict replay reconstructs the request, derives `NativeRNG` from the seed,
repeats every die and exact JSON lookup, and compares both hashes. A mismatched
table identity or changed collection is rejected.

## Source-compatible step timing

Native Replay writes `steps.jsonl` in addition to action and event collections.
Each line records the ordered action list, action sequences, integer
`delta_time_ms`, tick bounds and resulting world hash. Empty-action steps are
retained because they advance movement and timers. Strict replay restores the
same ordered-list versus side-mapping input form. This is an extension of the
existing Native Replay bundle, not a separate movement replay format.

## Production Source-frame replay

Schema `native-source-frame-replay/1.0` contains numbered complete GREEN
standard frames, `steps.jsonl`, and `replay_manifest.native.jsonl`. Only the
frames use `.json`, so a historical numeric-frame reader does not treat a
manifest as state. `0.json` has empty action/judge fields; each later frame
carries the action echo and adjudication that created its current state.

The importer validates every frame and collection hash. Strict Source replay
uses the shared ordered steps to drive the normal Native engine and compares
each expected state hash and the final hash. This complements rather than
replaces `native-replay/2.0`.
