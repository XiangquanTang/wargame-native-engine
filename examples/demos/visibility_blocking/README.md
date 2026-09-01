# visibility_blocking

Three independent observer/target pairs exercise the existing Native
visibility evaluator:

1. `101 → 103`: open, unobstructed and observable.
2. `201 → 204`: elevation `9` at hex `202` blocks line of sight.
3. `301 → 307`: the target is in FOREST; the terrain modifier halves the
   personnel observation limit, putting the target outside that limit.

The runner writes exact evaluator evidence to `visibility_evidence.json` and
checks it against the detached RED observation. No LOS calculation is copied
into the Demo layer.

```bash
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src \
python scripts/run_demo.py visibility_blocking --overwrite
```
