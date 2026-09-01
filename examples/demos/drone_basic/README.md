# Drone Basic

This Native simplified demo treats Source `[3,5]` as a predeployed, unarmed
AIR Operator. It uses ordinary MOVE, the shared Drone/loitering observation
profile, the existing Human and Agent action contracts, and strict action-stream
Replay.

The Drone has no finite lifecycle, weapons, ammunition, GUIDED FIRE assignment,
launch process, recovery process, or autonomous behavior.

Run from the repository root:

```bash
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src \
python scripts/run_demo.py drone_basic \
  --output-dir=/tmp/wargame-drone-basic --overwrite
```
