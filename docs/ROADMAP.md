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

## 0.4.0 implemented

- hash-checked standard-frame import/export;
- odd-r elevation LOS and named observation profiles;
- final curated JSON as the sole direct-fire table source;
- ordered legality checks with reason codes and evidence;
- discrete personnel, vehicle, and infantry-light-versus-vehicle resolution;
- independent deterministic base/correction dice and lookup evidence;
- suppression timers, passenger losses, ammunition, and cooldown;
- first-arrival scheduling primitives and strict direct-fire replay.

Indirect fire, anti-air, mines, electronic warfare, complex pathfinding, and
PyQt integration remain outside 0.4.0.

## Phase 4A.2 implemented

- ordered Move, Shoot, StopMove and transition dispatch in the existing step;
- explicit adjacent paths and center-to-center segment progression;
- named vehicle terrain, discrete slope and reciprocal road-edge timing;
- absolute road marching speed and 75-second transitions;
- personnel height, charge, fatigue and suppression-stop rules;
- deterministic multi-segment delta-time and dynamic stack rechecks;
- arrival-driven observation/valid-action refresh and Source movement fields;
- movement state/evidence in snapshots, state hashes and strict Native Replay.

Full frame recording, replay.zip, external human/Agent gateways,
HTTP/WebSocket, indirect fire, mines and complex automatic pathfinding remain
outside Phase 4A.2.

## Phase 4A.3 implemented

- formal RED `0`, BLUE `1`, GREEN `-1` Source perspectives;
- safe visible-enemy reconstruction and recursive relationship cleanup;
- isolated valid actions, actions, judge information and auxiliary fields;
- GREEN-only numbered-frame Recorder with atomic deterministic output;
- stable replay.zip packaging, manifest hashes and tamper validation;
- one reader for historical and Native numbered-frame archives;
- strict step replay and a movement/visibility/direct-fire acceptance run;
- Recorder-on/off trace and result equivalence checks.

CommandGateway, human control, Agent/API external control, HTTP, WebSocket,
PyQt, last-known-position memory, observable enemy actions, anonymous incoming
fire, indirect fire, mines and complex pathfinding remain outside Phase 4A.3.
