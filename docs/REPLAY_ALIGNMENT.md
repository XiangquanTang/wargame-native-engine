# Historical replay alignment

`ReplayAligner` compares evidence with a Native trajectory at scenario, side,
operator, time, position, event, combat, terminal and advantage-curve levels.
Stable operator IDs are preferred. Configured source mappings are next;
heuristic side/type/initial-position matching is opt-in and visibly labelled.

Time alignment supports `exact`, `nearest`, `fixed_ratio`, and `resampled`.
Every report records source time, Native tick, scale, offset, strategy, and
error; no implicit one-frame-per-tick assumption is made.

Action-level records pass through the normal Native valid-action and step APIs.
Each mapping is `DIRECT`, `NORMALIZED`, `APPROXIMATED`, or `UNSUPPORTED`.
Unsupported actions are reported and either skip the tick or stop the run by
explicit policy. State-, event-, and summary-level evidence never invents
actions and only runs the permitted comparison.

An alignment directory contains manifest, operator/time mapping, trajectory,
event, terminal and advantage metrics, warnings, summary, and (when permitted)
Replay-driven details. Missing comparison events are `not_comparable`, never
numeric zero.
