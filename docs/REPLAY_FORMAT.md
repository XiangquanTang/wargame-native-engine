# Native replay format

Schema `native-replay/2.0` is a directory:

```text
manifest.json
initial_state.json
actions.jsonl
events.jsonl
snapshots/tick-XXXXXXXX.json
episode_summary.json
checksums.sha256
```

An action line records submitted intent and acceptance. An event line records
Native adjudication. The two collections have independent schema identifiers;
acceptance is never represented as an adjudication event.

The manifest records `engine_version`, `ruleset_id`, `ruleset_version`, the
expanded `ruleset_hash`, scenario/map IDs, `scenario_hash`, seed, Agents,
effective max ticks, project commit and collection hashes. Snapshot schema
`native-snapshot/2.0` repeats scenario and ruleset identity/hash and refuses a
mismatched restore.

Strict replay is the default. Before replaying it checks:

- scenario hash;
- ruleset ID, semantic version and expanded-content hash;
- manifest seed against the initial snapshot and an optional caller seed;
- every replay collection checksum and summary hash.

Any mismatch raises `ValueError`. `strict=False` must be selected explicitly
and emits `RuntimeWarning`; it never silently substitutes another ruleset.
Accepted actions are grouped by tick and replayed through `NativeEngine`, then
the final world hash is compared with the recorded result.

```text
same NativeScenario + same expanded Ruleset + same action intents + same seed
= same ordered events, snapshots and final WorldState hash
```
