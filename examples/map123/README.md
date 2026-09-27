# Map123 public Native example

This directory publishes the original compressed Map123 static-visibility
resource and its canonical Native scenario/runtime metadata. It runs under
**Native simplified rules**; it is not an official-rules or SDK-equivalence
claim.

## Install and prepare

From the repository root, install the package (or keep `PYTHONPATH=src`) and
run the no-socket preparation check:

```bash
python -m pip install -e .
python -m scripts.run_map123 --prepare-only --scenario artillery-ammo2
```

The first command using these assets verifies `123see.npz`, extracts its sole
NPY payload without loading the 1 GB array into memory, validates all hashes
and the Map123 identity through the normal runtime loader, then atomically
publishes a cache below `local_data/map123/runtime-cache/`. Allow slightly more
than 1 GB of free disk space. Later commands validate and reuse the same
manifest-addressed cache; the cache is ignored by Git and can be shared by
both included scenarios.

## Start and restart

The default experience uses the explicitly labelled artillery demo, in which
the twelve supported artillery shooters have two rounds each:

```bash
python -m scripts.run_map123 --scenario artillery-ammo2 --port 8003
```

Run the same command again to reuse the validated cache and create a new unique
run namespace/output directory. To use the unmodified converted scenario
instead:

```bash
python -m scripts.run_map123 --scenario original --port 8003
```

The launcher prints the RED and BLUE loopback URLs. Runs are written below
`local_data/map123/runs/` unless `--output-dir` is supplied. `--seed`, `--port`,
`--run-namespace`, and the existing realtime timing/token options are also
available.

## Published files

- `scenario.json` is the original canonical Native scenario, unchanged.
- `scenario-artillery-ammo2.json` is a separate, provenance-labelled demo
  derivative. It does not modify the original scenario or adjudication rules.
- `123see.npz` is the byte-identical compressed source recorded by the
  manifest. SHA-256:
  `019bee4a66cbf8c21ea1c010bc9449706c38d1c1ce57c89e5d9ebc38a9c33bb0`.
- `movement_costs.native.json` and `map_runtime_manifest.native.json` are the
  byte-identical canonical metadata files. The expanded
  `visibility.native.npy` is deliberately not published or tracked.

If preparation reports a missing asset, hash mismatch, identity mismatch, or
invalid existing cache, the realtime session is not created. The preparation
code never replaces an existing cache directory.
