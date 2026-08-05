from __future__ import annotations

import pytest

from wargame_native_engine.compat.source_frame.models import UnitDomain
from wargame_native_engine.visibility import (
    HexCell, HexMapProfile, ObservationProfiles, evaluate_line_of_sight,
    evaluate_visibility,
)


def line_profile(end_col: int, *, elevations=None, terrains=None) -> HexMapProfile:
    elevations = elevations or {}
    terrains = terrains or {}
    return HexMapProfile({100 + col: HexCell(100 + col, elevations.get(col, 1),
                                             terrains.get(col, "OPEN"))
                          for col in range(1, end_col + 1)})


def test_adjacent_hex_always_visible() -> None:
    profile = line_profile(2, elevations={2: 99})
    assert evaluate_line_of_sight(profile, 101, 102).visible


def test_higher_intermediate_hex_blocks_los() -> None:
    profile = line_profile(4, elevations={2: 9})
    result = evaluate_line_of_sight(profile, 101, 104)
    assert not result.visible and 102 in result.blocking_hexes


def test_linear_elevation_interpolation() -> None:
    profile = line_profile(4, elevations={1: 1, 2: 2, 3: 3, 4: 4})
    result = evaluate_line_of_sight(profile, 101, 104)
    assert [round(float(item["ratio"]), 3) for item in result.samples] == [0.333, 0.667]


def test_high_observer_bonus() -> None:
    profile = line_profile(4, elevations={1: 4, 2: 3, 3: 2, 4: 1})
    assert evaluate_line_of_sight(profile, 101, 104).visible


def test_forest_intermediate_height_bonus() -> None:
    profile = line_profile(3, terrains={2: "FOREST"})
    result = evaluate_line_of_sight(profile, 101, 103)
    assert not result.visible and result.blocking_hexes == (102,)


def test_concealment_halves_observation() -> None:
    profile = line_profile(7)
    result = evaluate_visibility(profile, observer_hex=101, target_hex=107,
                                 observer_domain=UnitDomain.PERSONNEL,
                                 target_domain=UnitDomain.PERSONNEL,
                                 target_concealed=True)
    assert not result.observable and result.observation_limit == 5


def test_vehicle_concealment_ignored_from_higher_observer() -> None:
    profile = line_profile(16, elevations={1: 2, 16: 1})
    result = evaluate_visibility(profile, observer_hex=101, target_hex=116,
                                 observer_domain=UnitDomain.PERSONNEL,
                                 target_domain=UnitDomain.VEHICLE,
                                 target_concealed=True)
    assert result.observable and result.observation_limit == 25


def test_observation_profile_by_target_type() -> None:
    values = ObservationProfiles()
    assert values.limit(UnitDomain.VEHICLE, UnitDomain.PERSONNEL) == 10
    assert values.limit(UnitDomain.PERSONNEL, UnitDomain.VEHICLE) == 25
    assert values.limit(UnitDomain.AIR, UnitDomain.AIR) == 25
