from __future__ import annotations

from wargame_native_engine.compat.source_frame.models import UnitDomain
from wargame_native_engine.visibility.line_of_sight import (
    HexMapProfile,
    LineOfSightEvidence,
    evaluate_line_of_sight,
)
from wargame_native_engine.visibility.line_of_sight import hex_distance
from wargame_native_engine.visibility.profiles import ObservationProfiles
from wargame_native_engine.visibility.result import VisibilityResult


def within_maximum_observation_distance(
    *,
    observer_hex: int,
    target_hex: int,
    observer_domain: UnitDomain,
    target_domain: UnitDomain,
    observer_is_drone: bool = False,
    target_is_drone: bool = False,
    observation_profiles: ObservationProfiles | None = None,
) -> bool:
    """Cheap conservative gate before LOS evaluation.

    Terrain and concealment can only reduce the profile limit, so a pair beyond
    this unmodified maximum can never be observable. Returning ``True`` does
    not establish visibility; callers must still run ``evaluate_visibility``.
    """

    profiles = observation_profiles or ObservationProfiles()
    limit = profiles.limit(
        observer_domain,
        target_domain,
        observer_is_drone=observer_is_drone,
        target_is_drone=target_is_drone,
    )
    return limit > 0 and hex_distance(observer_hex, target_hex) <= limit


def evaluate_visibility(profile: HexMapProfile, *, observer_hex: int,
                        target_hex: int, observer_domain: UnitDomain,
                        target_domain: UnitDomain,
                        observer_elevation: float | None = None,
                        target_elevation: float | None = None,
                        target_concealed: bool = False,
                        target_terrain: str | None = None,
                        observer_is_drone: bool = False,
                        target_is_drone: bool = False,
                        observation_profiles: ObservationProfiles | None = None,
                        line_of_sight_evidence: LineOfSightEvidence | None = None,
                        ) -> VisibilityResult:
    profiles = observation_profiles or ObservationProfiles()
    los = (
        evaluate_line_of_sight(profile, observer_hex, target_hex)
        if line_of_sight_evidence is None
        else line_of_sight_evidence
    )
    limit = profiles.limit(observer_domain, target_domain,
                           observer_is_drone=observer_is_drone,
                           target_is_drone=target_is_drone)
    modifiers: list[dict[str, object]] = [dict(item) for item in los.modifiers]
    observer_height = (profile.cell(observer_hex).elevation if observer_elevation is None
                       else observer_elevation)
    target_height = (profile.cell(target_hex).elevation if target_elevation is None
                     else target_elevation)
    concealment_ignored = (target_domain is UnitDomain.VEHICLE
                           and target_height < observer_height)
    if target_concealed and not concealment_ignored:
        limit /= 2.0
        modifiers.append({"rule": "concealment_halves_observation", "factor": 0.5})
    elif target_concealed:
        modifiers.append({"rule": "vehicle_concealment_ignored_from_higher_observer",
                          "factor": 1.0})
    terrain = (target_terrain or profile.cell(target_hex).terrain).upper()
    if terrain in {"SETTLEMENT", "FOREST"}:
        limit /= 2.0
        modifiers.append({"rule": "target_terrain_halves_observation",
                          "terrain": terrain, "factor": 0.5})
    observable = los.visible and limit > 0 and los.distance <= limit
    if not los.visible:
        reason = "line_of_sight_blocked"
    elif limit <= 0:
        reason = "target_category_not_observable"
    elif los.distance > limit:
        reason = "outside_observation_limit"
    else:
        reason = "observable"
    return VisibilityResult(los.visible, observable, los.distance, limit,
                            los.blocking_hexes, tuple(modifiers), reason)
