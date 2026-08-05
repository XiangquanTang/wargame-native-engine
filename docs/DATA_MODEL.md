# Data model

Canonical runtime models are JSON-serializable dataclasses. `NativeScenario`
is immutable and hashes its normalized JSON representation. `WorldState` is
mutable and authoritative; snapshots and observations are detached copies.

The coordinate system is Cartesian XY. Distance fields use the scenario map's
unit, heading is degrees clockwise from north, health/damage are abstract
non-negative points, speed is distance per native tick, and visibility uses
2D Euclidean radius in the current ruleset. Elevation is preserved but the
configured elevation, occlusion and concealment switches are disabled.

`OperatorState.raw_attributes` preserves source fields without granting them
rule meaning. Migrated operators distinguish:

- `source_values`: the input record;
- `normalized_values`: explicit numeric/unit conversion;
- `effective_rule_values`: values used by Native rules;
- `provenance`: source, conversion, default or `derived_by_native_rules`.

Stages are `deployment`, `combat` and `terminal`. A natural win/draw has
`terminal=true`. A guard such as `max_ticks` has `truncated=true`; the two flags
remain separate.

Canonical action intents are `DEPLOY`, `END_DEPLOYMENT`, `MOVE`,
`CHANGE_STATE`, `ATTACK` and `WAIT`. `EngineAction` intent and `EngineEvent`
adjudication output use different schemas and replay collections.
