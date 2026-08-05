from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
from typing import Any, Mapping

from wargame_native_engine.io.serialization import canonical_hash, json_clone
from wargame_native_engine.rules.combat_rules import CombatRules
from wargame_native_engine.rules.movement_rules import MovementRules
from wargame_native_engine.rules.operator_rules import OperatorRules
from wargame_native_engine.rules.terminal_rules import TerminalRules
from wargame_native_engine.rules.terrain_rules import TerrainRules
from wargame_native_engine.rules.visibility_rules import VisibilityRules
from wargame_native_engine.version import ENGINE_VERSION


@dataclass(frozen=True, slots=True)
class TimingRules:
    tick_duration: float
    tick_unit: str
    deployment_simultaneous: bool
    combat_simultaneous: bool
    wait_allowed: bool
    no_progress_ticks: int | None


@dataclass(frozen=True, slots=True)
class Ruleset:
    ruleset_id: str
    ruleset_version: str
    ruleset_type: str
    description: str
    minimum_engine_version: str
    coordinate_unit: str
    deterministic: bool
    random_rules: tuple[tuple[str, Any], ...]
    timing: TimingRules
    movement: MovementRules
    visibility: VisibilityRules
    combat: CombatRules
    terminal: TerminalRules
    operators: OperatorRules
    terrain: TerrainRules
    _canonical_content: tuple[tuple[str, Any], ...]

    @property
    def content_hash(self) -> str:
        return canonical_hash(dict(self._canonical_content))

    def to_dict(self) -> dict[str, Any]:
        return json_clone(dict(self._canonical_content))


def default_ruleset_path() -> Path:
    return Path(__file__).resolve().parents[3] / "config/rulesets/native_default_v1.json"


def load_ruleset(path: str | Path | None = None) -> Ruleset:
    source = default_ruleset_path() if path is None else Path(path)
    root = _read_object(source)
    _validate_ruleset_root(root)
    base = source.parent
    operators_doc = _load_reference(base, root["operator_types"])
    weapons_doc = _load_reference(base, root["weapon_types"])
    terrain_doc = _load_reference(base, root["terrain_types"])
    _validate_profile_document(operators_doc, "operator_types")
    _validate_profile_document(weapons_doc, "weapon_types")
    _validate_profile_document(terrain_doc, "terrain_types")

    timing = _object(root["timing"], "timing")
    movement = _object(root["movement"], "movement")
    visibility = _object(root["visibility"], "visibility")
    combat = _object(root["combat"], "combat")
    terminal = _object(root["terminal"], "terminal")
    operator_profiles = _object(operators_doc["profiles"], "operator profiles")
    weapon_profiles = _object(weapons_doc["profiles"], "weapon profiles")
    terrain_profiles = _object(terrain_doc["profiles"], "terrain profiles")

    expanded = json_clone(root)
    expanded["operator_types"] = operators_doc
    expanded["weapon_types"] = weapons_doc
    expanded["terrain_types"] = terrain_doc
    canonical_content = tuple(sorted(expanded.items()))
    rules = Ruleset(
        ruleset_id=str(root["ruleset_id"]),
        ruleset_version=str(root["ruleset_version"]),
        ruleset_type=str(root["ruleset_type"]),
        description=str(root["description"]),
        minimum_engine_version=str(root["minimum_engine_version"]),
        coordinate_unit=str(root["coordinate_unit"]),
        deterministic=bool(root["deterministic"]),
        random_rules=_freeze_mapping(_object(root["random_rules"], "random_rules")),
        timing=TimingRules(
            tick_duration=_positive(timing, "tick_duration"),
            tick_unit=str(timing["tick_unit"]),
            deployment_simultaneous=bool(timing["deployment_simultaneous"]),
            combat_simultaneous=bool(timing["combat_simultaneous"]),
            wait_allowed=bool(timing["wait_allowed"]),
            no_progress_ticks=(None if timing.get("no_progress_ticks") is None
                               else int(timing["no_progress_ticks"])),
        ),
        movement=MovementRules(
            default_move_range=_nonnegative(movement, "default_move_range"),
            operator_type_ranges=_float_pairs(movement, "operator_type_ranges"),
            boundary_policy=str(movement["boundary_policy"]),
            impassable_terrain=tuple(sorted(str(item) for item in movement["impassable_terrain"])),
            terrain_multipliers=_float_pairs(movement, "terrain_multipliers"),
            allow_position_overlap=bool(movement["allow_position_overlap"]),
            minimum_unit_spacing=_nonnegative(movement, "minimum_unit_spacing"),
            float_tolerance=_positive(movement, "float_tolerance"),
        ),
        visibility=VisibilityRules(
            default_sensor_range=_nonnegative(visibility, "default_sensor_range"),
            operator_type_ranges=_float_pairs(visibility, "operator_type_ranges"),
            friendly_always_visible=bool(visibility["friendly_always_visible"]),
            enemy_condition=str(visibility["enemy_condition"]),
            distance_method=str(visibility["distance_method"]),
            inclusive_boundary=bool(visibility["inclusive_boundary"]),
            occlusion_enabled=bool(visibility["occlusion_enabled"]),
            elevation_enabled=bool(visibility["elevation_enabled"]),
            concealment_enabled=bool(visibility["concealment_enabled"]),
        ),
        combat=CombatRules(
            default_attack_range=_nonnegative(combat, "default_attack_range"),
            default_damage=_nonnegative(combat, "default_damage"),
            deterministic_hits=bool(combat["deterministic_hits"]),
            consume_ammunition=bool(combat["consume_ammunition"]),
            simultaneous_damage=bool(combat["simultaneous_damage"]),
            destroyed_actor_executes_submitted_action=bool(
                combat["destroyed_actor_executes_submitted_action"]),
            multi_attack_resolution=str(combat["multi_attack_resolution"]),
            invalidated_target_policy=str(combat["invalidated_target_policy"]),
            minimum_health=_nonnegative(combat, "minimum_health"),
            destroyed_threshold=_nonnegative(combat, "destroyed_threshold"),
            float_tolerance=_positive(combat, "float_tolerance"),
            weapon_profiles=tuple(sorted(
                (int(key), _freeze_mapping(_object(value, f"weapon {key}")))
                for key, value in weapon_profiles.items())),
        ),
        terminal=TerminalRules(
            max_ticks=int(terminal["max_ticks"]),
            elimination=bool(terminal["elimination"]),
            objective_score_to_win=(None if terminal.get("objective_score_to_win") is None
                                    else int(terminal["objective_score_to_win"])),
            mission_score_enabled=bool(terminal["mission_score_enabled"]),
            mutual_elimination_result=str(terminal["mutual_elimination_result"]),
            tie_result=str(terminal["tie_result"]),
            evaluation_order=tuple(str(item) for item in terminal["evaluation_order"]),
        ),
        operators=OperatorRules(tuple(sorted(
            (str(key), tuple(sorted((str(name), float(item))
                                    for name, item in _object(value, f"operator {key}").items())))
            for key, value in operator_profiles.items()))),
        terrain=TerrainRules(tuple(sorted(
            (str(key), _freeze_mapping(_object(value, f"terrain {key}")))
            for key, value in terrain_profiles.items()))),
        _canonical_content=canonical_content,
    )
    _validate_semantics(rules)
    return rules


def load_config_document(path: str | Path, kind: str) -> Mapping[str, Any]:
    """Load and validate one independently versioned profile document."""
    if kind not in {"operator_types", "weapon_types", "terrain_types"}:
        raise ValueError(f"unknown configuration kind: {kind}")
    value = _read_object(Path(path))
    _validate_profile_document(value, kind)
    return json_clone(value)


def _read_object(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"invalid rules configuration {path}: {exc}") from exc
    return dict(_object(value, str(path)))


def _load_reference(base: Path, value: Any) -> dict[str, Any]:
    if not isinstance(value, str) or not value:
        raise ValueError("configuration reference must be a non-empty path")
    return _read_object((base / value).resolve())


def _validate_ruleset_root(value: Mapping[str, Any]) -> None:
    required = {
        "schema_version", "ruleset_id", "ruleset_version", "ruleset_type",
        "description", "minimum_engine_version", "coordinate_unit",
        "deterministic", "random_rules", "timing", "movement", "visibility",
        "combat", "terminal", "operator_types", "weapon_types", "terrain_types",
    }
    missing = sorted(required - set(value))
    if missing:
        raise ValueError(f"ruleset missing required fields: {missing}")
    if value["schema_version"] != "native-ruleset/1.0":
        raise ValueError("unsupported ruleset schema")
    if value["ruleset_type"] != "native_simplified":
        raise ValueError("ruleset_type must be native_simplified")


def _validate_profile_document(value: Mapping[str, Any], kind: str) -> None:
    if value.get("schema_version") != f"native-{kind.replace('_', '-')}/1.0":
        raise ValueError(f"unsupported {kind} schema")
    _object(value.get("profiles"), f"{kind}.profiles")


def _validate_semantics(rules: Ruleset) -> None:
    if _version_tuple(ENGINE_VERSION) < _version_tuple(rules.minimum_engine_version):
        raise ValueError("ruleset requires a newer engine version")
    if not rules.deterministic:
        raise ValueError("this engine requires deterministic rulesets")
    if not (rules.timing.deployment_simultaneous
            and rules.timing.combat_simultaneous):
        raise ValueError("this engine requires simultaneous action phases")
    if not rules.timing.wait_allowed:
        raise ValueError("this engine currently requires WAIT to be allowed")
    if rules.timing.no_progress_ticks is not None:
        raise ValueError("no-progress termination is not enabled in this ruleset")
    if rules.movement.boundary_policy != "reject":
        raise ValueError("unsupported map boundary policy")
    if not rules.visibility.friendly_always_visible:
        raise ValueError("friendly operators must remain visible")
    if rules.visibility.enemy_condition != "alive_deployed_within_sensor_radius":
        raise ValueError("unsupported enemy visibility condition")
    if rules.visibility.distance_method != "euclidean_2d":
        raise ValueError("unsupported visibility distance method")
    if (rules.visibility.occlusion_enabled
            or rules.visibility.elevation_enabled
            or rules.visibility.concealment_enabled):
        raise ValueError("occlusion, elevation and concealment are not implemented")
    if not rules.combat.deterministic_hits:
        raise ValueError("probabilistic hits are not implemented")
    if rules.combat.consume_ammunition:
        raise ValueError("ammunition consumption is not implemented")
    if not rules.combat.simultaneous_damage:
        raise ValueError("default native engine supports simultaneous damage only")
    if not rules.combat.destroyed_actor_executes_submitted_action:
        raise ValueError("submitted attacks must execute during simultaneous damage")
    if rules.combat.multi_attack_resolution != "aggregate_then_apply":
        raise ValueError("unsupported multi-attack resolution")
    if rules.combat.invalidated_target_policy != "committed_target_receives_damage":
        raise ValueError("unsupported invalidated-target policy")


def _object(value: Any, name: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError(f"{name} must be an object")
    return value


def _freeze_mapping(value: Mapping[str, Any]) -> tuple[tuple[str, Any], ...]:
    frozen: list[tuple[str, Any]] = []
    for key, item in value.items():
        if isinstance(item, Mapping):
            normalized: Any = _freeze_mapping(item)
        elif isinstance(item, list):
            normalized = tuple(item)
        else:
            normalized = item
        frozen.append((str(key), normalized))
    return tuple(sorted(frozen))


def _float_pairs(value: Mapping[str, Any], name: str) -> tuple[tuple[str, float], ...]:
    return tuple(sorted((str(key), float(item))
                        for key, item in _object(value[name], name).items()))


def _positive(value: Mapping[str, Any], name: str) -> float:
    result = float(value[name])
    if result <= 0:
        raise ValueError(f"{name} must be positive")
    return result


def _nonnegative(value: Mapping[str, Any], name: str) -> float:
    result = float(value[name])
    if result < 0:
        raise ValueError(f"{name} must be non-negative")
    return result


def _version_tuple(value: str) -> tuple[int, ...]:
    try:
        return tuple(int(item) for item in value.split("."))
    except ValueError as exc:
        raise ValueError(f"invalid semantic version: {value}") from exc
