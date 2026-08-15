# Source-compatible visibility

`HexMapProfile` stores exact odd-r hex cells with elevation and named terrain.
LOS draws the observer-center to target-center hex line, linearly interpolates
endpoint elevation, adds one level when the observer is higher, and adds one
level to intervening residential/forest cells. It reports every cell whose
modified elevation is above the line. Adjacent cells always have LOS.

Observation is a separate calculation. Named profiles implement the supplied
personnel, vehicle, helicopter, UAV, and loitering-munition matrix. Concealment
and target residential/forest terrain each halve range. A lower concealed
vehicle ignores concealment when viewed from above. Results record LOS,
observable status, hex distance, effective limit, blockers, modifiers, and a
reason code. Raw source observation arrays remain compatibility data.

## Optional provenance-bound static lookup

`scripts.convert_map_runtime_data` can explicitly convert a matching legacy
`see.npz` into a canonical, read-only memory-mapped NPY. Runtime validates the
map identity, source hash, payload hash, dtype and exact shape before use. The
ten modes are the documented ground/20m/200m/500m endpoint combinations; mixed
height queries orient the higher endpoint first. Unknown AIR platforms are not
silently assigned a height mode.

The matrix answers only static line of sight. It does not replace the Native
observation-distance profile, concealment or forest/settlement range
modifiers, and it never makes a unit visible by itself. Direct-fire validation
records the selected mode and both hashes in `observation_evidence`. If no
runtime resource is configured, the algorithmic `HexMapProfile` path above is
unchanged.
