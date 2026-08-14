"""Explicit catalog of canonical Native Scenario bundles."""

from __future__ import annotations

from pathlib import Path
from types import MappingProxyType
from typing import Mapping

from wargame_native_engine.data.models import DataLoadError, ScenarioBundle


class ScenarioCatalog:
    def __init__(
        self,
        root: Path,
        entries: Mapping[str, ScenarioBundle],
    ) -> None:
        self.root = root
        self._entries = MappingProxyType(dict(entries))

    @classmethod
    def open(cls, data_root: str | Path) -> "ScenarioCatalog":
        root = Path(data_root)
        if not root.is_dir():
            raise DataLoadError("catalog_not_found", f"not a directory: {root}")
        entries: dict[str, ScenarioBundle] = {}
        # File discovery is deterministic but identity always comes from the
        # parsed canonical document, never from the filename.
        for path in sorted(root.glob("*.json"), key=lambda item: item.name):
            try:
                bundle = ScenarioBundle.from_path(path)
            except DataLoadError as exc:
                if exc.code in {"invalid_scenario_root", "invalid_native_scenario"}:
                    continue
                raise
            scenario_id = bundle.scenario_identity.scenario_id
            if scenario_id in entries:
                raise DataLoadError(
                    "duplicate_scenario_id", f"duplicate scenario {scenario_id!r}"
                )
            entries[scenario_id] = bundle
        if not entries:
            raise DataLoadError(
                "catalog_empty", f"no canonical Native Scenario JSON in {root}"
            )
        return cls(root, entries)

    @classmethod
    def from_bundles(
        cls,
        entries: Mapping[str, ScenarioBundle],
    ) -> "ScenarioCatalog":
        normalized: dict[str, ScenarioBundle] = {}
        for key, bundle in entries.items():
            identity = bundle.scenario_identity.scenario_id
            if str(key) != identity:
                raise DataLoadError(
                    "catalog_key_mismatch", f"{key!r} != {identity!r}"
                )
            bundle.validate()
            normalized[identity] = bundle
        if not normalized:
            raise DataLoadError("catalog_empty", "at least one bundle is required")
        return cls(Path("."), normalized)

    @property
    def scenario_ids(self) -> tuple[str, ...]:
        return tuple(sorted(self._entries))

    def load_scenario(self, scenario_id: str) -> ScenarioBundle:
        try:
            bundle = self._entries[str(scenario_id)]
        except KeyError as exc:
            raise DataLoadError(
                "scenario_not_found", f"unknown scenario {scenario_id!r}"
            ) from exc
        bundle.validate()
        return bundle
