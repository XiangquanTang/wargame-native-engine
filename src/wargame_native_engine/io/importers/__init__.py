"""Explicit, SDK-independent boundaries for source scenario formats."""

from wargame_native_engine.io.importers.legacy_scenario import (
    LegacyScenarioImporter, MigrationResult,
)
from wargame_native_engine.io.importers.historical_replay import (
    EvidenceLevel, HistoricalReplay, HistoricalReplayImporter, MappingStatus,
    load_historical_replay,
)
from wargame_native_engine.io.importers.source_replay_archive import (
    SourceReplayArchive, SourceReplayArchiveImporter,
)

__all__ = [
    "EvidenceLevel", "HistoricalReplay", "HistoricalReplayImporter",
    "LegacyScenarioImporter", "MappingStatus", "MigrationResult",
    "SourceReplayArchive", "SourceReplayArchiveImporter", "load_historical_replay",
]
