# Mechanized Assault Basic

This repository-authored deterministic scenario validates composition of
existing Native simplified capabilities. It does not add or override rules.

The RED passenger boards the stopped RED carrier. After the 75-second BOARD
transition, the carrier moves one adjacent hex to the objective, unloads the
passenger, confirms the BLUE contact and executes one explicit direct-fire
action. The published action stream is verified with strict Replay.

```bash
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src \
python scripts/run_demo.py mechanized_assault_basic \
  --output-dir=/tmp/wargame-mechanized-assault-demo \
  --overwrite
```
