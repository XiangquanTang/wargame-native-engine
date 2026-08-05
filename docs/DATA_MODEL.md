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

Canonical action intents include `DEPLOY`, `END_DEPLOYMENT`, `MOVE`,
`STOP_MOVE`, `CHANGE_STATE`, `ATTACK`, source-table `SHOOT`, `WEAPON_LOCK`,
`WEAPON_UNFOLD`, and `WAIT`. `EngineAction` intent and `EngineEvent`
adjudication output use different schemas and replay collections.

Historical evidence uses frozen `HistoricalReplay`, `HistoricalFrame`,
`HistoricalOperatorState`, `HistoricalAction`, `HistoricalEvent`, and
`HistoricalEpisodeSummary` records. Each replay has one evidence level. Raw,
normalized, and Native-comparable layers remain separate, with key fields
labelled `DIRECT`, `NORMALIZED`, `DERIVED`, `APPROXIMATED`, `UNMAPPED`, or
`MISSING`. An unconfirmed Action Echo has `executable=false`.

Operator alignment statuses are `EXACT_ID`, `SOURCE_MAPPING`, explicit
`HEURISTIC_MATCH`, and `UNMATCHED`. Time alignment always stores historical
time, target Native tick, scale, offset, strategy, and residual error.

## Source-compatible models

The direct-fire path uses odd-r hex coordinates encoded as
`row * 100 + column`. Integer `blood` and `max_blood` represent vehicles or
squads and are primary table variables; `health` is derived from their ratio.

`SourceFrame` has typed time, score, landmark, control-point, operator, and
passenger records plus untouched raw values. Mapping reports separate source,
normalized, and Native effective values. Unresolved numeric codes remain
`UNKNOWN`. Source `valid_actions`, Action Echo, and `see_enemy_bop_ids` do
not authorize rules.

Source-compatible operators additionally carry `current_hex`, fixed-point
segment progress, remaining path, movement mode, stop state, fatigue,
transition/cooldown/suppression timers, weapon lock/unfold state, and active
segment identity. `basic_speed` is immutable standard km/h; `speed` is current
segment progress in hexes per second. `WorldState.elapsed_time_ms`, sequence
counters, and movement evidence participate in snapshots and state hashes.
