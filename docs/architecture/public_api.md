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

For a human-facing integration, compose a side controller with the grouped
product service:

```python
from wargame_native_engine.operations import ProductOperationService

operations = ProductOperationService(red)
capabilities = operations.capabilities([1, 3])
preview = operations.preview_operation(
    [1, 3],
    "MOVE",
    request_id="red-preview-1",
    destination_hex=105,
)
result = operations.execute_preview(
    preview.preview_token,
    request_id="red-execute-1",
    confirm_partial=preview.partial_confirmation_required,
)
```

The public preview deliberately omits underlying action and route-proposal
identities. Execute resolves the private, immutable actions and composes the
existing stage/submit path. The browser uses this interface so operators never
need to manage internal batches or lifecycle versions.

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

The command above is the synchronous, whole-seat DecisionWindow application:
one execute finishes that side's decision for the current window.

For continuous fixed-tick human control use the realtime adapter instead:

```bash
PYTHONPATH=src python -m scripts.run_realtime_dual_control \
  --scenario-bundle examples/source_movement_scenario.json \
  --output-dir /tmp/wargame-realtime-web \
  --run-namespace local-realtime-web-000 \
  --tick-interval-ms 1000
```

Its HTTP handlers only preview high-level product operations and enqueue a
server-bound `SeatDecision`. They never call `GameSession.step`; the
`RealtimeAgentSession` background loop alone seals RED/BLUE commands, supplies
an empty batch for a missing seat, and advances the shared Gateway/Scheduler
path once per authoritative tick. Apply-time product validation remains
authoritative, so an operation based on an older observation may be recorded
as rejected rather than rewritten or applied to stale state.

The command prints separate token-bound RED and BLUE loopback URLs.  It loads
the file as one `ScenarioBundle`, creates the match through `WargameFacade`,
and gives the renderer the Bundle's map record.  It is a local development
interface, not internet-grade authentication.

`scripts.run_realtime_agent_match --red-agent=random --blue-agent=random`
provides a deterministic reference smoke over the same `SeatDecision` and
product-validation path. The random controller receives only its detached side
observation and capabilities. It does not return action IDs, paths, GREEN or
mutable Engine objects. Its output package can be opened directly by
`scripts.run_replay_viewer` because realtime publication includes Replay and
the required analysis sidecar.

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
