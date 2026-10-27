"""Read-only capability-to-implementation traceability registry."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
import json
from pathlib import Path

from wargame_native_engine.demo_registry import DemoRegistry


CAPABILITY_REGISTRY_SCHEMA = "native-capability-registry/v1"
ENGINE_STATUSES = frozenset(
    {"IMPLEMENTED", "SIMPLIFIED_IMPLEMENTATION", "NOT_IMPLEMENTED"}
)
DEMO_STATUSES = frozenset({"PASS", "PENDING"})
CATEGORIES = frozenset({"COMBAT", "MOBILITY", "OBJECTIVE", "OBSERVATION"})


class CapabilityRegistryError(ValueError):
    """The capability catalog or one of its traceability links is invalid."""


@dataclass(frozen=True, slots=True)
class CapabilityDescriptor:
    capability_id: str
    category: str
    engine_status: str
    demo_status: str
    demo_id: str
    demo_feature: str
    replay_support: bool
    replay_evidence: str
    human_support: bool
    agent_support: bool
    implementation_refs: tuple[str, ...]
    rule_refs: tuple[str, ...]
    test_refs: tuple[str, ...]
    notes: str


class CapabilityRegistry:
    """Validated, deterministic capability coverage matrix."""

    def __init__(
        self,
        repository_root: Path,
        descriptors: Sequence[CapabilityDescriptor],
    ) -> None:
        self.repository_root = repository_root.resolve()
        ordered = tuple(sorted(descriptors, key=lambda item: item.capability_id))
        if len({item.capability_id for item in ordered}) != len(ordered):
            raise CapabilityRegistryError("capability IDs must be unique")
        self._descriptors = ordered
        self._by_id = {item.capability_id: item for item in ordered}

    @classmethod
    def load(
        cls,
        path: str | Path,
        *,
        repository_root: str | Path | None = None,
    ) -> "CapabilityRegistry":
        source = Path(path).resolve()
        value = _json_object(source)
        if value.get("schema") != CAPABILITY_REGISTRY_SCHEMA:
            raise CapabilityRegistryError("unsupported capability registry schema")
        raw_capabilities = value.get("capabilities")
        if not isinstance(raw_capabilities, list):
            raise CapabilityRegistryError("capabilities must be an array")
        descriptors = tuple(_descriptor(item) for item in raw_capabilities)
        if tuple(item.capability_id for item in descriptors) != tuple(
            sorted(item.capability_id for item in descriptors)
        ):
            raise CapabilityRegistryError(
                "capabilities must be deterministically sorted by capability_id"
            )
        root = (
            Path(repository_root).resolve()
            if repository_root is not None
            else source.parents[2]
        )
        registry = cls(root, descriptors)
        registry.validate_references()
        return registry

    @property
    def descriptors(self) -> tuple[CapabilityDescriptor, ...]:
        return self._descriptors

    def resolve(self, capability_id: str) -> CapabilityDescriptor:
        try:
            return self._by_id[str(capability_id)]
        except KeyError as exc:
            raise CapabilityRegistryError(
                f"unknown capability: {capability_id}"
            ) from exc

    def validate_references(self) -> None:
        for descriptor in self.descriptors:
            for reference in (
                *descriptor.implementation_refs,
                *descriptor.rule_refs,
                *descriptor.test_refs,
            ):
                relative = reference.split("::", 1)[0]
                candidate = (self.repository_root / relative).resolve()
                if not candidate.is_relative_to(self.repository_root):
                    raise CapabilityRegistryError(
                        f"reference escapes repository root: {reference}"
                    )
                if not candidate.is_file():
                    raise CapabilityRegistryError(
                        f"capability reference is missing: {reference}"
                    )

    def validate_demo_traceability(self, demos: DemoRegistry) -> None:
        """Cross-check capability declarations against the Demo Library."""

        for descriptor in self.descriptors:
            demo = demos.resolve(descriptor.demo_id)
            if (
                demo.capability_ids
                and descriptor.capability_id not in demo.capability_ids
            ):
                raise CapabilityRegistryError(
                    f"{descriptor.capability_id} is absent from demo "
                    f"capability_ids for {demo.demo_id}"
                )
            if descriptor.demo_feature not in demo.features:
                raise CapabilityRegistryError(
                    f"{descriptor.capability_id} feature {descriptor.demo_feature} "
                    f"is absent from demo {demo.demo_id}"
                )
            expected = demos.expected_behavior(demo)
            expected_status = expected.get("status")
            outcome = expected.get("outcome")
            strict_replay_expected = (
                outcome.get("strict_replay_matches")
                if isinstance(outcome, Mapping)
                else None
            )
            if descriptor.demo_status == "PENDING":
                if descriptor.engine_status != "NOT_IMPLEMENTED":
                    raise CapabilityRegistryError(
                        f"pending capability {descriptor.capability_id} must be "
                        "NOT_IMPLEMENTED"
                    )
                if expected_status != "NOT_IMPLEMENTED":
                    raise CapabilityRegistryError(
                        f"pending demo {demo.demo_id} lacks NOT_IMPLEMENTED status"
                    )
                if any(
                    (
                        descriptor.replay_support,
                        descriptor.human_support,
                        descriptor.agent_support,
                    )
                ):
                    raise CapabilityRegistryError(
                        f"pending capability {descriptor.capability_id} advertises support"
                    )
            elif descriptor.engine_status == "NOT_IMPLEMENTED":
                raise CapabilityRegistryError(
                    f"unimplemented capability {descriptor.capability_id} cannot PASS"
                )
            elif not descriptor.replay_support:
                raise CapabilityRegistryError(
                    f"passing demo {demo.demo_id} must have Replay support"
                )
            elif strict_replay_expected is not True:
                raise CapabilityRegistryError(
                    f"replay-backed capability {descriptor.capability_id} lacks "
                    "a strict replay expectation"
                )


def load_default_capability_registry() -> CapabilityRegistry:
    """Load the optional demo-certification catalog shipped with this release.

    An empty catalog advertises no bundled demo certifications. It does not
    imply that the engine has no capabilities; use the live capability APIs
    for gameplay and ``examples/maps/catalog.json`` for shipped scenarios.
    """
    repository_root = Path(__file__).resolve().parents[2]
    return CapabilityRegistry.load(
        repository_root
        / "config/capabilities/native_capability_registry_v1.json",
        repository_root=repository_root,
    )


def _descriptor(value: object) -> CapabilityDescriptor:
    if not isinstance(value, Mapping):
        raise CapabilityRegistryError("each capability descriptor must be an object")
    capability_id = _text(value.get("capability_id"), "capability_id")
    category = _choice(value.get("category"), CATEGORIES, "category")
    engine_status = _choice(
        value.get("engine_status"), ENGINE_STATUSES, "engine_status"
    )
    demo_status = _choice(value.get("demo_status"), DEMO_STATUSES, "demo_status")
    return CapabilityDescriptor(
        capability_id=capability_id,
        category=category,
        engine_status=engine_status,
        demo_status=demo_status,
        demo_id=_text(value.get("demo_id"), "demo_id"),
        demo_feature=_text(value.get("demo_feature"), "demo_feature"),
        replay_support=_boolean(value.get("replay_support"), "replay_support"),
        replay_evidence=_text(value.get("replay_evidence"), "replay_evidence"),
        human_support=_boolean(value.get("human_support"), "human_support"),
        agent_support=_boolean(value.get("agent_support"), "agent_support"),
        implementation_refs=_references(
            value.get("implementation_refs"), "implementation_refs"
        ),
        rule_refs=_references(value.get("rule_refs"), "rule_refs"),
        test_refs=_references(value.get("test_refs"), "test_refs"),
        notes=_text(value.get("notes"), "notes"),
    )


def _json_object(path: Path) -> dict[str, object]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise CapabilityRegistryError(
            f"cannot load capability registry: {path}"
        ) from exc
    if not isinstance(value, dict):
        raise CapabilityRegistryError("capability registry root must be an object")
    return value


def _text(value: object, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise CapabilityRegistryError(f"capability {field} must be a non-empty string")
    return value.strip()


def _choice(value: object, choices: frozenset[str], field: str) -> str:
    text = _text(value, field)
    if text not in choices:
        raise CapabilityRegistryError(f"unsupported capability {field}: {text}")
    return text


def _boolean(value: object, field: str) -> bool:
    if not isinstance(value, bool):
        raise CapabilityRegistryError(f"capability {field} must be boolean")
    return value


def _references(value: object, field: str) -> tuple[str, ...]:
    if not isinstance(value, list) or not all(
        isinstance(item, str) and item.strip() for item in value
    ):
        raise CapabilityRegistryError(f"capability {field} must be a string array")
    normalized = tuple(item.strip() for item in value)
    if normalized != tuple(sorted(set(normalized))):
        raise CapabilityRegistryError(
            f"capability {field} must be unique and deterministically sorted"
        )
    for reference in normalized:
        path = Path(reference.split("::", 1)[0])
        if path.is_absolute() or ".." in path.parts:
            raise CapabilityRegistryError(
                f"capability {field} contains an unsafe reference"
            )
    return normalized
