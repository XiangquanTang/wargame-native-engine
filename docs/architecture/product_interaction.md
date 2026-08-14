# Product interaction contract

## Scope

The loopback RED/BLUE page is a human-facing composition over the existing
Native simplified rules. It does not replace the operation, gateway, scheduler
or engine layers. The only advance chain remains:

```text
product execute
  -> SideController stage/submit
  -> CommandGateway
  -> DecisionWindow / SimulationScheduler
  -> GameSession.step
  -> NativeEngine
```

The ordinary page deliberately does not present frozen action identifiers,
route-proposal identifiers, internal window lifecycle values, batch ordering or
engine action JSON. Those contracts remain available to Python integrations and
low-level compatibility endpoints.

## Human workflow

```text
select one or more friendly units
  -> choose Move / Fire / Stop / weapon state / Wait
  -> choose a destination hex or visible target when required
  -> inspect per-unit server results
  -> explicitly confirm a valid subset when some units are rejected
  -> press Execute once
  -> wait for the other side
  -> receive the next side-safe map scene
```

Single click replaces the selection. Ctrl/Cmd-click toggles one friendly unit;
Shift-click adds one. Empty-map click clears selection unless the page is
selecting a move destination. Escape cancels the active operation, then clears
selection when no operation is active. Enemy and destroyed units cannot enter
the friendly selection set. Stack spreading in the shared renderer keeps
co-located units independently clickable.

## Fixed operation toolbar

The toolbar is always present. Applicability counts are returned by
`ProductOperationService.capabilities()` after querying the existing
`UnitCommandMenu` for each selected unit. The browser does not infer
availability from unit type.

* Move sends only a destination intent. The server invokes the existing
  deterministic `MovementPlanningService` independently per unit and returns
  complete adjacent paths plus authoritative durations.
* Fire sends only a currently visible target identity. Every shooter is matched
  against its frozen SHOOT actions. Multiple valid weapons require an explicit
  per-unit choice.
* Stop, unfold, lock and selected-unit wait resolve only current frozen actions.
* Wait with no selected unit is an explicitly confirmed whole-side empty
  submission, not a fabricated engine action.

## Group preview

`ProductOperationService` returns a detached `GroupOperationPreview` with an
opaque token, aggregate counts and per-unit results. Public results contain
paths, durations, weapon summaries and human-readable rejection reasons; they
do not expose the action identities stored in the private registry.

Preview is observer-only. It cannot advance the environment or write a fact
frame. Rejected intentions are recorded separately as control audit. A newer
preview involving the same actor supersedes an older unexecuted preview.
Tokens are scoped to one service/session/side and current decision cycle and
expire when that cycle changes.

When a group is partly valid, execution is disabled until the operator confirms
that only the valid subset will run. Zero-valid previews cannot execute.

## One-click execution and retries

The product execute endpoint performs the existing stage and submit operations
inside one `ProductOperationService` lock. It resolves server-owned detached
actions, clears prior staging, stages at most one action per actor, then calls
the existing side submission once. It does not call the environment directly.

An execute request has one stable request identity. Repeating the same request
and token returns the first result; using that request identity with another
token fails. A successfully executed token cannot be used by another request.
The page disables the Execute button while the request is in flight.

## Product status

The normal page maps service state to four labels:

| Product label | Meaning |
| --- | --- |
| Waiting for selection | this side may operate |
| Waiting for opponent | this side has submitted |
| Advancing | a submitted request is being processed |
| Terminal | Native termination has ended the match |

The page polls server state; it does not use client wall time to decide whether
an operation was accepted. A frame change invalidates the local preview while
retaining still-visible, still-controllable selected units.

## Side and evidence boundaries

The product bootstrap response contains only a RED or BLUE `RenderScene`, a
small product status and public session identity. Its frame is reduced to time,
cities and the side-safe viewer units required by the map. It does not return
GREEN, FrameAnalysis, raw frozen actions, opponent actions or window internals.

Invalid unit operations never reach `GameSession.step`, numbered GREEN frames
or `steps.jsonl`. Valid subset execution sends only the accepted frozen actions.
Rejected preview details may appear in `control_audit.native.jsonl`, which stays
outside replay fact hashes. Replay, strict replay and analysis formats are
unchanged.

## HTTP product surface

The normal page uses only:

```text
GET  /api/product/bootstrap
GET  /api/product/status
POST /api/product/capabilities
POST /api/product/preview
GET  /api/product/preview
POST /api/product/execute
```

The existing low-level scene, command, proposal, stage and submit routes remain
for compatibility and tests. They are not called by `dual_control.js`.

## Known limits

* The server is loopback-only and seat tokens are not internet-grade
  authentication.
* The page uses polling rather than WebSocket push.
* No client-side pathfinder, LOS calculation, fire adjudication or animation is
  introduced.
* A synchronous second-side submission may complete the scheduler before the
  opposite browser observes a persistent Advancing state; both pages still
  converge on the next server state.
* Browser and Node runtime evidence depends on those runtimes being available;
  Python HTTP and static-contract tests remain authoritative when absent.
