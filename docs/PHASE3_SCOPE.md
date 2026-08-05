# Phase 3 scope: historical replay alignment and Native calibration

## Available historical evidence

The old project is read-only input. Its already parsed artifacts can provide
session/replay manifests and hashes, SDK step/time fields, dense evaluator
state frames, red/blue operator identifiers and dynamic fields, issued command
records, Action Echo and receipt/evidence status, discrete session/action
outcome events, episode summaries, and derived generalized-Lanchester advantage
records. Raw replay ZIPs and map/SDK dependencies are not copied.

Evidence is kept distinct:

- explicit, parameter-complete commands with actor/side/time are action-level;
- evaluator frames with position, health, alive and state are state-trajectory
  level;
- attack/damage/destroyed/terminal records are event-level;
- winner, final time/survival and end reason alone are summary-only.

An issued command, Action Echo, or `ActionOutcome(status=UNKNOWN)` is not assumed
to have executed.

## Reliable and unreliable mappings

Stable replay/scenario/map/operator IDs, explicit side, numeric time, direct XY,
health/alive/state, explicit target IDs/positions, terminal flags/winner, and
recorded advantage values can map directly or through a documented unit/time
normalization. Linear cells can map only when grid dimensions and cell size are
declared. Unknown compatibility fields remain source raw data.

Actions missing a target or required payload, ambiguous echoes, SDK-only action
types, hex coordinates without map evidence, hidden adjudication reasons,
communication/electronic-warfare state, complex terrain/path semantics and
opaque scoring factors are unsupported or unmapped. State deltas are never
treated as proof of a unique action.

## Calibratable Native parameters

This phase bounds search to existing transparent parameters: operator-type move
range, documented time scale, sensor radius/tolerance, Native weapon range and
deterministic damage, explicit health normalization, elimination/objective/max
tick and draw behavior. The committed search fixture varies ground-unit move
range and one Native weapon's range/damage. Structural action/event protocols,
synchronous damage and validation data are never search parameters.

## Selected reduced samples

Four mutually exclusive, sanitized fixtures are committed:

- `smoke-state-01`: state trajectory derived from the field shape of an already
  parsed bounded-horizon evaluator run;
- `calibration-action-01` and `calibration-action-02`: small, parameter-complete
  action/state/event trajectories for bounded calibration;
- `validation-action-01`: a distinct action-level trajectory held out until
  candidate selection.

They retain source-format structure and mapping uncertainty but replace names,
IDs, geometry and scale. They are not complete original replays. Their split,
source hashes, evidence levels and inclusion reasons are recorded in
`config/calibration/replay_split_v1.json`.

## Explicit non-goals

Phase 3 does not reconstruct an official combat model, infer missing actions,
copy SDK code/resources, fit unsupported visibility/terrain effects, recover
probabilistic hit logic, or claim SDK equivalence. Output is described only as
**Historical-replay-calibrated native simplified rules**. Four reduced samples
are necessarily `small_sample_calibration`, not general validation.
