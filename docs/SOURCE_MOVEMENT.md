# Source-compatible movement

Phase 4A.2 adds path-segment movement to the existing Native simplified engine
loop. It does not add a second movement environment or an automatic planner.

## Unified step flow

```text
controller.step(state)
  -> GameSession.step(actions, delta_time_seconds=...)
  -> NativeEngineBackend.step(...)
  -> NativeEngine.step(...)
  -> ActionRouter
       Move / Shoot / StopMove / ChangeState / WeaponLock / WeaponUnFold
  -> MovementRuntime and table-driven DirectFireAdjudicator
  -> EventQueue -> WorldState -> observation -> valid actions
  -> SourceFrameExporter -> state, done
```

`SourceCompatibleEnvironment` is only a frame facade over this chain. It does
not own a movement loop and cannot mutate `WorldState` directly.

## Action routing and paths

Source-compatible control submits one ordered list of `EngineAction` values.
The list may mix actions for multiple units. Common actor, side, existence and
control checks run before type-specific validation. Each action gets a
monotonic `action_sequence`.

A Move action supplies `parameters.path` (or compatibility name
`parameters.move_path`) as the exact sequence of future hex centers. Every pair
must be present in the current cell's configured `neighbors[6]`. The engine
does not call A*, infer missing cells, or replace an active move task. Move
creates the first segment; it does not jump to the destination.

Due work is ordered by:

```text
event_time -> action_sequence -> event_sequence
```

## Map adapter

The optional NativeScenario hex map carries `pos`, `cond`, `elev`,
`neighbors[6]`, `roads[6]`, and `rivers[6]`. Terrain codes are converted to
named terrain by the movement policy. Road use requires a reciprocal, equal,
non-zero code on both ends of an edge; a destination-only marker is not a road
edge. Unknown terrain, road types, or elevation units reject movement.

`hex_size_m=200` is stored once in the policy with status
`DERIVED_SOURCE_PARAMETER`, based on the supplied 4 km/20-hex and 10 km/50-hex
relationships. It is not duplicated as a business-code constant.

## Vehicle speed

The next hex controls ordinary vehicle terrain speed:

| Terrain | Multiplier |
| --- | ---: |
| Normal | 1 |
| Forest | 1/2 |
| Settlement | 1/3 |
| Small-river edge | 1/2 |
| Large-river cell | 1/4 |
| Soft ground | 1/4 |

Slope levels 0 through 5 use `1, 1, 1/2, 1/3, 1/4, 1/5`. Level 6 or greater
is impassable. A continuous road edge overrides both terrain and slope.

Marching requires a vehicle, locked weapon, explicit 75-second transition, and
a wholly road-connected submitted path. Absolute speeds are 40 km/h on rural
roads, 60 km/h on general roads, and 90 km/h on graded roads. Marching cannot
leave the road or shoot.

## Personnel movement

Personnel ordinary speed ignores terrain. A real adjacent height difference
over 60 metres halves speed. `CHARGE_1` and `CHARGE_2` multiply speed by two and
four; every completed charge hex adds one fatigue level. Fatigue level one
blocks second-level charge and level two blocks movement. A fully stopped unit
recovers one level per 75 seconds.

Moving personnel that becomes suppressed completes the active center-to-center
segment, retains the remaining path, and begins stopping. The unresolved Source
`keep` code is not repurposed as suppression.

## Segment state and StopMove

Before reaching the next center, `current_hex` remains the previous center and
`cur_pos_millionths` advances monotonically. Observation and shooting use that
previous hex. On arrival the engine emits `ARRIVED_HEX`, updates `current_hex`,
removes the first `move_path` entry, rechecks the four-friendly-ground-unit
limit, and replans the next segment. Large deltas can cross several segments;
unused time is never discarded.

StopMove requests a stop without truncating the current segment. At its center,
the remaining path is cleared, speed becomes zero, and a 75-second transition
begins. `stop` becomes true only when that transition completes.

## Deterministic time, state and replay

Time uses integer milliseconds and progress uses millionths of a hex. Each
segment has one evidence record containing action/event order, scheduled time,
endpoints, speeds, terrain, elevation, slope, road, mode, distance, traversal,
progress, blocking/arrival result, and policy ID/version/hash. Updating the same
total duration in 1-, 5-, or 20-second calls produces the same WorldState and
evidence.

Snapshots contain active segments, timers, sequence counters and evidence.
The existing Native Replay bundle adds `steps.jsonl`, preserving ordered action
lists, empty-action time advances and `delta_time_ms`; no separate production
movement Replay schema is introduced.

## Observation, valid actions and Source frames

After due arrivals and timers, LOS and `see_enemy_bop_ids` are recomputed, then
valid actions are regenerated. Imported Source visibility and valid actions are
never treated as rule truth. Moving, marching, stopping and transition states
feed direct-fire legality.

`SourceFrameExporter` writes immutable `basic_speed`, current segment `speed`
in hexes/second, reached-center `cur_hex`, fractional `cur_pos`, remaining
`move_path`, `move_state`, `stop`, and `move_to_stop_remain_time`. Native-only
fatigue and transition state use `extra.native`. `actions` are the ordered
commands that produced the frame; plain movement does not fabricate
`judge_info`. A later Recorder can consume these fields and movement evidence
without adding another rules path.
