from __future__ import annotations

from pathlib import Path

import pytest

from wargame_native_engine.core.action import ActionType, ValidAction
from wargame_native_engine.core.engine import NativeEngine
from wargame_native_engine.io.scenario import NativeScenario, load_scenario


ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def scenario_path() -> Path:
    return ROOT / "examples" / "minimal_scenario.json"


@pytest.fixture
def scenario(scenario_path: Path) -> NativeScenario:
    return load_scenario(scenario_path)


def action_of(engine: NativeEngine, side: str,
              action_type: ActionType) -> ValidAction:
    return next(item for item in engine.valid_actions(side)
                if item.action.action_type is action_type)


def advance_to_combat(engine: NativeEngine) -> None:
    engine.step({side: action_of(engine, side, ActionType.DEPLOY).action
                 for side in engine.scenario.side_order})
    engine.step({side: action_of(engine, side, ActionType.END_DEPLOYMENT).action
                 for side in engine.scenario.side_order})
