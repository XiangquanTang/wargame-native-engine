# Interactive Combined-arms and Multi-weapon Test

This Native simplified demo places seven RED unit identities at distinct map
positions so they can be inspected through the realtime Human, Viewer and
Agent paths:

- artillery: Source `[2,3]`, weapon 72;
- infantry: Source `[1,2]`, weapons 29 and 35;
- heavy tank: Source `[2,0]`, weapons 43 and 36, plus a boarding profile;
- heavy combat vehicle: Source `[2,1]`, weapons 43, 4, 54 and 73;
- unmanned ground vehicle: Source `[2,4]`, weapons 43, 4 and 74, plus explicit
  weapon-73 guidance affiliation;
- Drone: Source `[3,5]`, unarmed, with the side-wide air-guider profile;
- loitering munition: Source `[3,7]`, weapon 76 and the authoritative limited
  lifecycle profile.

A BLUE vehicle and personnel target supply observable opposition so the Human
direct-fire panel can expose weapon choices with different target domains.
The automated smoke
path deliberately exercises only the Drone's ordinary MOVE and observation
contracts, then verifies strict action-stream Replay. The other four units are
present for interactive capability, symbol and action testing; their presence
does not claim that every family is fired automatically by this demo.

For the interactive realtime Web page, run:

```bash
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src \
python -m scripts.run_realtime_dual_control \
  --scenario-bundle=examples/demos/combined_arms_test/scenario.json \
  --output-dir=/tmp/wargame-combined-arms-live \
  --run-namespace=combined-arms-live-001 \
  --tick-interval-ms=1000 --host=127.0.0.1 --port=8003 --overwrite
```

Run from the repository root:

```bash
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src \
python scripts/run_demo.py combined_arms_test \
  --output-dir=/tmp/wargame-combined-arms-test --overwrite
```
