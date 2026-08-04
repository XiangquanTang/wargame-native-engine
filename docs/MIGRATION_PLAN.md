# Migration plan

This project is a clean, independent implementation. It does not copy or call
the official SDK, bypass authentication, or claim equivalent adjudication.
Every rule implemented here is labelled **Native simplified rules**.

Source snapshot used for planning:

- repository: `/home/txq/wargame/wargame_benchmark_lab`
- branch: `feature/full-game-agent-interface`
- commit: `6288919fb8ff9f68e59f93117daab453e4ed5aba`
- worktree at inspection: clean

## Directly migrate

These symbols are wholly project-authored and have no rule-engine dependency.
They are reimplemented in this repository rather than imported at runtime.

| Old file and symbol | New file | Migration | SDK removal |
|---|---|---|---|
| `session/agent.py`: `Agent` protocol | `agents/protocol.py` | Preserve reset/act/close boundary and immutable contexts | Replace SDK seat/tool types with native sides and `EngineAction` |
| `session/agent.py`: `FirstValidActionAgent` | `agents/first_valid.py` | Preserve canonical deterministic selection | Remove SDK source/type filters; use native priority and action ID |
| `session/agent.py`: `RandomLegalAgent` | `agents/random_legal.py` | Preserve explicit seeded RNG and canonical pre-sort | Remove SDK bindings |
| `session/models.py`: result and step-result concepts | `core/result.py`, `session/episode.py` | Retain terminal/truncated distinction and serializable records | Replace SDK stage/evidence fields with native rule fields |
| `cases/serialization.py`: canonical JSON/hash pattern | `io/serialization.py` | Clean implementation of stable JSON, cloning and SHA-256 | None |
| `recording/session_replay.py`: manifest and JSONL pattern | `io/replay.py` | Keep checksummed collections and action/event separation | Remove SDK replay and evaluator streams |
| `runs/models.py`: deterministic `run_id` pattern | `session/episode.py` | Content-address run identity from scenario/rules/agents/seed | Remove benchmark and SDK backend identity |
| `session/local_tools.py`: local tool boundary | `session/local_tools.py` | Provide status/observation/actions/submit/result calls | Use `GameSession` only; no SDK registry |
| Generic fixture ideas | `tests/` | Small deterministic fixtures and serialization assertions | Native scenario only |

## Migrate after removing SDK dependencies

| Old file and symbol | New file | Change required |
|---|---|---|
| `session/game_session.py`: `GameSession` public lifecycle | `session/game_session.py` | Keep orchestration API; delegate only to `NativeEngineBackend` |
| `session/episode.py`: bounded full-game loop | `session/episode.py` | Replace SDK progress/error semantics with native synchronous ticks |
| `session/run_game.py`: CLI layout | `cli/run_game.py` | Remove SDK path/version/see-data/auth options; accept native scenario |
| UI immutable view-model approach | `core/observation.py`, `core/event.py` | Publish detached side observations/events without PyQt dependency |
| Run manifest seed/version/commit fields | `io/replay.py` | Record native engine and ruleset versions only |

## Rewrite completely

The following old responsibilities are SDK-bound and are not copied:

- `SDKEngineBackend` and the `TrainEnv` lifecycle become `NativeEngine` and
  `NativeEngineBackend`.
- Official action compiler, dynamic SDK bindings and payload validation become
  an explicit `EngineAction` model plus transparent native validators.
- SDK raw observation parsing becomes `ObservationBuilder` over `WorldState`.
- SDK terminal, Action Echo and result interpretation become native terminal
  rules and native adjudication events.
- `.engine_config`, authorization, SDK discovery, worker processes, SDK replay
  export and evaluator/GREEN handling have no counterpart in this repository.
- World state, deployment, movement, visibility, combat, damage, snapshots and
  replay execution are original native implementations.

## Format reference only

The following may inform adapters, never internal adjudication:

- operator field names such as `obj_id`, `cur_pos`, `blood`, `basic_speed` and
  weapon identifier arrays;
- action numbers 1 (move), 2 (shoot), 6 (change state), deployment actions and
  333 (end deployment);
- scenario, observation and replay envelope organization;
- red/blue information separation and the principle that hidden global state
  is not an Agent observation.

The native canonical model uses descriptive string action types, metric XY
coordinates and explicit units. Compatibility mappings retain the original
value in metadata. Unknown, ambiguous, documented-only and unresolved fields
remain opaque; they do not become inferred rules.

## Deferred from version 0.1

- high-fidelity or official-equivalent combat adjudication;
- complete weapon and ammunition systems;
- complex line of sight, elevation occlusion and concealment;
- road-network or terrain-cost pathfinding;
- electronic warfare and communications networks;
- full logistics and replenishment;
- SDK 5.0.0 compatibility;
- WeaponLock and any unresolved official action;
- hidden official rules or undocumented payload behavior;
- large scenario/data imports and C++ optimization;
- PyQt application and benchmark leaderboard implementation.

## Minimal delivery scope

Version 0.1 loads one small native scenario, runs deployment, bounded straight-
line movement, radius visibility, deterministic attacks and explicit terminal
rules. `FirstValidActionAgent` versus itself must reach a natural terminal
state, produce a checksummed replay and reproduce the same result from the same
scenario, actions, ruleset and seed.
