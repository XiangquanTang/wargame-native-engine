"""Bind an uploaded Source scenario to one raw map and prepare a new game.

Input files are never rewritten. Native rule conventions are recorded in an
explicit generated supplement alongside the conversion report.
"""
from __future__ import annotations

from contextlib import contextmanager
import json
from pathlib import Path
import shutil
from uuid import uuid4

from wargame_native_engine.data.map_workspace import (
    _coordinate_compatibility, _directory, _map_compatibility, _read_json,
    _reject_symlinks, _runtime, _unpack, _validate_odd_r_edges,
    initialize_workspace,
)
from wargame_native_engine.io.structured_map import load_structured_map
from wargame_native_engine.io.native_map_converter import convert_benchmark_basic_map
from wargame_native_engine.data.source_scenario import (
    EXPLICIT_NATIVE_SUPPLEMENT, convert_source_scenario,
    publish_source_scenario_conversion,
)
from wargame_native_engine.data.models import ScenarioBundle


@contextmanager
def _one_raw_map(source: Path):
    _reject_symlinks(source)
    if source.is_file() and source.suffix.lower() == ".zip":
        with _unpack(source) as root:
            candidates = sorted(root.rglob("basic.json"))
            if len(candidates) != 1:
                raise ValueError("new game requires exactly one basic.json in the map ZIP")
            yield candidates[0]
        return
    if source.is_dir():
        candidates = sorted(source.rglob("basic.json"))
        if len(candidates) != 1:
            raise ValueError("new game requires exactly one basic.json in the map folder")
        source = candidates[0]
    _reject_symlinks(source)
    if not source.is_file():
        raise ValueError(f"map input not found: {source}")
    yield source


def prepare_source_game(
    map_input: Path, scenario_input: Path, workspace: Path, *,
    coordinate_system: str | None = None,
    visibility_modes: tuple[int, ...] | None = None,
) -> dict:
    """Prepare one fresh, fully bound game; never select an existing run."""
    from wargame_native_engine.data.source_scenario_profiles import build_source_scenario_profile

    map_input, scenario_input, workspace = Path(map_input), Path(scenario_input), Path(workspace)
    _reject_symlinks(scenario_input)
    source_scenario = _read_json(scenario_input)
    initialize_workspace(workspace)
    prepared_root = _directory(workspace / "scenarios")
    token = uuid4().hex
    staging = prepared_root / f".preparing-{token}"
    final = prepared_root / f"game-{token}"
    staging.mkdir()
    try:
        with _one_raw_map(map_input) as basic:
            raw_map = _read_json(basic)
            coordinates, coordinate_source = _coordinate_compatibility(raw_map, coordinate_system)
            structured = load_structured_map(basic, input_format="basic", coordinate_system_override=coordinates)
            geometry = _validate_odd_r_edges(structured)
            if (coordinate_source == "shared_odd_r_convention"
                    and not geometry["checked_diagonal_edges"]):
                raise ValueError("missing coordinate_system needs odd-r diagonal-neighbor evidence or --coordinate-system odd-r")
            scale_override = 200 if "scale" not in raw_map else None
            map_conversion = convert_benchmark_basic_map(basic, coordinate_system_override=coordinates,
                                                       hex_size_m_override=scale_override)
            native_map = map_conversion.native_map
            scenario_id = source_scenario.get("scenario_id")
            if isinstance(scenario_id, bool) or not isinstance(scenario_id, (str, int)) or not str(scenario_id):
                raise ValueError("source scenario requires scenario_id")
            profile = build_source_scenario_profile(source_scenario, map_id=native_map.map_id,
                                                    hex_size_m=native_map.hex_size_m,
                                                    elevation_unit_m=native_map.elevation_unit_m)
            supplement = staging / "native-supplement.json"
            supplement.write_text(json.dumps(profile, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            conversion = convert_source_scenario(
                scenario_input, basic, conversion_mode=EXPLICIT_NATIVE_SUPPLEMENT,
                source_scenario_identity=scenario_input.name,
                source_map_identity=f"map-{native_map.map_id}/basic.json",
                expected_scenario_id=str(scenario_id), expected_map_id=native_map.map_id,
                supplement_path=supplement, coordinate_system_override=coordinates,
                hex_size_m_override=scale_override,
            )
            published = publish_source_scenario_conversion(conversion, staging / "converted")
            modes, mapping_source = _map_compatibility(basic, visibility_modes)
            runtime = _runtime(conversion.scenario.map, basic, workspace,
                               visibility_modes=modes, mapping_source=mapping_source)
            if runtime["status"] != "ready":
                raise ValueError("new game requires map movement and visibility tables")
            bundle = ScenarioBundle.from_path(published.scenario_path).with_map_runtime_data(runtime["path"])
            try:
                bundle.validate()
                # Check initial state/LOS combinations before publishing ready.
                from wargame_native_engine.core.engine import NativeEngine
                from wargame_native_engine.compat.source_frame.template import source_frame_template_for_scenario
                source_frame_template_for_scenario(bundle.scenario)
                engine = NativeEngine(bundle.scenario, map_runtime_data=bundle.map_runtime_data)
                engine.reset(seed=int(profile["seed_policy"]["default_seed"]))
                for side in ("red", "blue"):
                    engine.observe(side)
            finally:
                if bundle.map_runtime_data is not None:
                    bundle.map_runtime_data.visibility.close()
            shutil.copyfile(scenario_input, staging / "source-scenario.json")
            shutil.copyfile(basic, staging / "source-map.json")
            report = {
                "schema": "native-source-game-preparation/1.0", "status": "ready",
                "scenario_id": str(scenario_id), "map_id": native_map.map_id,
                "scenario_path": str((final / "converted" / published.scenario_path.name).resolve()),
                "supplement_path": str((final / supplement.name).resolve()),
                "runtime": runtime, "conversion": conversion.manifest,
                "coordinate_system_source": coordinate_source, "coordinate_geometry": geometry,
                "run_namespace": f"source-game-{token}",
                "output_dir": str((workspace / "runs" / f"source-game-{token}").resolve()),
            }
            (staging / "preparation.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        staging.rename(final)
        report["preparation_path"] = str((final / "preparation.json").resolve())
        return report
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise
