# Table-driven direct fire

The sole executable source is
`config/adjudication/source_tables_v1/native_adjudication_tables_final.json`:

- schema `native-adjudication-tables/1.2`;
- revision `1.2.1-final`;
- file SHA-256 `8d29864abdb8ac6d5043bff0c7a0761a0f99492a4933823d71a972b8172ff32e`;
- content SHA-256 `2d0d03fa7ac5f1072b550378c20347b3e35e088a1348b459ae05967c0bab11d6`.

Loading fails on identity, hash, or table-shape errors. Lookups accept exact
integer coordinates only; interpolation and generic fallback do not exist.

Personnel selects attack level by weapon, attacker `blood`, and distance;
applies low-to-high elevation correction; rolls 2D6 for the base result; then
rolls a separate D6 with JSON modifiers. Symbolic correction uses the explicit
Native convention `NO_EFFECT < SUPPRESS < LOSS_1 ...`, recorded in evidence.

Vehicles select attack level by weapon/distance, use target `blood`, attack
level, and a first 2D6 for base loss, then an independent 2D6 plus JSON
modifiers and named armor for correction. Infantry light weapons use their
special attack table and shared personnel base-result table, followed by the
vehicle correction path. Final losses are clamped to remaining strength.

Every legal attack consumes one declared ammunition unit and starts the
JSON-defined cooldown. Numeric vehicle loss suppresses; repeated personnel
suppression loses one squad and resets the JSON-defined 150-second timer.
Vehicle losses propagate equally to carried squads.

This is transparent source-table-driven Native simplified adjudication, not an
official, SDK-equivalent, or hidden-algorithm implementation.
