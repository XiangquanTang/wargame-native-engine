"""Documented source-frame compatibility without runtime SDK dependencies."""

from wargame_native_engine.compat.source_frame.exporter import SourceFrameExporter
from wargame_native_engine.compat.source_frame.importer import SourceFrameImporter
from wargame_native_engine.compat.source_frame.models import (
    ArmorClass,
    SourceFrame,
    SourceUnitState,
    UnitDomain,
    UnitState,
)

__all__ = [
    "ArmorClass", "SourceFrame", "SourceFrameExporter", "SourceFrameImporter",
    "SourceUnitState", "UnitDomain", "UnitState",
]
