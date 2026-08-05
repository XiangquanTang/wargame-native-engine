# Source-compatible perspectives

Phase 4A.3 defines one Source-compatible frame contract with three projections:

| Perspective | Numeric value | Purpose |
| --- | ---: | --- |
| `RED` | `0` | Red controller observation |
| `BLUE` | `1` | Blue controller observation |
| `GREEN` | `-1` | Complete referee/Recorder state |

`GOD` is an internal alias for `GREEN`; public fields and documentation use
`GREEN`. The complete `96.json` shape is treated as a GREEN example, not as a
hidden-rule source.

## Projection boundary

```text
WorldState -> Visibility/Observation -> SideObservation
           -> SourceFrameExporter (GREEN)
           -> PerspectiveProjector -> RED / BLUE / GREEN
```

`PerspectiveProjector` is pure and does not import or call LOS. It consumes the
two already-computed `SideObservation` values. Querying RED or BLUE cannot alter
GREEN, WorldState, observation, valid actions, or event ordering.
`ObservationBundle.for_perspective()` returns detached values.

The policy is
`config/source_compat/source_perspective_policy_v1.json`. It records every
public/filtering decision and has a canonical content hash. Unknown data is not
copied from GREEN to make a side frame look complete.

## Unit filtering

RED contains all red units and only currently visible blue units; BLUE is
symmetric. There is no last-known-position memory. Friendly records start from
their Source record, but ID relationships are recursively limited to IDs in the
current side frame. This includes passenger, carrier, launcher, launch, target,
partner, owner and observation relations.

A visible enemy is rebuilt from the safe fields present in SideObservation.
The projector never copies its GREEN record. Unavailable Source fields receive
explicit `None`, empty collections, `UNKNOWN`, or `-1` values. Enemy ammunition,
cooldown, path, weapons, passengers, observation set, raw attributes, and
Native extensions are not disclosed. Visibility of a carrier does not imply
visibility of its passengers.

The core `engine.observe(side)` boundary also removes `raw_attributes` from
visible enemies. Friendly raw attributes remain detached copies.

## Other fields

- `valid_actions`: own side only, including nested target-ID checks; integer and
  string actor keys are handled identically.
- `actions`: GREEN keeps ordered received actions. A side receives only its own
  accepted actions. Observable enemy actions are not implemented.
- `judge_info`: GREEN keeps all adjudication evidence. A side receives a record
  only when every referenced enemy is visible or all participants are friendly.
  Native evidence, RNG data, table coordinates, world hashes, and rule-source
  evidence are removed from side records. Anonymous incoming-fire events are
  not implemented.
- `role_and_grouping_info` and `communication`: own side only; unknown ownership
  is hidden.
- `jm_points`: own or explicitly public only; unknown ownership is hidden.
- `landmarks`: public and own entries; owned enemy entries are hidden.
- `cities`, `scores`, `time`, `scenario_id`, and `terrain_id`: public.
- `extra.native`: GREEN contains replay provenance and complete allowed Native
  metadata. RED/BLUE are constructed from a small allowlist and never by
  deleting fields from GREEN.

All perspectives retain the stable top-level Source fields, using empty
collections when a perspective has no visible data.

## Environment API

```python
bundle = env.setup_views(seed=42)
red_state = bundle.red
blue_state = bundle.blue

bundle, done = env.step_views(actions, delta_time_seconds=20)
green_state = bundle.green  # referee/Recorder only
```

Compatibility methods remain: `setup()` and `step()` return GREEN only. A red or
blue controller must never receive the bundle or a reference to GREEN.

