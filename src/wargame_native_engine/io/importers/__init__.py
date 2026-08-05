"""Explicit, SDK-independent boundaries for source scenario formats."""

from wargame_native_engine.io.importers.legacy_scenario import (
    LegacyScenarioImporter, MigrationResult,
)

__all__ = ["LegacyScenarioImporter", "MigrationResult"]
