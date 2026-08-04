# Data model

All canonical models are JSON-serializable dataclasses. The native coordinate
system is a Cartesian XY plane:

- X/Y, altitude, movement range, sensor range and attack range use scenario
  distance units; the bundled scenario declares those units as metres.
- heading is degrees clockwise from north;
- health and damage are abstract non-negative points;
- speed is distance units per tick; version 0.1 movement is bounded by
  `min(move_range, max_speed)` and does not integrate acceleration;
- tick is a discrete simultaneous-resolution counter.

`OperatorState.raw_attributes` preserves unrecognized importer fields. A known
field and its source value may both be retained when normalization is lossy.
Opaque fields have no rule effect unless a later native ruleset documents one.

Stages are `deployment`, `combat` and `terminal`. `terminal` means a native
winning/drawing condition was met. `truncated` means the max-tick guard ended
the episode without converting the guard into a winner.

Canonical actions are `DEPLOY`, `END_DEPLOYMENT`, `MOVE`, `CHANGE_STATE`,
`ATTACK` and `WAIT`. Compatibility numbers are format aliases only:

| External-format number | Native action |
|---:|---|
| 1 | MOVE |
| 2 | ATTACK |
| 6 | CHANGE_STATE |
| 333 | END_DEPLOYMENT |

Unsupported numbers produce `unsupported_action`; they are never ignored or
guessed.
