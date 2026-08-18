# Public source and data provenance

This file records the release boundary; it is not a grant of rights beyond the
repository `LICENSE`.

## Included public material

* Python source, tests, scripts and browser assets are maintained in this
  repository and contain no vendored SDK package or binary dependency.
* `examples/source_movement_scenario.json`, `minimal_scenario.json`,
  `direct_fire_scenario.json`, `joint_fire_scenario.json` and
  `public_source_movement_map.json` are labelled project-authored synthetic
  fixtures.
* Reduced historical-shaped fixtures explicitly state that original content is
  not included. They are not used by the Quick Start.
* `config/` contains the versioned Native simplified rule, movement,
  perspective and mapping configuration used by the implementation.

## Excluded material

The release does not include Map 123 source files or runtime matrices, private
scenarios, private Replay archives, `.engine_config`, credentials, old project
source, SDK packages/binaries, generated match output, or files from an author
workstation path. Optional acceptance tests locate external read-only inputs
only when `WARGAME_SOURCE_SNAPSHOT_ROOT` or
`WARGAME_SOURCE_REPLAY_ROOT` is explicitly provided.

## Owner confirmation before making the repository public

The repository already declares the MIT License. The repository owner should
still confirm that the two human-readable adjudication guides under
`docs/rules/` and the curated machine table derived for this Native project may
be redistributed under the repository license. The code does not describe
them as official rules and does not read a PDF at runtime.
