from __future__ import annotations

import argparse
import json
from pathlib import Path
import subprocess
from typing import Any

from wargame_native_engine.agents import (
    FirstValidActionAgent, RandomLegalAgent,
)
from wargame_native_engine.backends.native import NativeEngineBackend
from wargame_native_engine.io.scenario import load_scenario
from wargame_native_engine.rules.config import default_ruleset_path, load_ruleset
from wargame_native_engine.session.episode import EpisodeRunner
from wargame_native_engine.session.game_session import GameSession


def parser() -> argparse.ArgumentParser:
    value = argparse.ArgumentParser(
        description="Run one game using Native simplified rules")
    value.add_argument("--scenario", required=True, type=Path)
    value.add_argument("--ruleset", type=Path, default=default_ruleset_path())
    value.add_argument("--red-agent", default="first-valid",
                       choices=("first-valid", "random-legal"))
    value.add_argument("--blue-agent", default="first-valid",
                       choices=("first-valid", "random-legal"))
    value.add_argument("--seed", type=int, default=42)
    value.add_argument("--max-ticks", type=int)
    value.add_argument("--output", type=Path,
                       default=Path("local_data/runs/minimal_demo"))
    return value


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        if args.max_ticks is not None and args.max_ticks < 1:
            raise ValueError("max-ticks must be positive")
        _validate_output(args.output)
        scenario = load_scenario(args.scenario)
        ruleset = load_ruleset(args.ruleset)
        session = GameSession(NativeEngineBackend(
            scenario, max_ticks=args.max_ticks, ruleset=ruleset))
        agents = {
            "red": _agent(args.red_agent, args.seed),
            "blue": _agent(args.blue_agent, args.seed + 1),
        }
        if set(agents) != set(scenario.side_order):
            raise ValueError("CLI currently requires scenario sides red and blue")
        outcome = EpisodeRunner(
            session, agents=agents, output_dir=args.output,
            project_commit=_git_commit(),
        ).run(args.seed)
        print(json.dumps(outcome.to_dict(), ensure_ascii=False,
                         sort_keys=True, indent=2))
        result = outcome.result
        return 0 if result.get("terminal") and not result.get("truncated") else 3
    except Exception as exc:
        print(json.dumps({"status": "failed", "error_type": type(exc).__name__,
                          "message": str(exc)}, ensure_ascii=False,
                         sort_keys=True, indent=2))
        return 2


def _agent(name: str, seed: int) -> Any:
    if name == "first-valid":
        return FirstValidActionAgent()
    if name == "random-legal":
        return RandomLegalAgent(seed)
    raise ValueError(f"unknown agent: {name}")


def _validate_output(path: Path) -> None:
    resolved = path.expanduser().resolve()
    if "local_data" not in resolved.parts and not str(resolved).startswith("/tmp/"):
        raise ValueError("output must be under local_data or /tmp")


def _git_commit() -> str | None:
    try:
        completed = subprocess.run(
            ["git", "rev-parse", "HEAD"], check=True, capture_output=True,
            text=True, timeout=2.0,
        )
        return completed.stdout.strip() or None
    except (OSError, subprocess.SubprocessError):
        return None


if __name__ == "__main__":
    raise SystemExit(main())
