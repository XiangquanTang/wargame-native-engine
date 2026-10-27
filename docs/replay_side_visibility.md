# Replay side views: recorded visibility, not a color mask

The default **全局 / GREEN** view and all existing default JSON responses are
unchanged. **红方 / RED** and **蓝方 / BLUE** show every current friendly unit
(including passengers) plus only enemies in that side's current recorded
visibility set. They are not “show only red/blue units” filters.

## Evidence and compatibility

Existing Native Source 1.1/1.2 recordings already contain the needed visibility
membership evidence. `NativeEngine._refresh_source_observation` overwrites each
unit's `see_enemy_bop_ids` after reset/restore/steps. Dead or undeployed observers
produce empty lists; dead or undeployed targets are excluded. Its
`_visible_enemies` method uses the union across **all** friendly records.
`SourceFrameExporter` records these arrays unchanged. The viewer reads that
same union from the current frame's operators and passengers, never prior
frames, distances, GREEN unit counts, or a newly executed simulation.

This interpretation is enabled only for a validated Native archive with Source
1.1/1.2 and recognized Source movement and perspective-policy provenance in both
manifest and frames. Generic/nonhex or older historical recordings cannot
establish those semantics. Missing, null, malformed, foreign-side, nonexistent,
or contradictory visibility references make the relevant side unavailable;
missing evidence is never silently interpreted as “sees nothing.” An affected
side is unavailable for that replay, while its GREEN view remains usable.

The UI labels side views as **derived from recorded visibility**, not original
complete RED/BLUE frames. No new recording format, duplicate side archives,
sidecar, engine refresh, training change, or write to the user's replay is
introduced. Validation stays in the existing single streaming frame pass.

## Conservative contact fields

- Friendly records use the live projector's relation cleaning, removing hidden
  IDs and GREEN-only native evidence. Own passengers retain their recorded
  collection and position.
- A visible enemy contributes only ID, opposite side, recorded hex position,
  altitude and alive status. Health, ammunition, movement path/progress, boarding,
  readiness, suppression, native facts and eight-factor analysis stay unknown.
  Enemy contacts do not inherit membership from GREEN's passenger ledger.
- GREEN template names and type codes are not guaranteed to equal Native live
  contact identity. They never supply enemy classification. A narrowly gated
  optional path uses the explicitly loaded Native scenario's public identity:
  the adjacent `native-realtime-match-summary/1.1` must report an exact boolean
  strict replay match and bind the exact ZIP hash, scenario hash/ID, seed and
  final state hash, under known adjudication semantics 1.6. Only currently
  visible IDs whose side matches the scenario receive
  `SCENARIO_DERIVED_IDENTITY`. This is publication-reported verification, not
  verification newly performed by the viewer or original observed identity.
  Without that complete binding, or for new/unmatched IDs, the enemy remains a
  generic unknown contact. A scenario hash alone is insufficient because an
  arbitrary restored snapshot can change subtype. The private identity lookup
  is never added to the map API or exposed as an enemy roster.
- Derived views do not claim complete side-filtered action/judge evidence. Their
  action and judge arrays are empty and side analysis is null. The UI explicitly
  says global event/audit details are not shown in a side view.
- Own factor details remain available from an explicitly scoped
  `own_unit_analyses` list filtered by both currently owned IDs and analysis
  side. They are labeled global-model-derived own factors, not original side
  analysis. Enemy factors remain unknown and cannot join the global cache.
- Global curves and any already-loaded same-frame global summaries remain
  explicitly labeled **全局**. They are not that side's intelligence. Selecting a
  visible enemy never joins its ID to GREEN eight-factor details.

## HTTP and browser behavior

`/api/frame/{index}` and `/api/frame-bundle/{index}` accept an optional
`perspective=RED` or `perspective=BLUE`. Omitted perspective or `GREEN` preserves
the original bytes and cache key. Side bundles return `frame`, `analysis: null`,
`own_unit_analyses` and `own_analysis_source: GLOBAL_MODEL_DERIVED_OWN_FACTORS`;
`/api/analysis/{index}?perspective=RED|BLUE` returns HTTP 409 rather than global
analysis. `/api/perspectives` reports availability and an explanatory reason.
Malformed perspectives return 400 and unavailable evidence returns 409.

Both response and browser cache keys contain perspective. View changes abort
obsolete requests, discard late successes/failures before caching/rendering,
clear selection/hover and clear dynamic units while awaiting the new frame.
Missing evidence, wrong response perspective and unknown projection provenance
fail closed with an empty dynamic view, never a GREEN fallback. Restart the
viewer after replacing its code; mixed old-server/new-static responses are
explicitly rejected by the frontend perspective check.

Regression coverage: `checks/test_replay_visibility.py` and
`checks/replay_timeline_ui.cjs`, alongside existing replay loading, map,
proxy-prefix and archive integrity checks.
