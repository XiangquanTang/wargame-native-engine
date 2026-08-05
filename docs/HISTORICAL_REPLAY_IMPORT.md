# Historical replay import

`HistoricalReplayImporter` reads already parsed replay evidence without
importing the old repository at runtime. A source path is hashed before and
after import; any change aborts the operation. Local output contains an import
manifest, normalized replay, frame/action/event JSONL, summary, unmapped fields,
and warnings.

Evidence levels are assigned in descending order: complete executable actions,
dense state trajectory, discrete events, then summary only. `ACTION_LEVEL`
requires actor, side, time and required target parameters for every action.
An unconfirmed `ACTION_ECHO` is non-executable, so it cannot promote a replay to
action level.

Fields retain three layers: `source_raw`, `normalized_historical`, and
`native_comparable`. Unknown fields are listed and preserved in raw records;
they have no rule effect. Time and coordinate units are mandatory. The reduced
fixture adapter also recognizes the old parsed canonical manifest shape, but
does not load SDK code or treat issued commands as confirmed execution.

The committed four fixtures use sanitized names, IDs, geometry, and scale. They
preserve representative parsed field structure only; no complete original
replay or protected resource is committed.
