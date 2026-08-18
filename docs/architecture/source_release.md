# GitHub source-release architecture

This repository is a source distribution of a **Native simplified rules**
engine. It does not claim official-SDK equivalence and does not require the old
benchmark repository at runtime.

## Runtime composition

```text
Human Web ---------+
Random/Rule Agent -+
RL adapter --------+--> SeatDecision / product operation
External controller+              |
                                  v
                CommandGateway / RealtimeAgentSession
                                  |
                                  v
                     GameSession / NativeEngine
                                  |
             same-pre-state joint adjudication
                     + atomic tick commit
                                  |
                    +-------------+-------------+
                    v             v             v
                  RED           BLUE          GREEN
                                                |
                                  +-------------+-------------+
                                  v                           v
                               Replay                    FrameAnalysis
```

The Web, Agent, training and CLI paths are adapters. They do not implement a
second movement, visibility, direct-fire or termination system.

## Architecture invariants

1. There is one authoritative `WorldState`.
2. Controllers never mutate `WorldState` directly.
3. Human and Agent inputs are controller variants over the same product model.
4. Manual and realtime scheduling share the same Engine core.
5. Command arrival may be asynchronous; adjudication is not arrival-ordered.
6. Commands sealed for one tick are qualified against the same pre-state.
7. Canonical ordering supplies determinism, not hidden first-mover advantage.
8. A failed core tick cannot partially commit World, RNG, time or sequences.
9. Long-running actions are advanced by the Engine without repeated commands.
10. RED and BLUE are detached constrained observations.
11. GREEN is complete referee evidence and is never a side observation.
12. Strict replay re-executes the repository's official Native rule path.
13. Analysis, Viewer and Renderer are non-authoritative observers.
14. A rule semantic change requires replay-semantics compatibility review.

## Public and trusted surfaces

Recommended application entry points are:

* `WargameFacade` and `MatchHandle` for product matches;
* `SeatDecision`, `LocalSeatAgent` and `RealtimeAgentSession` for controllers;
* `NativeMultiSeatEnv` for synchronous deterministic training integration;
* `replay_source_archive(..., strict=True)` for deterministic verification;
* the scripts and examples documented in the root README.

`NativeEngine`, mutable `WorldState`, `GameSession.step`, raw `EngineAction`,
restore operations and `SessionManager.advance_ms` are trusted/advanced
boundaries. They remain available for internal composition and tests, but are
not the untrusted-client integration surface.

## Evidence package

Realtime publication produces one atomic directory:

| Artifact | Meaning |
| --- | --- |
| `replay.zip` | Numbered GREEN facts, accepted ordered actions and replay manifest |
| `frame_analysis.native.jsonl` | Non-authoritative per-frame analysis sidecar |
| `seat_commands.native.jsonl` | Server-labelled controller input lifecycle |
| `realtime_ticks.native.jsonl` | Authoritative tick/frame association |
| `control_audit.native.jsonl` | Product/Gateway rejection and acceptance audit |
| `match_summary.native.json` | Counts, identities and terminal/truncated result |

Generated evidence belongs under `/tmp` or ignored local output directories; it
is never source data.

## Source-release scope

The first release targets `git clone` plus editable installation. It is not a
promise of a stable PyPI SDK, HLA/DIS compatibility, proprietary adjudication
equivalence, distributed hard realtime, or bitwise identity across every
Python/CPU platform.
