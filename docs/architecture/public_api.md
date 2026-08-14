# Product facade public API

## Purpose

The product entry point composes existing Native simplified rules.  It is not
a second engine.  All advances remain:

```text
SideController -> CommandGateway -> DecisionWindow -> SimulationScheduler
  -> LiveAnalyzedSession -> SourceCompatibleEnvironment
  -> GameSession.step -> NativeEngine.step
```

## Python entry point

```python
from wargame_native_engine.operations import WargameFacade

app = WargameFacade.open("examples", storage_root="/tmp/wargame-matches")
match = app.create_match(
    scenario_id="source-movement-demo",
    red_controller_id="red-human",
    blue_controller_id="blue-human",
    seed=42,
    decision_interval_ms=10_000,
    frame_interval_ms=1_000,
)
red = match.bind_controller("RED", controller_id="red-human")
blue = match.bind_controller("BLUE", controller_id="blue-human")

view = red.observe()
menu = red.get_unit_commands(1)
proposal = red.plan_move(1, 106, request_id="red-plan-0")
red.stage_action(proposal.action_id)
red.submit_batch(request_id="red-submit-0", expected_window=0)
blue.submit_batch(request_id="blue-empty-0", expected_window=0)

publication = match.publish("/tmp/wargame-match", truncated=False)
match.close()
```

`plan_move` accepts a destination as a planning request.  It returns a full,
adjacent route and authoritative segment durations.  The browser or caller
cannot submit or edit that path: `stage_action` resolves the action ID from the
server-owned window proposal registry.  The engine validates the route again
when it executes.

`get_unit_commands` is side-safe and groups only the current frozen actions for
that friendly unit.  SHOOT target and weapon IDs come from the rule engine;
clients do not calculate LOS or fire legality.

## Local dual-side application

```bash
PYTHONPATH=src python -m scripts.run_dual_side_control \
  --scenario examples/source_movement_scenario.json \
  --output-dir /tmp/wargame-dual-match \
  --host 127.0.0.1 \
  --port 8002 \
  --decision-interval-ms 10000 \
  --frame-interval-ms 1000
```

The command prints separate token-bound RED and BLUE loopback URLs.  It loads
the file as one `ScenarioBundle`, creates the match through `WargameFacade`,
and gives the renderer the Bundle's map record.  It is a local development
interface, not internet-grade authentication.

## Six logical boundaries

| Boundary | Public composition | Prohibited coupling |
| --- | --- | --- |
| Data | `ScenarioCatalog`, immutable `ScenarioBundle`, map conversion provenance | filename-based identity guesses or fabricated old rules |
| Engine | existing `GameSession`/`NativeEngine`/movement/visibility | HTTP, HTML or client input mutation |
| Operations | `SideController`, `UnitCommandMenu`, route proposals | direct WorldState writes or client-supplied EngineAction |
| Rendering | `RenderScene`, shared `map_renderer.js` | A*, LOS, damage or terminal logic |
| Services | existing Gateway/window/scheduler/deadline/HTTP | rule duplication or parallel advance path |
| Evidence | Recorder, strict replay, control audit, analysis sidecar | analysis/audit entering fact hashes |

## Historical data boundary

The four unique `basic.json` maps used by the five bounded samples have a
deterministic, provenance-backed `NativeMap` conversion.  Their old Scenario
documents are not automatically executable Native scenarios.  Although they
contain operators, initial positions and `time.max_time`, they do not provide
the complete Native weapon profiles required by direct fire, an explicit
Native deployment contract, or unambiguous Native terminal policy.  Supplying
defaults would invent adjudication.  They remain valid Replay/Viewer inputs
until an explicit, curated conversion configuration supplies those facts.

## Compatibility

Existing Gateway, Viewer, Agent and Training APIs remain available.  New local
Agent code should prefer `OperationAgentMatchRunner`; new Python and training
integrations should prefer `SideController`.  GREEN facts and FrameAnalysis are
never returned by these side-control interfaces.
