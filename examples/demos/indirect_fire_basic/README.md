# indirect_fire_basic

This deterministic Native simplified demo exercises the complete indirect-fire
pipeline: explicit hex planning, mission flight, scatter, ACTIVE impact area,
one-hit-per-operator resolution, expiry, publication, and strict replay.

```bash
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src \
python scripts/run_demo.py indirect_fire_basic --seed 5 --overwrite
```
