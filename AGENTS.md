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
