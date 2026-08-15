"""Stable product-facing data boundary."""

from wargame_native_engine.data.catalog import ScenarioCatalog
from wargame_native_engine.data.models import (
    DataLoadError,
    DataProvenance,
    MapIdentity,
    ScenarioBundle,
    ScenarioIdentity,
)
from wargame_native_engine.data.map_runtime import (
    MAP_RUNTIME_SCHEMA,
    MANIFEST_FILENAME as MAP_RUNTIME_MANIFEST_FILENAME,
    MapRuntimeData,
    MapRuntimeDataError,
    MovementCostLookup,
    PublishedMapRuntimeData,
    StaticVisibilityLookup,
    publish_map_runtime_data,
)
from wargame_native_engine.data.source_scenario import (
    EXPLICIT_NATIVE_SUPPLEMENT,
    STRICT_SOURCE,
    PublishedSourceScenarioConversion,
    SourceScenarioConversion,
    SourceScenarioConversionError,
    convert_source_scenario,
    inspect_source_scenario,
    publish_source_scenario_conversion,
)

__all__ = [
    "DataLoadError",
    "DataProvenance",
    "MapIdentity",
    "ScenarioBundle",
    "ScenarioCatalog",
    "ScenarioIdentity",
    "MAP_RUNTIME_SCHEMA",
    "MAP_RUNTIME_MANIFEST_FILENAME",
    "MapRuntimeData",
    "MapRuntimeDataError",
    "MovementCostLookup",
    "PublishedMapRuntimeData",
    "StaticVisibilityLookup",
    "publish_map_runtime_data",
    "EXPLICIT_NATIVE_SUPPLEMENT",
    "STRICT_SOURCE",
    "PublishedSourceScenarioConversion",
    "SourceScenarioConversion",
    "SourceScenarioConversionError",
    "convert_source_scenario",
    "inspect_source_scenario",
    "publish_source_scenario_conversion",
]
