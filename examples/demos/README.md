# Native Engine Demo Library

Demo-to-engine/replay/interface coverage is recorded separately in
[`config/capabilities/native_capability_registry_v1.json`](../../config/capabilities/native_capability_registry_v1.json).
Validate those links with:

```bash
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src python scripts/check_capabilities.py check
```

This directory is a data-driven catalog of small, deterministic demonstrations.
Each entry owns a scenario, a human-readable guide and a machine-readable
expected-behavior contract. Demo-specific conditions never enter Engine code.

```bash
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src python scripts/run_demo.py list
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src python scripts/run_demo.py boarding_basic
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src python scripts/run_demo.py concealment_basic
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src python scripts/run_demo.py combined_arms_test
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src python scripts/run_demo.py guided_fire_basic
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src python scripts/run_demo.py direct_fire_basic
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src python scripts/run_demo.py visibility_blocking
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src python scripts/run_demo.py objective_control_basic
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src python scripts/run_demo.py indirect_fire_basic
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src python scripts/run_demo.py loitering_munition_basic
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src python scripts/run_demo.py loitering_munition_expiry
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src python scripts/run_demo.py mechanized_assault_basic
```

All registered demos finish with `status=PASS`. The `combined_arms_test` demo
places artillery, infantry, a heavy tank, an unmanned ground vehicle and a
Drone at distinct RED positions for Viewer/Human/Agent testing; its automated
smoke path moves the Drone and verifies observation plus strict Replay. The concealment demo
executes `ENTER_CONCEALMENT`, then demonstrates deterministic exposure on MOVE.
The boarding demo exercises
the 75-second BOARD/UNLOAD lifecycle; the indirect-fire demo
uses an explicit `FIRE/INDIRECT` input and exercises mission creation, flight,
deterministic scatter, ACTIVE-area triggering, resolution and strict replay.
The two loitering-munition demos separately verify an operator-controlled
MOVE/weapon-76 attack/self-consumption flow and authoritative 1,200-second
lifecycle expiry; both strict-replay their input action/time streams.
`mechanized_assault_basic` is the first composite scenario and executes its
declared BOARD, MOVE, UNLOAD, objective-control, visibility and direct-fire
phases through one authoritative action stream.

Every run publishes:

```text
replay.zip
evidence.json
summary.json
validation_report.json
```

Visibility additionally publishes `visibility_evidence.json`. Implemented
demos always run strict Replay verification before reporting PASS.

To add a demo, create a directory with `scenario.json`, `README.md` and
`expected_behavior.json`, then add one deterministically sorted descriptor to
`registry.json`. Composite demos declare sorted `capability_ids` and ordered
`phases`; every phase selects a reusable validator. Add a validator only when
the orchestration operation itself is new; never branch on a demo ID.

The capability benchmark layer validates Demo, Human, Agent and Replay
traceability together:

```bash
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src python scripts/run_benchmark.py list
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src python scripts/run_benchmark.py check
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src python scripts/run_benchmark.py run indirect_fire_full_cycle
```
