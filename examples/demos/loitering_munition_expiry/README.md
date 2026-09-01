# Loitering Munition Expiry

This companion scenario advances one authoritative 1,200,000 ms interval with
no attack. The finite-lived AIR Operator expires through the normal destruction
fact and strict Replay re-executes the elapsed time.

```bash
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src \
python scripts/run_demo.py loitering_munition_expiry \
  --output-dir=/tmp/wargame-loitering-expiry --overwrite
```
