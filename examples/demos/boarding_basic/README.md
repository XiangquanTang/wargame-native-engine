# boarding_basic

This repository-authored Native simplified scenario prepares the minimal
single-passenger ground boarding contract. RED personnel `101` and explicitly
authorized carrier `102` start stopped in the same hex.

The registered runner validates Engine, Human and Agent capability projection,
immediate `ON_BOARD` state, the 75-second lock/timer, UNLOAD, formal lifecycle
events, refreshed actions and strict replay. The scenario contains no
demo-specific Engine logic.

```bash
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src \
python scripts/run_demo.py boarding_basic \
  --output-dir=/tmp/wargame-native-demo-boarding \
  --overwrite
```
