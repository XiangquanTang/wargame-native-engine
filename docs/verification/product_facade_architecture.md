# Product facade architecture and acceptance

## Result

The repository now exposes one application composition over the six logical
boundaries.  Canonical NativeScenario data is loaded once as a `ScenarioBundle`;
its detached scenario feeds Engine setup and its map projection feeds both
Replay and Live rendering.  `WargameFacade -> MatchHandle -> SideController`
provides the application API without creating another advance path.

## Movement and command authorization

`DeterministicRoutePlanner` uses Dijkstra with `(total cost, complete path)` as
the stable tie-break.  It asks the existing `MovementPlanner` for every edge's
legality and `traversal_ms`.  A route is only a proposal.  The Gateway stores a
detached `EngineAction` in the active window registry; the client receives an
opaque action ID, and execution revalidates every adjacent segment.

The integration scenario planned `101 -> 105` as `(102, 103, 104, 105)` with
80,000ms authoritative duration.  With a 10,000ms DecisionWindow and 1,000ms
fact frame, persistent movement crossed five observed positions without the
controller resubmitting MOVE.  A later SHOOT was selected from a frozen engine
action.  Strict replay passed and online/offline analysis JSONL bytes matched.

## Rendering and side safety

`RenderScene` combines the static Bundle map with either a RED/BLUE detached
ControllerView or a validated GREEN Replay frame.  The same Canvas geometry
and `map_renderer.js` draw Replay and Live modes.  Live overlays contain only
server planned paths and engine-authorized shoot target IDs.  No JavaScript
pathfinding, LOS or damage rule exists.

The dual-side HTTP tests open a real loopback HTTP server, verify token-bound
RED/BLUE scenes, request a multi-cell server proposal, stage by action ID,
submit both batches, and observe exactly one fact advance.  Node.js and a
browser runtime were not installed on the acceptance host; the two existing
runtime JS tests therefore remained skipped and no screenshot was fabricated.

## Historical five-sample evidence

The bounded acceptance re-ran only the five named sample archives.  All five
converted, loaded in the Viewer, aligned sampled frame hashes, and left Replay,
map and Scenario source hashes unchanged.  Four unique map conversions yielded
10,000 cells (123), 4,200 cells (221), and 7,084 cells each (96 and 9601).
Maps 96/9601 retain warnings for 43 asymmetric road and 241 asymmetric river
edges instead of silently repairing source facts.

Map conversion is adjudication-capable; old Scenario conversion is not claimed.
The old documents omit complete Native direct-fire weapon profiles and explicit
Native deployment/terminal semantics.  They are presentation/replay inputs
until curated rule evidence is supplied.

## 100-operator measurement

The current single-run 50-vs-50 measurement produced 337 frozen actions per
side.  A 95-segment server route took about 996ms to plan/register; proposal
lookup took below 1ms.  The RenderScene JSON was about 741KB.  A fully managed
Recorder+Analyzer frame took about 2.03s and visibility remained about 47% of
profiled Python internal time.  This proves the interface works at 100
operators; it does not prove a 1Hz SLA.  No speculative cache was added.

## Reproduction

```bash
PYTHONDONTWRITEBYTECODE=1 pytest -p no:cacheprovider \
  tests/test_product_loop.py tests/test_dual_side_control.py -q

PYTHONPATH=src python -m scripts.run_decision_window_acceptance \
  --replay-root <five-sample-replay-root> \
  --snapshot-root <five-sample-sdk-snapshot> \
  --output-root /tmp/product-five-sample-acceptance

PYTHONPATH=src python -m scripts.measure_decision_window_scaling \
  --map-config <map-221-basic.json> \
  --operator-count 100 \
  --output /tmp/product-100-operator.json
```

The detailed machine evidence is in
`docs/verification/product_facade_acceptance.json`.
