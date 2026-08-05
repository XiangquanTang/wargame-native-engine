# Native rule calibration

Calibration uses the disjoint split in
`config/calibration/replay_split_v1.json`, the bounded grid in
`parameter_space_v1.json`, and explicit metric weights. The current grid varies
ground move range, Native weapon 1001 range, and deterministic damage, producing
eight candidates. Negative or unbounded values and structural protocol changes
are rejected.

The flow is baseline evaluation on calibration replays, deterministic grid
ranking, fixed best-candidate construction, then a single held-out validation
evaluation. Validation data is not loaded during search. Equal scores break by
canonical parameter hash. The baseline ruleset is read-only.

Output includes all candidates, baseline/best/validation reports, comparison,
and a candidate ruleset plus profile files. Provenance records parent ID,
version and hash, split/run IDs, source hashes, objective weights, timestamp,
parameter changes and limitations.

Recommendations are limited to `ACCEPT_CANDIDATE`, `REJECT_CANDIDATE`,
`INSUFFICIENT_EVIDENCE`, or `OVERFITTING_SUSPECTED`. Calibration improvement
with material validation regression triggers overfitting. Fewer than three
calibration or two validation replays remains insufficient evidence even when
the held-out score improves. The output is only a
**Historical-replay-calibrated native simplified rules** candidate.

The v1 split's action-level calibration and validation records are controlled
conformance fixtures. The inspected upstream smoke has state-level evidence and
`UNKNOWN` action outcomes, which are intentionally not promoted to executable
actions. Consequently, a numerically improved v1 candidate remains
`INSUFFICIENT_EVIDENCE` and is not committed as a recommended ruleset.
