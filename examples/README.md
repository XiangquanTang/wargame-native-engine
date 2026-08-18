# Public examples

The release examples are small, deterministic, repository-authored fixtures.
They contain no Map 123 payload, historical private scenario, SDK binary,
credential, or dependency on another checkout.

| File | Purpose | Provenance |
| --- | --- | --- |
| `source_movement_scenario.json` | Canonical six-cell movement and realtime control scenario | Project-authored synthetic fixture |
| `public_source_movement_map.json` | Presentation-only odd-r terrain for the replay viewer | Project-authored projection of the synthetic map above |
| `joint_fire_scenario.json` | Two-unit same-pre-state mutual-fire demonstration | Project-authored synthetic fixture |
| `minimal_scenario.json` | Classic minimal Native CLI scenario | Project-authored synthetic fixture |
| `direct_fire_scenario.json` | Transparent direct-fire CLI fixture | Project-authored synthetic fixture |

`run_source_release.py` executes one joint authoritative tick and verifies its
Replay. `run_joint_fire.py` submits RED and BLUE SHOOT operations through the
product layer, demonstrates mutual destruction under the configured Native
simplified simultaneous-damage policy, and strictly replays the result.
`verify_replay.py` is a thin strict-verification entry point for a replay and
its canonical scenario bundle.

The reduced historical-shaped fixtures are sanitized test data. Their own
manifests state that original content is not included; they are not needed by
the public Quick Start.
