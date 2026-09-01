# Native capability benchmarks

`native_capability_benchmark_v1.json` is a data-only traceability registry. It
maps each Native simplified Engine capability to one public Demo and names the
checks and ordered event evidence required for a passing benchmark.

The runner always executes the registered scenario, generates a replay,
strictly re-executes the recorded action stream, and independently re-executes
that same stream to expose deterministic world, RNG and event hashes. It never
restores a recorded final snapshot as validation.

```bash
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src python scripts/run_benchmark.py list
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src python scripts/run_benchmark.py check
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src python scripts/run_benchmark.py run guided_fire_basic
```

Generated reports belong under `/tmp` or `local_data/`; do not commit them.
