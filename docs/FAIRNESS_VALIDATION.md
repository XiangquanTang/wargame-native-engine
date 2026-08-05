# Fairness validation

Version 0.2.0 tests procedural symmetry, not military realism or absolute
fairness. `ScenarioSymmetryTransformer` swaps red/blue labels, mirrors all map
positions and objectives, swaps Agent assignments and reverses result labels.
Every transformed scenario receives a new canonical hash.

The fairness CLI runs the original scenario and optional side-swap and vertical
mirror variants for each seed. Role A/B Agent seeds stay attached to the Agent
role when sides are swapped, so paired comparisons do not accidentally change
the random stream.

Outputs are `manifest.json`, `games.jsonl`, `summary.json`, `failures.jsonl`
and per-game replays. Summary fields include terminal/truncated and red/blue/
draw counts, per-variant win rates, tick/action averages, illegal actions,
Replay/state hash mismatches, paired winner consistency, side-swap outcome
reversal, mirror consistency and average role score/survival/damage differences.

Interpretation separates scenario geometry, Agent policy, random sampling and
side labels. A non-50% aggregate win rate is not itself an engine defect. The
stronger implementation checks are:

- action mapping insertion order does not change events or world hash;
- operator dictionary insertion order does not change world hash;
- legal mutual attacks execute and can produce a configured draw;
- same seed reproduces complete batch summaries;
- Snapshot continuation equals uninterrupted hashes;
- every game strictly replays to its recorded final world hash;
- paired Agent-role winners reverse physical side labels under side swap.

The implementation uses standard-library means and proportions; it introduces
no external statistics dependency.
