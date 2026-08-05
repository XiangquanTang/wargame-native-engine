# Ruleset configuration

The default entry point is `config/rulesets/native_default_v1.json`. It
references independently schema-versioned operator, weapon and terrain JSON
files. Loading expands all four documents, validates supported semantics and
computes one canonical SHA-256 content hash.

Identity metadata is:

```json
{
  "ruleset_id": "native-default",
  "ruleset_version": "1.0.0",
  "ruleset_type": "native_simplified",
  "minimum_engine_version": "0.2.0",
  "coordinate_unit": "m",
  "deterministic": true
}
```

## Parameters currently represented

- timing: tick duration/unit, simultaneous deployment/combat declarations,
  WAIT permission and nullable no-progress threshold;
- movement: default/type range, reject-at-boundary policy, impassable terrain,
  terrain multipliers, overlap/spacing and floating tolerance;
- visibility: default/type sensor radius, friendly visibility declaration,
  enemy condition, Euclidean distance, inclusive boundary and disabled
  occlusion/elevation/concealment switches;
- combat: default range/damage, deterministic hit, ammunition switch,
  simultaneous aggregate damage, committed destroyed-actor behavior, invalid
  target policy, health floor/threshold and tolerance;
- terminal: elimination, objective threshold, mission score switch, max ticks,
  mutual elimination/tie result and evaluation order.

Scenario operator values are explicit effective overrides when positive;
otherwise profile/default rule values apply. A scenario can provide its mission
max tick and objective threshold, while CLI `--max-ticks` is an explicit run
override recorded in Replay.

The current engine validates only implemented combinations: deterministic
rules, 2D Euclidean visibility, rejected out-of-bounds moves, simultaneous
aggregate damage and draw on mutual elimination. Unsupported combinations fail
during `load_ruleset`, before reset.

There are no runtime YAML or JSON-Schema packages. Validation uses the standard
library against the same required fields and semantic constraints documented by
the JSON schemas under `config/schemas/`.
