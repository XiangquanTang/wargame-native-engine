"""Local, read-only replay and eight-factor analysis viewer."""

from wargame_native_engine.viewer.replay_view import (
    ReplayView,
    ReplayViewerLoadError,
    load_replay_view,
)
from wargame_native_engine.viewer.map_config import (
    ViewerMapConfig,
    ViewerMapConfigError,
    load_viewer_map_config,
)
from wargame_native_engine.viewer.server import create_replay_viewer_server
from wargame_native_engine.viewer.live_control import (
    controller_view_record,
    native_scenario_map_record,
)
from wargame_native_engine.viewer.live_server import create_live_control_server
from wargame_native_engine.viewer.operator_registry import (
    OperatorDescriptor,
    OperatorIdentity,
    OperatorRegistry,
    OperatorRegistryError,
    OperatorSymbol,
    WeaponDescriptor,
    load_operator_registry,
    resolve_operator,
)
from wargame_native_engine.viewer.scenario_config import (
    ViewerScenarioConfig,
    ViewerScenarioConfigError,
    load_viewer_scenario_config,
)

__all__ = [
    "ReplayView",
    "ReplayViewerLoadError",
    "OperatorDescriptor",
    "OperatorIdentity",
    "OperatorRegistry",
    "OperatorRegistryError",
    "OperatorSymbol",
    "ViewerMapConfig",
    "ViewerMapConfigError",
    "ViewerScenarioConfig",
    "ViewerScenarioConfigError",
    "WeaponDescriptor",
    "create_replay_viewer_server",
    "create_live_control_server",
    "controller_view_record",
    "load_replay_view",
    "load_operator_registry",
    "load_viewer_map_config",
    "load_viewer_scenario_config",
    "native_scenario_map_record",
    "resolve_operator",
]
