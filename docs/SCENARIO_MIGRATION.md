# Scenario migration

The implemented boundary is:

```text
legacy JSON data -> LegacyScenarioImporter -> NativeScenario
                                      |-> schema/load validation
                                      |-> migration report
```

The importer reads JSON only. It does not import the old project, alter
`sys.path`, call an SDK, or modify its source file. The committed source is a
sanitized and reduced 3v3 fixture with the field structure observed in a local
legacy frame; metadata labels it `real_structure_reduced_fixture`, not a full
original scenario.

Mappings include scenario/map IDs, explicit red/blue color mapping, map bounds,
time limit, operators, cities-as-objectives and deployment positions. Operator
mapping covers ID, side, type/subtype, XY/grid cell, altitude, heading,
health, speed, movement/sensor ranges, weapons, state and alive status.

The reduced fixture uses an explicit linear-cell conversion:

```text
x = (cur_hex % grid_width + 0.5) * cell_size
y = (cur_hex // grid_width + 0.5) * cell_size
```

Health and speed scales are reported. Movement/sensor values are labeled
`derived_by_native_rules`; source weapon IDs are preserved and mapped to a
small Native weapon profile. Unknown fields remain in each operator's
`raw_attributes.source_values`, appear in `unmapped_fields.json`, and have no
adjudication effect.

Each migration writes `migration_manifest.json`, `field_mapping.json`,
`unmapped_fields.json`, `warnings.json` and `native_scenario.json`. The manifest
contains source identifier/hash, importer version, ruleset identity/hash,
mapped/defaulted/derived fields and output scenario hash. A safe example is in
`examples/migration_report_small/`.
