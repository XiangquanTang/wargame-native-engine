# Source standard-frame compatibility

`SourceFrameImporter` treats a documented standard frame as immutable data. It
records SHA-256, validates required top-level and unit fields, reads both
`operators` and `passengers`, and retains every unknown key. `cur_hex` is
normalized from `row * 100 + column` into `(x=column, y=row)` while the original
value remains available.

Mapping reports retain source, normalized, and Native effective layers. The
final knowledge base leaves armor codes, the complete type/subtype enumeration,
ammunition-slot meanings, `keep` semantics, and observation-array indices under
ambiguity `U01`; these remain `UNKNOWN` and block affected adjudication. The 23
weapon-ID mappings in the final JSON are user-confirmed.

The exporter starts from retained raw records and updates compatible core
identity, position, discrete strength, movement, relationship, action, time,
and Native judge-evidence slots. Byte equality is not promised. Source
`valid_actions`, Action Echo, and `see_enemy_bop_ids` are never rule inputs.

`96.json` does not contain its terrain elevation/feature grid. Its compatible
smoke run therefore reports `COMPATIBILITY_LOAD_ONLY` instead of assuming a
flat map or inventing an attack.
