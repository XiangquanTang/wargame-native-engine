"""Immutable canonical scenario bundles shared by Engine and Renderer."""

from __future__ import annotations

from dataclasses import dataclass, replace
import hashlib
import json
from pathlib import Path
from types import MappingProxyType
from typing import Any, Mapping

from wargame_native_engine.io.scenario import (
    NativeScenario,
    native_map_identity_hash,
    scenario_from_dict,
)
from wargame_native_engine.io.serialization import canonical_hash, canonical_json
from wargame_native_engine.data.map_runtime import MapRuntimeData
from wargame_native_engine.viewer.live_control import native_scenario_map_record


SCENARIO_BUNDLE_SCHEMA = "native-scenario-bundle/1.0"


class DataLoadError(ValueError):
    """Stable structured failure at the product data boundary."""

    def __init__(self, code: str, message: str) -> None:
        self.code = str(code)
        self.message = str(message)
        super().__init__(f"{self.code}:{self.message}")


@dataclass(frozen=True, slots=True)
class ScenarioIdentity:
    scenario_id: str
    sha256: str


@dataclass(frozen=True, slots=True)
class MapIdentity:
    map_id: str
    sha256: str


@dataclass(frozen=True, slots=True)
class DataProvenance:
    source_sha256: str
    source_kind: str
    converter_id: str | None = None
    converter_version: str | None = None
    warnings: tuple[str, ...] = ()
    source_path_identity: str | None = None

    def public_record(self) -> dict[str, object]:
        return {
            "source_sha256": self.source_sha256,
            "source_kind": self.source_kind,
            "converter_id": self.converter_id,
            "converter_version": self.converter_version,
            "warnings": list(self.warnings),
        }


@dataclass(frozen=True, slots=True)
class ScenarioBundle:
    """Canonical bytes plus detached Engine/Renderer projections.

    ``NativeScenario`` contains mutable operator values for runtime setup, so a
    bundle never returns its private parsed object.  Each Engine consumer gets a
    detached scenario reconstructed from the same canonical bytes.  Rendering
    is also derived from those bytes, which prevents same-name map drift.
    """

    scenario_identity: ScenarioIdentity
    map_identity: MapIdentity
    provenance: DataProvenance
    _canonical_payload: bytes
    _map_runtime_data: MapRuntimeData | None = None

    @classmethod
    def from_path(cls, path: str | Path) -> "ScenarioBundle":
        source = Path(path)
        if not source.is_file():
            raise DataLoadError("scenario_not_found", f"not a file: {source}")
        raw = source.read_bytes()
        try:
            value = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise DataLoadError("invalid_scenario_json", str(source)) from exc
        if not isinstance(value, Mapping):
            raise DataLoadError("invalid_scenario_root", "root must be an object")
        return cls.from_record(
            value,
            provenance=DataProvenance(
                source_sha256=hashlib.sha256(raw).hexdigest(),
                source_kind="canonical_native_scenario",
                source_path_identity=str(source.resolve()),
            ),
        )

    @classmethod
    def from_scenario(
        cls,
        scenario: NativeScenario,
        *,
        provenance: DataProvenance | None = None,
    ) -> "ScenarioBundle":
        return cls.from_record(
            scenario.to_dict(),
            provenance=provenance or DataProvenance(
                source_sha256=canonical_hash(scenario.to_dict()),
                source_kind="native_scenario_object",
            ),
        )

    @classmethod
    def from_record(
        cls,
        value: Mapping[str, Any],
        *,
        provenance: DataProvenance,
    ) -> "ScenarioBundle":
        try:
            scenario = scenario_from_dict(value)
        except (KeyError, TypeError, ValueError) as exc:
            raise DataLoadError("invalid_native_scenario", str(exc)) from exc
        canonical_record = scenario.to_dict()
        payload = (canonical_json(canonical_record) + "\n").encode("utf-8")
        scenario_sha = hashlib.sha256(payload).hexdigest()
        map_sha = native_map_identity_hash(scenario.map)
        return cls(
            ScenarioIdentity(scenario.scenario_id, scenario_sha),
            MapIdentity(scenario.map.map_id, map_sha),
            provenance,
            payload,
        )

    @property
    def canonical_payload(self) -> bytes:
        return bytes(self._canonical_payload)

    @property
    def scenario(self) -> NativeScenario:
        value = json.loads(self._canonical_payload.decode("utf-8"))
        return scenario_from_dict(value)

    def validate(self) -> None:
        scenario = self.scenario
        actual_scenario = hashlib.sha256(self._canonical_payload).hexdigest()
        actual_map = native_map_identity_hash(scenario.map)
        if actual_scenario != self.scenario_identity.sha256:
            raise DataLoadError("scenario_identity_mismatch", actual_scenario)
        if actual_map != self.map_identity.sha256:
            raise DataLoadError("map_identity_mismatch", actual_map)
        if self._map_runtime_data is not None:
            if (
                self._map_runtime_data.map_id != self.map_identity.map_id
                or self._map_runtime_data.map_sha256 != self.map_identity.sha256
            ):
                raise DataLoadError(
                    "map_runtime_identity_mismatch",
                    self._map_runtime_data.manifest_sha256,
                )

    @property
    def map_runtime_data(self) -> MapRuntimeData | None:
        return self._map_runtime_data

    def with_map_runtime_data(
        self,
        source: str | Path | MapRuntimeData,
    ) -> "ScenarioBundle":
        runtime = (
            source
            if isinstance(source, MapRuntimeData)
            else MapRuntimeData.load(source, self.scenario.map)
        )
        result = replace(self, _map_runtime_data=runtime)
        result.validate()
        return result

    def render_map_record(self) -> Mapping[str, object]:
        record = native_scenario_map_record(self.scenario)
        record["map_identity"] = {
            "map_id": self.map_identity.map_id,
            "sha256": self.map_identity.sha256,
        }
        record["scenario_identity"] = {
            "scenario_id": self.scenario_identity.scenario_id,
            "sha256": self.scenario_identity.sha256,
        }
        record["data_provenance"] = self.provenance.public_record()
        return MappingProxyType(record)

    def public_record(self) -> Mapping[str, object]:
        value: dict[str, object] = {
            "schema": SCENARIO_BUNDLE_SCHEMA,
            "scenario_identity": {
                "scenario_id": self.scenario_identity.scenario_id,
                "sha256": self.scenario_identity.sha256,
            },
            "map_identity": {
                "map_id": self.map_identity.map_id,
                "sha256": self.map_identity.sha256,
            },
            "provenance": self.provenance.public_record(),
        }
        if self._map_runtime_data is not None:
            value["map_runtime_data"] = self._map_runtime_data.identity_record()
        return MappingProxyType(value)
