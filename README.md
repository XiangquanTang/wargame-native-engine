# wargame_native_engine

`wargame_native_engine` is a small, transparent and deterministic Python
wargame environment. It is an independent implementation: it does not require
the official SDK, its authentication configuration, its binaries, or the old
benchmark repository.

The current `0.1.0` ruleset is deliberately labelled **Native simplified
rules**. It supports deployment, straight-line movement, radius visibility,
deterministic direct attack, health/destroyed state, elimination/objective/max-
tick endings, per-side Agents, snapshots and a checksummed native replay. It
does not claim official SDK-equivalent adjudication.

## Install and run

```bash
python -m pip install -e .
python -m wargame_native_engine.cli.run_game \
  --scenario examples/minimal_scenario.json \
  --red-agent first-valid \
  --blue-agent first-valid \
  --seed 42 \
  --max-ticks 500 \
  --output local_data/runs/minimal_demo
```

The command exits with zero only for a natural terminal result. A max-tick
ending is represented as `truncated: true`, never as an authoritative winner.

## Python API

```python
from wargame_native_engine.backends.native import NativeEngineBackend
from wargame_native_engine.agents import FirstValidActionAgent
from wargame_native_engine.io.scenario import load_scenario
from wargame_native_engine.session.game_session import GameSession

scenario = load_scenario("examples/minimal_scenario.json")
session = GameSession(NativeEngineBackend(scenario, max_ticks=500))
session.reset(seed=42)
agents = {side: FirstValidActionAgent() for side in scenario.side_order}
while not session.is_terminal():
    batch = {}
    for side in session.current_seats():
        batch[side] = agents[side].act(
            observation=session.observe(side),
            valid_actions=session.valid_actions(side),
            context=session.agent_context(side),
        )
    session.step(batch)
session.close()
```

See `docs/ARCHITECTURE.md`, `docs/DATA_MODEL.md` and
`docs/REPLAY_FORMAT.md` for the stable boundaries and rule scope.
