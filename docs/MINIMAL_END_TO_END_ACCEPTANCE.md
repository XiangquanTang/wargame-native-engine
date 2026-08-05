# Minimal Source replay acceptance

Run the committed deterministic acceptance script:

```bash
python scripts/run_minimal_source_replay.py \
  --output local_data/acceptance/source_perspectives_smoke \
  --seed 42
```

The synthetic `source_movement_scenario.json` has one red vehicle and one blue
personnel unit on the explicit six-cell map. RED and BLUE controllers receive
only their own projected frame. The referee combines their actions and is the
only object that holds GREEN and the Recorder.

```text
setup_views -> record GREEN 0.json
RED movement + BLUE waits -> center arrivals -> visibility change
RED direct fire -> judge_info -> side elimination
finalize -> replay.zip -> Source archive import -> historical import
-> strict Native step replay -> final hash comparison
```

The script reruns the same episode without a Recorder and compares the control
trace and final result. For seed 42 it requires three perspectives, movement,
an arrived-hex change, a visibility change, legal direct fire, GREEN
`judge_info`, natural `side_eliminated`, zero import warnings, equal strict
replay hashes, and equal Recorder-on/off results.

Exact frame counts and hashes are written to `acceptance_summary.json`; they
must only be quoted after running the command. Generated files remain under
`local_data/`.

