# objective_control_basic

This demo records the existing passive objective-control rule; it does not add
a CAPTURE action.

1. RED starts alone inside objective `1` and gains control/score.
2. RED leaves while BLUE moves in, transferring control to BLUE.
3. RED returns while BLUE remains, producing the current contested semantic:
   `controlling_side = null` and no side receives the contested tick's score.

All movement is submitted through `RealtimeAgentSession` and advanced by the
normal long-running movement runtime.

```bash
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src \
python scripts/run_demo.py objective_control_basic --overwrite
```
