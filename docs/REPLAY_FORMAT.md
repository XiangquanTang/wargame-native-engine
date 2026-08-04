# Native replay format

Schema `native-replay/1.0` is a directory:

```text
manifest.json
initial_state.json
actions.jsonl
events.jsonl
snapshots/tick-XXXXXXXX.json
episode_summary.json
checksums.sha256
```

An action line records Agent intent and the engine acceptance decision. An
event line records an actual native adjudication outcome. They use independent
schema names and identifiers; acceptance is never treated as execution.

The finalized manifest records engine/ruleset/schema versions, scenario and
map identifiers, scenario hash, seed, Agents, project commit, start time and
SHA-256 hashes for every collection. A reader verifies all hashes. The replay
executor groups accepted submitted actions by `tick_before`, resets the same
scenario and seed, and compares the final result/world hash.

Reproducibility contract:

```text
same NativeScenario + same ruleset + same ordered actions + same seed
= same events, snapshots and episode result
```
