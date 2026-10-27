# Ordinary direct-fire maximum-range policy

## Deliberate game rule change

Adjudication semantics `native-joint-prestate/1.3` changes ordinary ammunition-based
DIRECT shots in the Source-compatible hex game. This is a user-requested Native
game convention, not a correction to the published source rule documents.

- A visible target beyond the weapon's maximum range no longer causes rejection
  solely for range. All other legality checks still apply.
- Such a shot causes no losses, destruction, suppression, suppression refresh or
  passenger damage. It does not consult damage tables or consume random dice.
- Normal ammunition consumption, cooldown and firing exposure still occur.
- Exactly-at-maximum-range and in-range shots use the existing tables unchanged.
- The ordinary direct-fire profile has no minimum-range field; no new lower-bound
  exemption was added. Indirect-fire minimum/maximum-range rules are untouched.
- GUIDED shots, indirect fire, self-as-munition/drone attack policies and the legacy
  coordinate-based ATTACK mode retain their previous range rules. The latter has
  no comparable ammo/cooldown model and is not the Web SHOOT path.

Nonexistent or friendly targets, unobserved targets, invalid map positions,
unsupported target domains, unavailable weapons, exhausted ammunition, cooldown,
movement, transitions and all other legality conditions are not relaxed. A group
may contain both in-range and out-of-range shooters; each accepted shooter's own
resource effect is applied once. Unrelated invalid group members are still
rejected by the authoritative execution checks.

## UI and Agent contract

The red dashed circles represent visible candidate targets, not automatically
selected attackers. The yellow solid circle marks the clicked target. Explicit
Ctrl/Shift friendly multi-selection is preserved. Selecting a weapon or target
does not replace that friendly selection.

Product previews continue to authorize an **intent**, not guarantee a hit or
execution-time legality. Their human-readable summary explains the zero-effect
rule. The frozen legal-action catalog uses the same updated adjudicator as
execution. The direct-fire validation evidence retains `within_weapon_range`
and adds `range_effect`: `NORMAL` or `OUT_OF_RANGE_NO_EFFECT` for ordinary shots.
The zero-effect combat event records `reason_code: OUT_OF_RANGE_NO_EFFECT`, range,
and an all-zero/no-suppression outcome. Training and JEV/LLM consumers should use
execution evidence rather than interpret submission acceptance as damage.

No existing training, SeatOperation or function-calling argument is removed or
renamed. However, legal-action catalogs and training action masks intentionally
expand to include otherwise-legal out-of-range shots. API-shape compatibility is
not action-space equivalence: compare checkpoints, datasets and reported results
only with their matching adjudication semantics.

Internal replayable DirectFireRequest additionally preserves fire_mode,
so a GUIDED request cannot silently become an ordinary DIRECT shot on round-trip.

## Replay and evidence

Snapshots and full replay manifests record `/1.3`; strict restoration/replay
rejects `/1.2` rather than silently apply new rules to old runs. Isolated direct-fire
replays now also record and check that semantics identity. Historical performance
and equivalence files in `benchmarks/` remain evidence for commit `ded41a29` under
`/1.2`, not a new `/1.3` performance claim.

Run `python devtools/run_checks.py` from the repository root for maintained
backend, replay, interface and UI regression checks. UI control-flow tests use
Node.js with a lightweight DOM harness, not a full rendered-browser test.
