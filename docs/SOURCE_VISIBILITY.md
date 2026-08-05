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
