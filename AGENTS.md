# Native engine repository instructions

- This repository is independent. Never import the old benchmark repository at
  runtime or add it to `sys.path`.
- Never copy SDK binaries, SDK packages, `.engine_config`, licenses, private
  scenarios, restricted source or authentication material.
- All adjudication in this repository is **Native simplified rules**. Never
  describe it as official or SDK-equivalent behavior.
- `WorldState` is authoritative. Agents receive detached side observations and
  current native valid actions, never the mutable world object.
- Keep action intent and adjudication events separate in APIs and replay files.
- Unknown compatibility fields stay in metadata/raw attributes and do not
  authorize inferred rules.
- Preserve determinism: sort all generated actions/events, use `NativeRNG`, and
  record the scenario hash, ruleset, seed and engine version.
- Keep generated runs under `local_data/` or `/tmp`; do not commit them.
- Treat every historical source as read-only and record its source hash.
- Never infer missing historical actions from state changes or Action Echo.
- Keep calibration and validation replay partitions disjoint; validation data
  must not participate in search or candidate selection.
- Never overwrite the baseline ruleset during calibration.
- Describe candidates only as historical-replay-calibrated Native simplified
  rules, never as official rules or recovered official adjudication.
- Record all source replay hashes and parameter changes in calibrated candidate
  provenance.
- Mark small-sample calibration and evidence limitations in every recommendation.
- Evaluate validation only after candidate selection; do not tune from validation
  results.
- Treat `docs/rules/Native兵棋裁决知识库_Final.md` as the Phase 4A guide and
  `native_adjudication_tables_final.json` as the sole machine table source.
- Do not read the PDF at runtime, interpolate tables, apply continuous damage,
  or use Lanchester factors in adjudication.
- Keep source numeric codes `UNKNOWN` unless the curated JSON maps them.
- Record every direct-fire die, modifier, lookup coordinate, discrete outcome,
  table hash, seed, and Native convention flag in replay evidence.
