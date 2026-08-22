# Public examples

The release examples are small, deterministic, repository-authored fixtures.
They contain no Map 123 payload, historical private scenario, SDK binary,
credential, or dependency on another checkout.

| File | Purpose | Provenance |
| --- | --- | --- |
| `source_movement_scenario.json` | Canonical six-cell movement and realtime control scenario | Project-authored synthetic fixture |
| `public_source_movement_map.json` | Presentation-only odd-r terrain for the replay viewer | Project-authored projection of the synthetic map above |
| `joint_fire_scenario.json` | Two-unit same-pre-state mutual-fire demonstration | Project-authored synthetic fixture |
| `public_river_valley_scenario.json` | 24x18 continuous-control battlefield | Project-authored deterministic fixture |
| `public_river_valley_map.json` | Terrain-rich presentation map for that battlefield | Project-authored deterministic fixture |
| `public_close_combat_scenario.json` | 24x18, 6v6 immediate-contact Human/Agent showcase | Project-authored deterministic fixture |
| `public_close_combat_map.json` | Terrain-rich presentation map for the close-combat fixture | Project-authored deterministic fixture |
| `minimal_scenario.json` | Classic minimal Native CLI scenario | Project-authored synthetic fixture |
| `direct_fire_scenario.json` | Transparent direct-fire CLI fixture | Project-authored synthetic fixture |

`generate_public_battlefield.py` deterministically rebuilds both river-valley
records byte-for-byte. The map contains 432 configured cells, five terrain
semantics, elevation, roads, rivers, bridges and 12 controllable units.
`generate_close_combat_demo.py` reuses that project-authored terrain recipe and
deterministically builds a separate 6v6 deployment. Paired starting positions
have clear LOS at six to eight hexes, so direct-fire-capable vehicles have
legal initial SHOOT actions without weakening visibility or weapon rules.

`run_source_release.py` executes one joint authoritative tick and verifies its
Replay. `run_joint_fire.py` submits RED and BLUE SHOOT operations through the
product layer, demonstrates mutual destruction under the configured Native
simplified simultaneous-damage policy, and strictly replays the result.
`verify_replay.py` is a thin strict-verification entry point for a replay and
its canonical scenario bundle.

`scripts/run_tactical_agent_demo.py` runs a deterministic rule controller for
each seat over `RealtimeAgentSession`. The controller sees only detached side
observations and current server-issued capabilities; it never reads GREEN or
constructs paths. The generated evidence package can be opened with
`public_close_combat_map.json`.

The reduced historical-shaped fixtures are sanitized test data. Their own
manifests state that original content is not included; they are not needed by
the public Quick Start.
