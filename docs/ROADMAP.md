# Roadmap and implemented scope

## 0.2.0 implemented

- versioned, validated and hashed Native simplified rule/profile JSON;
- ruleset identity in reset events, results, snapshots and Replay manifests;
- reduced, sanitized 3v3 fixture plus traceable legacy JSON importer;
- side-swap and position-mirror scenario transformations;
- stable FirstValid/action/event ordering and synchronous aggregate damage;
- paired random-Agent fairness evaluation with strict Replay checks;
- deterministic Snapshot continuation and batch seed reproduction tests.

## Explicitly outside 0.2.0

C++ adjudication, official or SDK-equivalent behavior, high-fidelity weapon
families, complex path planning, elevation/occlusion adjudication, bulk legacy
dataset migration, PyQt work, LLM Agents and benchmark rankings are not
implemented. Their absence is not filled by inferred compatibility behavior.

## 0.3.0 implemented

- read-only historical replay import with source hashes and four evidence levels;
- sanitized reduced fixtures with disjoint smoke/calibration/validation split;
- initial-frame-only Native scenario construction and explicit operator/time mapping;
- position, time, event, combat, terminal and optional advantage-curve metrics;
- action-level Replay-driven adjudication without missing-action inference;
- deterministic bounded grid calibration and provenance-bearing candidate output;
- held-out validation, overfitting detection and constrained recommendations;
- Phase 2 fairness, strict Replay and Snapshot regression coverage.

The committed four-replay fixture is a small-sample validation harness. It does
not establish broad fidelity or equivalence to an external engine.
