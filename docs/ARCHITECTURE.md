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

## Joint resolution and transactional ticks

Every action in a sealed RED/BLUE batch is validated against the same
authoritative pre-tick state. Accepted intents are staged and canonically
resolved. Direct-fire effects contribute to an aggregate target table before
losses are applied, so canonical ordering cannot create hidden initiative.
The configured simultaneous-damage and destroyed-actor policies determine
whether an already-qualified action executes.

The Engine evolves an isolated working state through action effects, ongoing
movement, visibility and termination. Only a successful complete tick commits
the working World, NativeRNG state, logical time and sequences. A failure at
any core stage restores the exact tick-start snapshot and produces no GREEN
fact frame.

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

## Source-compatible adjudication path

```text
source frame -> SourceFrameImporter -> normalized WorldState
terrain/elevation profile -> LOS + named ObservationProfile
command -> CommandScheduler -> ActionTimer -> EventQueue
validated request -> final JSON direct-fire tables -> discrete mutation
                                             -> strict direct-fire replay
```

This does not replace the synchronous `native_simplified` mode. Realtime
arrival order determines command admission metadata and target Tick, not
combat initiative. Commands sealed into the same Tick use joint same-pre-state
adjudication. Personnel uses base 2D6 plus an independent correction D6;
vehicles use two independent 2D6 rolls. `blood` is authoritative for discrete
loss and compatibility `health` is derived.

## Unified source-compatible movement

Phase 4A.2 keeps movement in the authoritative lifecycle:

```text
controller -> GameSession -> NativeEngineBackend -> NativeEngine.step
           -> ActionRouter -> Movement / DirectFire / transitions
           -> EventQueue -> WorldState -> Observation -> ValidActions
           -> SourceFrameExporter
```

`SourceCompatibleEnvironment` only adapts the resulting world to a frame. It
does not own a second movement loop. Legacy scenarios without a hex grid retain
the existing synchronous native-default path.

## Source perspectives and GREEN recording

```text
WorldState -> existing Visibility/SideObservation
           -> complete SourceFrameExporter GREEN
           -> pure PerspectiveProjector -> RED / BLUE / GREEN
                                      GREEN -> observer-only Recorder
                                            -> numbered frames + steps -> ZIP
```

RED and BLUE never derive visibility or inspect GREEN records. The projector
rebuilds visible enemy records from safe SideObservation fields and cleans
nested ID relations. Only the referee owns GREEN. The Recorder consumes a
detached GREEN serialization and has no engine/backend reference.

The public source-release composition, supported entry points and non-
authoritative adapter boundaries are summarized in
`docs/architecture/source_release.md`.
