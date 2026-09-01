# Loitering Munition Basic

This Native simplified demo treats the loitering munition as a predeployed AIR
Operator. It uses ordinary MOVE, side observation, SHOOT with Source weapon 76,
the existing direct-fire tables, and strict action-stream Replay.

The accepted attack consumes the Operator itself in the same atomic fire
transaction. No ammunition key is created. Launcher release, dynamic creation,
autonomous search, and autonomous attack are outside v1.

Run from the repository root:

```bash
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src \
python scripts/run_demo.py loitering_munition_basic \
  --output-dir=/tmp/wargame-loitering-basic --overwrite
```
