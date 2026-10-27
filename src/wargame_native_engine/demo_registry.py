"""Deterministic, data-only catalog for repository-authored demonstrations."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from copy import deepcopy
from dataclasses import dataclass
import json
from pathlib import Path


DEMO_REGISTRY_SCHEMA = "native-demo-registry/v2"
LEGACY_DEMO_REGISTRY_SCHEMA = "native-demo-registry/1.0"
DEMO_EXPECTED_SCHEMA = "native-demo-expected/1.0"

# Accepted identifiers are a compatibility vocabulary, not a claim that a
# corresponding demo/certification is bundled. Keep external v2 catalogs
# readable when this distribution ships an empty optional capability catalog.
_LEGACY_CAPABILITY_IDS = (
    "boarding", "concealment", "direct_fire", "drone", "guided_fire",
    "indirect_fire", "loitering_munition", "objective_control", "visibility",
)


class DemoRegistryError(ValueError):
    """A demo catalog or one of its referenced contracts is invalid."""


@dataclass(frozen=True, slots=True)
class CompositeScenarioPhase:
    name: str
    required_capability: str
    validator: str

    def to_record(self) -> dict[str, str]:
        return {
            "name": self.name,
            "required_capability": self.required_capability,
            "validator": self.validator,
        }


@dataclass(frozen=True, slots=True)
class DemoDescriptor:
    demo_id: str
    name: str
    purpose: str
    scenario: str
    expected_behavior: str
    readme: str
    features: tuple[str, ...]
    capability_ids: tuple[str, ...]
    phases: tuple[CompositeScenarioPhase, ...]
    expected: Mapping[str, object]

    @property
    def composite(self) -> bool:
        return len(self.capability_ids) > 1

    def to_record(self) -> dict[str, object]:
        return {
            "id": self.demo_id,
            "name": self.name,
            "purpose": self.purpose,
            "scenario": self.scenario,
            "expected_behavior": self.expected_behavior,
            "readme": self.readme,
            "features": list(self.features),
            "capability_ids": list(self.capability_ids),
            "phases": [value.to_record() for value in self.phases],
            "expected": deepcopy(dict(self.expected)),
        }


class DemoRegistry:
    """Validated catalog whose paths cannot escape the demo-library root."""

    def __init__(
        self,
        root: Path,
        descriptors: Sequence[DemoDescriptor],
    ) -> None:
        self.root = root.resolve()
        ordered = tuple(sorted(descriptors, key=lambda value: value.demo_id))
        if len({value.demo_id for value in ordered}) != len(ordered):
            raise DemoRegistryError("demo IDs must be unique")
        self._descriptors = ordered
        self._by_id = {value.demo_id: value for value in ordered}

    @classmethod
    def load(
        cls,
        path: str | Path,
        *,
        known_capability_ids: Sequence[str] | None = None,
    ) -> "DemoRegistry":
        source = Path(path)
        value = _json_object(source)
        if value.get("schema") not in {
            DEMO_REGISTRY_SCHEMA,
            LEGACY_DEMO_REGISTRY_SCHEMA,
        }:
            raise DemoRegistryError("unsupported demo registry schema")
        raw_demos = value.get("demos")
        if not isinstance(raw_demos, list):
            raise DemoRegistryError("demo registry demos must be an array")
        if known_capability_ids is None and value.get("schema") == DEMO_REGISTRY_SCHEMA:
            known_capability_ids = _default_known_capability_ids()
        known = (
            None if known_capability_ids is None else frozenset(
                _text(item, "known capability")
                for item in known_capability_ids
            )
        )
        descriptors = tuple(
            _descriptor(
                item,
                known,
                require_capabilities=(value.get("schema") == DEMO_REGISTRY_SCHEMA),
            )
            for item in raw_demos
        )
        registry = cls(source.parent, descriptors)
        for descriptor in registry.descriptors:
            registry.scenario_path(descriptor)
            registry.readme_path(descriptor)
            expected = registry.expected_behavior(descriptor)
            if expected.get("demo_id") != descriptor.demo_id:
                raise DemoRegistryError(
                    f"expected behavior ID mismatch for {descriptor.demo_id}"
                )
        return registry

    @property
    def descriptors(self) -> tuple[DemoDescriptor, ...]:
        return self._descriptors

    def resolve(self, demo_id: str) -> DemoDescriptor:
        try:
            return self._by_id[str(demo_id)]
        except KeyError as exc:
            raise DemoRegistryError(f"unknown demo: {demo_id}") from exc

    def scenario_path(self, demo: DemoDescriptor | str) -> Path:
        descriptor = self.resolve(demo) if isinstance(demo, str) else demo
        return self._required_path(descriptor.scenario, "scenario")

    def readme_path(self, demo: DemoDescriptor | str) -> Path:
        descriptor = self.resolve(demo) if isinstance(demo, str) else demo
        return self._required_path(descriptor.readme, "README")

    def expected_behavior(
        self, demo: DemoDescriptor | str,
    ) -> dict[str, object]:
        descriptor = self.resolve(demo) if isinstance(demo, str) else demo
        value = _json_object(
            self._required_path(descriptor.expected_behavior, "expected behavior")
        )
        if value.get("schema") != DEMO_EXPECTED_SCHEMA:
            raise DemoRegistryError(
                f"unsupported expected behavior schema for {descriptor.demo_id}"
            )
        return deepcopy(value)

    def _required_path(self, relative: str, kind: str) -> Path:
        value = (self.root / relative).resolve()
        if not value.is_relative_to(self.root):
            raise DemoRegistryError(f"demo {kind} path escapes library root")
        if not value.is_file():
            raise DemoRegistryError(f"demo {kind} is missing: {relative}")
        return value


def load_default_demo_registry() -> DemoRegistry:
    repository_root = Path(__file__).resolve().parents[2]
    catalog = repository_root / "examples/demos/registry.json"
    if not catalog.is_file():
        raise DemoRegistryError(
            "optional default demo library is not included in this release; "
            "use examples/maps/catalog.json for shipped scenarios, or load "
            "an explicitly supplied demo registry"
        )
    return DemoRegistry.load(
        catalog,
        known_capability_ids=_default_known_capability_ids(),
    )


def _default_known_capability_ids() -> tuple[str, ...]:
    repository_root = Path(__file__).resolve().parents[2]
    capability_record = _json_object(
        repository_root
        / "config/capabilities/native_capability_registry_v1.json"
    )
    capabilities = capability_record.get("capabilities")
    if not isinstance(capabilities, list):
        raise DemoRegistryError("default capability registry lacks capabilities")
    known = tuple(
        _text(value.get("capability_id"), "capability_id")
        for value in capabilities
        if isinstance(value, Mapping)
    )
    return known if capabilities else _LEGACY_CAPABILITY_IDS


def _descriptor(
    value: object,
    known_capability_ids: frozenset[str] | None,
    *,
    require_capabilities: bool,
) -> DemoDescriptor:
    if not isinstance(value, Mapping):
        raise DemoRegistryError("each demo descriptor must be an object")
    demo_id = _text(value.get("id"), "id")
    features = value.get("features")
    if (
        not isinstance(features, list)
        or not features
        or not all(isinstance(item, str) and item for item in features)
    ):
        raise DemoRegistryError(f"demo {demo_id} features must be strings")
    normalized_features = tuple(sorted(set(features)))
    if tuple(features) != normalized_features:
        raise DemoRegistryError(
            f"demo {demo_id} features must be unique and deterministically sorted"
        )
    expected = value.get("expected")
    if not isinstance(expected, Mapping):
        raise DemoRegistryError(f"demo {demo_id} expected must be an object")
    capability_ids = _capability_ids(value, demo_id)
    if require_capabilities and not capability_ids:
        raise DemoRegistryError(
            f"demo {demo_id} must declare capability_id or capability_ids"
        )
    if known_capability_ids is not None:
        unknown = sorted(set(capability_ids).difference(known_capability_ids))
        if unknown:
            raise DemoRegistryError(
                f"demo {demo_id} has unknown capabilities: {', '.join(unknown)}"
            )
    phases = _phases(value.get("phases"), demo_id, capability_ids)
    return DemoDescriptor(
        demo_id,
        _text(value.get("name"), "name"),
        _text(value.get("purpose"), "purpose"),
        _relative(value.get("scenario"), "scenario"),
        _relative(value.get("expected_behavior"), "expected_behavior"),
        _relative(value.get("readme"), "readme"),
        normalized_features,
        capability_ids,
        phases,
        deepcopy(dict(expected)),
    )


def _capability_ids(
    value: Mapping[str, object], demo_id: str,
) -> tuple[str, ...]:
    singular = value.get("capability_id")
    plural = value.get("capability_ids")
    if singular is not None and plural is not None:
        raise DemoRegistryError(
            f"demo {demo_id} cannot define capability_id and capability_ids"
        )
    if plural is not None:
        if not isinstance(plural, list):
            raise DemoRegistryError(
                f"demo {demo_id} capability_ids must be an array"
            )
        result = tuple(_text(item, "capability_id") for item in plural)
    elif singular is not None:
        result = (_text(singular, "capability_id"),)
    else:
        # The v1 catalog used features only. Preserve read compatibility;
        # repository-authored v2 entries declare capability_ids explicitly.
        return ()
    if not result:
        raise DemoRegistryError(f"demo {demo_id} capability_ids cannot be empty")
    if len(result) != len(set(result)):
        raise DemoRegistryError(
            f"demo {demo_id} capability_ids must not contain duplicates"
        )
    if result != tuple(sorted(result)):
        raise DemoRegistryError(
            f"demo {demo_id} capability_ids must be deterministically sorted"
        )
    return result


def _phases(
    value: object,
    demo_id: str,
    capability_ids: tuple[str, ...],
) -> tuple[CompositeScenarioPhase, ...]:
    if value is None:
        return ()
    if not isinstance(value, list):
        raise DemoRegistryError(f"demo {demo_id} phases must be an array")
    phases: list[CompositeScenarioPhase] = []
    for item in value:
        if not isinstance(item, Mapping) or set(item) != {
            "name", "required_capability", "validator"
        }:
            raise DemoRegistryError(
                f"demo {demo_id} phase must define name, required_capability "
                "and validator"
            )
        phase = CompositeScenarioPhase(
            name=_text(item.get("name"), "phase name"),
            required_capability=_text(
                item.get("required_capability"), "required_capability"
            ),
            validator=_text(item.get("validator"), "phase validator").upper(),
        )
        if phase.required_capability not in capability_ids:
            raise DemoRegistryError(
                f"demo {demo_id} phase {phase.name} requires undeclared "
                f"capability {phase.required_capability}"
            )
        phases.append(phase)
    names = tuple(item.name for item in phases)
    if len(names) != len(set(names)):
        raise DemoRegistryError(f"demo {demo_id} phase names must be unique")
    if len(capability_ids) > 1 and not phases:
        raise DemoRegistryError(f"composite demo {demo_id} requires phases")
    return tuple(phases)


def _json_object(path: Path) -> dict[str, object]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise DemoRegistryError(f"cannot load demo JSON: {path}") from exc
    if not isinstance(value, dict):
        raise DemoRegistryError(f"demo JSON root must be an object: {path}")
    return value


def _text(value: object, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise DemoRegistryError(f"demo {field} must be a non-empty string")
    return value.strip()


def _relative(value: object, field: str) -> str:
    text = _text(value, field)
    path = Path(text)
    if path.is_absolute() or ".." in path.parts:
        raise DemoRegistryError(f"demo {field} must be a safe relative path")
    return path.as_posix()
