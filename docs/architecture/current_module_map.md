# Current module map and product-boundary plan

## Scope

This document maps the repository at baseline
`65955dc1fb7f2af99711970ffb7750162717527a`.  The target is a product-facing
composition over the existing Native simplified-rules engine, not a second
engine and not a directory-wide rewrite.

## Real advance and observation chain

```text
HTTP / Agent / Training caller
  -> CommandGateway (identity, request id, frozen action ids)
  -> DecisionWindow (one ordered batch per side)
  -> SimulationScheduler (decision interval -> fact-frame intervals)
  -> SessionManager.advance
  -> LiveAnalyzedSession.step
  -> SourceCompatibleEnvironment.step_views
  -> GameSession.step
  -> NativeEngine.step
  -> RED / BLUE SideObservation + GREEN production frame
  -> Recorder facts + replay / EightFactorAnalyzer sidecar
```

`GameSession.step` remains the sole formal adjudication advance.  Product
facades may compose this chain but may not receive or mutate `WorldState`.

## Existing files and target logical ownership

| Existing implementation | Current responsibility | Target logical module | Boundary / migration |
| --- | --- | --- | --- |
| `io/scenario.py` | `NativeMap`, `NativeScenario`, canonical input | Data | Retain; wrap in immutable `ScenarioBundle` and catalog. |
| `io/native_map_converter.py` | explicit benchmark `basic.json` to NativeMap | Data | Retain offline converter and provenance; never import benchmark at runtime. |
| `viewer/map_config.py`, `viewer/scenario_config.py` | presentation-only historical parsing | Data compatibility | Retain replay compatibility; do not let same-name presentation facts enter adjudication. |
| `core/engine.py` | authoritative rules, observations, valid actions | Engine | Retain. Add only read-only planning adapter methods that call existing movement rules. |
| `movement/planner.py` | validates an explicitly supplied adjacent path and computes segment traversal | Engine | Single movement legality/cost source for route search and execution. |
| `movement/runtime.py` | persistent segment execution over simulation time | Engine | Retain unchanged as the only movement executor. |
| `visibility/*` | odd-r LOS, elevation, terrain and observation limits | Engine | Retain unchanged; no LOS in UI or Operations. |
| `gateway/decision_window.py` | freezes side action IDs and deterministic merge | Services | Extend its private snapshot with server-generated proposals; no parallel pending store. |
| `gateway/command_gateway.py` | identity, idempotency, deadlines, window close | Services | Operations must register/submit through this lock boundary. |
| `gateway/session_manager.py` | isolated session, Recorder, live Analyzer, publication | Services / Evidence | Retain; add read-only planner access without exposing managed state. |
| `viewer/static/map_renderer.js` | shared odd-r Canvas geometry and map/unit drawing | Rendering | Make the one shared renderer for Replay and Live dual-side modes. |
| `viewer/static/app.js` | replay player state/timeline | Rendering | Retain replay mode. |
| `viewer/static/live_app.js` | one-side map control over adjacent valid actions | Rendering compatibility | Migrate interaction state to shared scene/command contracts. |
| `viewer/static/dual_control.js` | dual-side batch list without map | Rendering / Services client | Integrate the shared map renderer; do not create another geometry implementation. |
| `training/env.py` | one DecisionWindow per training step | Operations consumer | Keep side-only observations; optionally use the same destination planner/proposal registry. |
| `agents/*` | local side Agent and frozen authorization | Operations consumer | Keep ID-only contract; use facade/controller composition where appropriate. |
| `replay/*`, `io/replay.py` | facts, steps and strict replay | Evidence | Retain independent from control audit and analysis. |
| `analysis/*` | deterministic derived FrameAnalysis | Evidence | Retain observer-only, outside adjudication. |

## Confirmed capability gaps

1. `MovementPlanner.validate_path` accepts a multi-segment adjacent path and
   computes authoritative per-segment `traversal_ms`; the Source valid-action
   generator currently emits only one-neighbour paths.
2. There is no deterministic destination-to-path query API or proposal
   registry bound to a DecisionWindow.
3. There is no immutable bundle proving that Engine and Renderer use the same
   NativeMap identity.
4. Replay and single-side Live pages use `map_renderer.js`; the dual-side page
   currently renders observation JSON and an action list only.
5. Historical Source scenarios are safe presentation inputs, while NativeMap
   conversion is adjudication-capable.  A historical scenario must not become
   executable unless all Native-required unit, deployment and terminal facts
   are explicit.
6. The measured 100-operator cost is dominated by visibility/projection and a
   fully managed frame; this task will close product interfaces before adding
   any cache.

## Target public boundary

```text
Data:       ScenarioCatalog -> immutable ScenarioBundle
Engine:     existing GameSession / NativeEngine rules adapters
Operations: UnitCommandMenu, MoveProposal, ProposalRegistry, SideController
Rendering:  RenderScene -> shared map_renderer.js
Services:   SessionManager / CommandGateway / DecisionWindow / HTTP
Evidence:   Recorder / strict replay / control audit / analysis sidecar
Facade:     WargameFacade -> MatchHandle -> SideController
```

The Facade owns composition only.  It must not expose Engine, WorldState,
Recorder internals, GREEN frames to side controllers, or mutable DecisionWindow
objects.
