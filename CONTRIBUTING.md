# Contributing

## Setup

```bash
python -m pip install -e ".[dev]"
```

Run the fast release gate:

```bash
PYTHONDONTWRITEBYTECODE=1 pytest -p no:cacheprovider \
  tests/test_joint_tick_semantics.py \
  tests/test_source_release_example.py \
  tests/test_source_perspectives.py \
  tests/test_replay.py \
  tests/test_random_product_seat_agent.py -q
```

Run all Python tests before submitting a rule, Replay or orchestration change:

```bash
PYTHONDONTWRITEBYTECODE=1 pytest -p no:cacheprovider -q
git diff --check
```

Node.js is optional for Engine development but required to execute the static
browser JavaScript contract tests rather than skip them.

## Non-negotiable boundaries

Follow `AGENTS.md` and `docs/architecture/source_release.md`. In particular:

* keep one authoritative `WorldState` and route formal advances through
  `GameSession -> NativeEngine`;
* never expose GREEN, mutable Engine objects or hidden opponent facts to a side;
* preserve same-pre-state joint adjudication and transactional tick rollback;
* use `NativeRNG` and canonical ordering for deterministic content;
* do not add an Agent-owned pathfinder, visibility rule or damage rule;
* do not add old-project runtime imports, private data, credentials or binaries;
* keep generated runs in `/tmp` or ignored `local_data/` paths.

Every rule-semantic change needs a focused regression, deterministic replay
coverage, and an explicit adjudication/replay compatibility decision. Agent
protocol changes require side-isolation and detached-input tests.
