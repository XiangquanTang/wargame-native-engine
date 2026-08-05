# Architecture

The implemented dependency direction is:

```text
JSON rule/profile files -> immutable Ruleset
legacy JSON -> ScenarioImporter -> NativeScenario
parsed historical evidence -> HistoricalReplayImporter -> HistoricalReplay
HistoricalReplay initial frame -> HistoricalScenarioBuilder -> NativeScenario
HistoricalReplay + Native trajectory -> ReplayAligner -> component metrics
calibration split + bounded parameter grid -> candidate -> held-out validation
Ruleset + NativeScenario -> NativeEngine -> authoritative WorldState
                                      |-> detached SideObservation -> Agent
                                      |-> EngineAction -> systems -> EngineEvent
                                      |-> Snapshot / Replay -> strict verification
```

`WorldState` is the sole authoritative mutable state. Agents receive detached
side observations and current canonical valid actions. They never receive the
world object, hidden opponent state, or another side's observation.

`NativeEngine` loads and validates a `Ruleset` before reset. The frozen object
is retained for the episode. Systems receive the relevant movement,
visibility, combat or terminal rule object rather than embedding tolerances,
ranges, damage or max-tick constants.

## Synchronous resolution

Every side's action is validated against the same pre-tick valid-action set.
Accepted intents are canonically sorted. Deployment, state and straight-line
movement effects are applied, then every attack committed as legal for that
tick contributes to an aggregate damage table. Damage is applied by sorted
target ID, so an operator destroyed during that damage pass still executes its
already committed attack. Terminal evaluation follows damage and objective
scoring.

Events use a stable `(tick, event priority, side, actor, target, event hash)`
order. Mapping insertion order is not part of adjudication. This is the
documented Native simplified policy, not a claim about an external engine.

`GameSession` owns lifecycle and delegates rules to one backend. The single-game
CLI, migration CLI, fairness CLI, Replay and local Agent tools all use these
same boundaries.

Historical import is a data boundary, not a runtime dependency on the old
project. Action-level evidence may enter `HistoricalReplayDriver`; state,
event, and summary evidence can only enter their corresponding aligners.
Candidate search is deterministic and completes before validation replays are
loaded. Candidate provenance references, but never changes, the parent ruleset.
