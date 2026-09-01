# direct_fire_basic

This project-authored synthetic scenario locks the legacy direct-fire contract:

```text
SHOOT + weapon_id
→ FireIntent(DIRECT)
→ DirectFireRequest
→ DirectFireAdjudicator
→ authoritative damage
→ GREEN evidence
→ strict Replay
```

RED operator `1` can observe and legally shoot BLUE operator `2` with weapon
`36`. The demo does not introduce a fire rule or a demo-only action.

```bash
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src \
python scripts/run_demo.py direct_fire_basic --overwrite
```
