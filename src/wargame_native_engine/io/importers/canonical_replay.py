from __future__ import annotations

from pathlib import Path
from typing import Any, Mapping

from wargame_native_engine.io.importers.historical_replay import (
    EvidenceLevel, HistoricalEpisodeSummary, HistoricalEvent,
    HistoricalImportResult, HistoricalReplay, HISTORICAL_IMPORTER_VERSION,
    MappingStatus, _read_json, _read_jsonl_optional,
)
from wargame_native_engine.io.serialization import json_clone


class CanonicalReplayImporter:
    """Minimal adapter for already parsed game-session-replay/1.0 archives."""

    def import_directory(self, root: Path,
                         source_hash: str) -> HistoricalImportResult:
        manifest = _read_json(root / "manifest.json")
        commands = _read_jsonl_optional(root / "commands.jsonl")
        events_raw = _read_jsonl_optional(root / "events.jsonl")
        result_raw = _read_json(root / "result.json")
        snapshots = _read_jsonl_optional(root / "snapshots.jsonl")
        events = tuple(HistoricalEvent(
            historical_time=float(item.get("step_index", 0)),
            event_type=str(item.get("event_type", "unknown")),
            side=item.get("side"),
            actor_id=(None if item.get("operator_id") is None
                      else int(item["operator_id"])),
            mapping_status=MappingStatus.NORMALIZED.value,
            source_raw=json_clone(item),
        ) for item in events_raw)
        summary = HistoricalEpisodeSummary(
            terminal=result_raw.get("terminal"),
            truncated=result_raw.get("truncated"), winner=result_raw.get("winner"),
            terminal_reason=result_raw.get("terminal_reason"),
            terminal_time=result_raw.get("total_steps"), final_survivors={},
            cumulative_damage={}, source_raw=json_clone(result_raw))
        # Public session snapshots are side-filtered observations, not complete
        # world frames. Preserve them, but do not promote them to state evidence.
        evidence = (EvidenceLevel.EVENT_LEVEL if events
                    else EvidenceLevel.SUMMARY_ONLY)
        warnings = ({"code": "canonical_commands_not_assumed_executed",
                     "count": len(commands)},) if commands else ()
        replay = HistoricalReplay(
            replay_id=str(manifest.get("run_id", "canonical-replay")),
            source_format="game-session-replay/1.0", source_hash=source_hash,
            evidence_level=evidence,
            scenario_id=str(manifest.get("scenario_id", "unknown")),
            map_id=str(manifest.get("map_id", "unknown")),
            time_unit="sdk_step", coordinate_unit="unknown",
            frames=(), actions=(), events=events, advantage_curve=(),
            summary=summary,
            metadata={"source_manifest": json_clone(manifest),
                      "snapshot_records_preserved": len(snapshots)},
            warnings=warnings)
        import_manifest = {
            "schema_version": "historical-import/1.0",
            "replay_id": replay.replay_id, "source_identifier": str(root),
            "source_hash": source_hash, "source_format": replay.source_format,
            "importer_version": HISTORICAL_IMPORTER_VERSION,
            "evidence_level": evidence.value, "scenario_id": replay.scenario_id,
            "map_id": replay.map_id, "time_unit": replay.time_unit,
            "coordinate_unit": replay.coordinate_unit,
            "frame_count": 0, "action_count": 0,
            "event_count": len(events), "warning_count": len(warnings),
        }
        return HistoricalImportResult(
            replay, import_manifest,
            {"commands": [{"count": len(commands),
                            "mapping_status": "UNMAPPED",
                            "reason": "command receipt/parameters not guaranteed"}],
             "snapshots": [{"count": len(snapshots),
                             "mapping_status": "UNMAPPED",
                             "reason": "canonical public snapshot adapter is minimal"}]})
