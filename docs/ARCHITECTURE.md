# Architecture

The engine has one dependency direction:

```text
NativeScenario -> NativeEngine -> WorldState
                         |
                         +-> ObservationBuilder -> SideObservation -> Agent
                         +-> ActionValidator -> systems -> EngineEvent
                         +-> snapshot/result -> GameSession -> CLI/replay/tools
```

`WorldState` is the sole authoritative mutable state. Systems implement bounded
native rules and receive explicit rule configuration. They do not call map or
SDK helpers. `NativeEngine.step()` accepts one synchronous action per active
side, validates every action against the pre-tick valid-action set, resolves
movement before attacks, applies attack damage simultaneously, then evaluates
terminal rules.

`GameSession` owns the public lifecycle but does not duplicate rules. CLI,
Agents, future benchmark code and future UI adapters all consume this session
boundary. `LocalAgentTools` exposes the same boundary without a network server.

## Information boundary

`observe(side)` returns a detached `SideObservation`: all friendly operators,
only sensor-visible enemies, public objectives and legal action identifiers.
Agents never receive `WorldState`, hidden opponents, another side's observation
or future replay frames.

## Resolution order

Within a tick, actions are canonically ordered by scenario side order. Valid
deployment/state/movement actions resolve first. Valid attacks are evaluated
against the pre-damage state and accumulated damage is applied simultaneously.
Terminal evaluation occurs after damage and objective scoring. This is an
explicit native policy, not an assertion about an external engine.
