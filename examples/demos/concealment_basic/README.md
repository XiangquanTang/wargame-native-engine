# Concealment Basic

This project-authored Native simplified scenario validates the explicit
`ENTER_CONCEALMENT` posture action. RED infantry enters concealment immediately,
then a server-planned adjacent `MOVE` exposes it. Both posture changes are
derived from the ordered action stream and verified by strict replay.

```bash
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src \
python scripts/run_demo.py concealment_basic \
  --output-dir=/tmp/wargame-concealment-demo --overwrite
```
