"""Read-only map intake, isolated conversion, and per-map loading reports.

Original files stay in ``inbox``. Content-addressed runtime caches and unique
reports are separate from the inputs and from existing Map123 runs.
"""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import html
import json
from pathlib import Path, PurePosixPath
import stat
import tempfile
from uuid import uuid4
import zipfile

from wargame_native_engine.data import MapRuntimeData, ScenarioBundle, publish_map_runtime_data
from wargame_native_engine.io.native_map_converter import convert_benchmark_basic_map
from wargame_native_engine.io.structured_map import MAX_SOURCE_BYTES, load_structured_map

MAX_ARCHIVE_BYTES = 512 * 1024 * 1024
MAX_ARCHIVE_MEMBERS = 4096
WORKSPACE_DIRECTORIES = ("inbox", "runtime-cache", "reports", "runs")


def initialize_workspace(workspace: Path) -> dict[str, str]:
    """Create only missing directories, leaving all existing data untouched."""
    return {name: str(_directory(workspace / name)) for name in WORKSPACE_DIRECTORIES}


def _reject_symlinks(path: Path) -> None:
    for component in (path, *path.parents):
        if component.is_symlink():
            raise ValueError(f"map workspace symlink is not supported: {component}")


def _directory(path: Path) -> Path:
    _reject_symlinks(path)
    path.mkdir(parents=True, exist_ok=True)
    return path.resolve()


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _read_json(path: Path) -> dict:
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError(f"duplicate JSON key: {key}")
            result[key] = value
        return result

    if path.stat().st_size > MAX_SOURCE_BYTES:
        raise ValueError("map JSON exceeds 64 MiB")
    value = json.loads(path.read_text(encoding="utf-8-sig"), object_pairs_hook=unique,
                       parse_constant=lambda value: (_ for _ in ()).throw(ValueError(f"invalid JSON number: {value}")))
    if not isinstance(value, dict):
        raise ValueError("map JSON must be an object")
    return value


def _discover(root: Path) -> list[Path]:
    if root.is_file():
        return [root]
    if not root.is_dir():
        raise ValueError(f"input not found: {root}")
    # No symlink traversal: uploaded data cannot redirect discovery outside inbox.
    found = []
    for path in sorted(root.rglob("*")):
        if path.is_symlink() or any(parent.is_symlink() for parent in path.parents if parent != root.parent):
            continue
        if path.is_file() and (path.suffix.lower() == ".zip" or path.name == "basic.json"
                               or path.name.startswith("scenario") and path.suffix == ".json"
                               or path.name in ("native_map.native.json", "native-map.json")):
            found.append(path)
    return found


@contextmanager
def _unpack(source: Path):
    """Extract bounded plain members; reject aliases, symlinks, and traversal."""
    if source.stat().st_size > MAX_ARCHIVE_BYTES:
        raise ValueError("map ZIP exceeds 512 MiB")
    with tempfile.TemporaryDirectory(prefix="native-map-intake-") as directory:
        root = Path(directory)
        with zipfile.ZipFile(source) as archive:
            members = archive.infolist()
            if len(members) > MAX_ARCHIVE_MEMBERS or sum(m.file_size for m in members) > MAX_ARCHIVE_BYTES:
                raise ValueError("map ZIP exceeds extraction limits")
            seen = set()
            for member in members:
                name = member.filename
                path = PurePosixPath(name)
                mode = member.external_attr >> 16
                if ("\\" in name or path.is_absolute() or ".." in path.parts
                        or not path.parts or ":" in path.parts[0]
                        or any(ord(c) < 32 for c in name)
                        or stat.S_ISLNK(mode) or member.flag_bits & 1):
                    raise ValueError(f"unsafe map ZIP member: {name!r}")
                normalized = path.as_posix().rstrip("/")
                if normalized in seen:
                    raise ValueError(f"duplicate map ZIP member: {name!r}")
                seen.add(normalized)
                target = root.joinpath(*path.parts)
                if member.is_dir():
                    target.mkdir(parents=True, exist_ok=True)
                    continue
                if stat.S_IFMT(mode) not in (0, stat.S_IFREG):
                    raise ValueError(f"nonregular map ZIP member: {name!r}")
                target.parent.mkdir(parents=True, exist_ok=True)
                with archive.open(member) as incoming, target.open("xb") as outgoing:
                    remaining = member.file_size
                    while chunk := incoming.read(min(1024 * 1024, remaining + 1)):
                        remaining -= len(chunk)
                        if remaining < 0:
                            raise ValueError("map ZIP size mismatch")
                        outgoing.write(chunk)
                    if remaining:
                        raise ValueError("map ZIP size mismatch")
        yield root


def _visibility_modes(value, *, label: str) -> tuple[int, ...]:
    if (not isinstance(value, (list, tuple)) or not 1 <= len(value) <= 10
            or any(isinstance(mode, bool) or not isinstance(mode, int) or not 0 <= mode < 10 for mode in value)
            or len(set(value)) != len(value)):
        raise ValueError(f"{label} must contain 1–10 unique integer channel IDs in 0–9")
    return tuple(value)


def _map_compatibility(basic: Path, override: tuple[int, ...] | None) -> tuple[tuple[int, ...] | None, str]:
    sidecar = basic.parent / "map-compatibility.json"
    declared = None
    if sidecar.exists() or sidecar.is_symlink():
        _reject_symlinks(sidecar)
        record = _read_json(sidecar)
        if set(record) != {"visibility_modes"}:
            raise ValueError("map-compatibility.json requires only visibility_modes")
        declared = _visibility_modes(record["visibility_modes"], label="map-compatibility.json visibility_modes")
    if override is not None and declared is not None and override != declared:
        raise ValueError("--visibility-modes conflicts with map-compatibility.json")
    modes = override if override is not None else declared
    source = "cli" if override is not None else "map-compatibility.json" if declared is not None else "canonical_prefix_convention"
    return modes, source


def _runtime(native_map, basic: Path, workspace: Path, *, visibility_modes: tuple[int, ...] | None = None,
             mapping_source: str = "canonical_prefix_convention") -> dict:
    movement = basic.parent / "cost.pickle"
    visibility = sorted(basic.parent.glob("*see.npz"))
    if not movement.exists() and not visibility:
        return {"status": "absent"}
    if not movement.is_file() or len(visibility) != 1:
        raise ValueError("runtime data requires cost.pickle and exactly one *see.npz")
    sources = [basic, movement, visibility[0]]
    if any(path.is_symlink() for path in sources):
        raise ValueError("runtime input symlinks are not supported")
    source_hashes = [_sha256(path) for path in sources]
    cache_input = [native_map.to_dict(), source_hashes]
    if visibility_modes is not None:
        cache_input.append({"visibility_modes": list(visibility_modes)})
    identity = hashlib.sha256(json.dumps(cache_input, sort_keys=True).encode()).hexdigest()
    target = workspace / "runtime-cache" / f"map-runtime-{identity}"
    _reject_symlinks(target)
    if target.exists() and any(path.is_symlink() for path in target.rglob("*")):
        raise ValueError("runtime cache symlinks are not supported")
    hit = target.exists()
    if not hit:
        publish_map_runtime_data(native_map, source_map_path=basic, movement_cost_path=movement,
                                 visibility_npz_path=visibility[0], output_dir=target, visibility_modes=visibility_modes)
    runtime = MapRuntimeData.load(target, native_map)
    try:
        identity_record = runtime.identity_record()
        available_modes = list(runtime.visibility.mode_order)
        expected_modes = list(visibility_modes) if visibility_modes is not None else list(range(runtime.visibility.shape[0]))
        if available_modes != expected_modes:
            raise ValueError("runtime cache visibility mapping differs from requested configuration")
    finally:
        runtime.visibility.close()
    return {"status": "ready", "cache_hit": hit, "path": str(target.resolve()), "identity": identity_record,
            "visibility_modes": available_modes, "missing_visibility_modes": [mode for mode in range(10) if mode not in available_modes],
            "visibility_mapping_source": mapping_source}


def _coordinate_compatibility(raw: dict, override: str | None) -> tuple[str | None, str]:
    declared = raw.get("coordinate_system")
    if declared is not None:
        if not isinstance(declared, str) or declared.lower() not in ("odd-r", "oddr"):
            raise ValueError("map coordinate_system must be odd-r")
        return override, "source"
    return (override, "cli") if override is not None else ("odd-r", "shared_odd_r_convention")


def _validate_odd_r_edges(structured: dict) -> dict:
    columns = {name: index for index, name in enumerate(structured["cell_columns"])}
    checked = diagonal_edges = 0
    for cell in structured["cells"]:
        x, y = cell[columns["x"]], cell[columns["y"]]
        neighbors = cell[columns["neighbors"]]
        if neighbors is None:
            continue
        shift = y % 2
        expected = ((y, x + 1), (y - 1, x + shift), (y - 1, x + shift - 1),
                    (y, x - 1), (y + 1, x + shift - 1), (y + 1, x + shift))
        for direction, neighbor in enumerate(neighbors):
            if neighbor < 0:
                continue
            if divmod(neighbor, 100) != expected[direction]:
                raise ValueError(f"neighbor geometry does not match odd-r at ({x}, {y}) direction {direction}")
            checked += 1
            diagonal_edges += direction in (1, 2, 4, 5)
    return {"status": "validated_declared_edges" if checked else "convention_without_edge_evidence",
            "checked_directed_edges": checked, "checked_diagonal_edges": diagonal_edges}


def _check_map(source: Path, workspace: Path, destination: Path, *, coordinate_system: str | None,
               terrain_only: bool, require_runtime: bool, visibility_modes: tuple[int, ...] | None, result: dict) -> None:
    raw = _read_json(source)
    kind = "basic" if "map_data" in raw else "native-scenario" if "map" in raw else "native-map"
    effective_coordinates, coordinate_source = (None, "native")
    if kind == "basic":
        effective_coordinates, coordinate_source = _coordinate_compatibility(raw, coordinate_system)
    structured = load_structured_map(source, input_format=kind,
                                    coordinate_system_override=effective_coordinates)
    if kind == "basic":
        result["coordinate_system"] = "odd-r"
        result["coordinate_system_source"] = coordinate_source
        result["coordinate_geometry"] = _validate_odd_r_edges(structured)
        if (coordinate_source == "shared_odd_r_convention"
                and not result["coordinate_geometry"]["checked_diagonal_edges"]):
            raise ValueError("missing coordinate_system needs odd-r diagonal-neighbor evidence or --coordinate-system odd-r")
    destination.mkdir(parents=True)
    structured_path = destination / "structured-map.json"
    structured_path.write_text(json.dumps(structured, ensure_ascii=False, allow_nan=False), encoding="utf-8")
    result.update({"input_format": kind, "map_id": structured["map_id"], "map_hash": structured["map_hash"],
              "cell_count": len(structured["cells"]), "terrain_ready": True, "scenario_ready": False,
              "structured_map": str(structured_path.resolve()), "runtime": {"status": "not_applicable"}})
    from wargame_native_engine.io.map_preview import render_map_preview
    preview = destination / "preview.html"
    preview.write_text(render_map_preview(structured, title=f"地图 {structured['map_id']}"), encoding="utf-8")
    result["preview"] = str(preview.resolve())
    if kind == "basic":
        if terrain_only:
            result["runtime"] = {"status": "skipped"}
        else:
            modes, mapping_source = _map_compatibility(source, visibility_modes)
            default_scale = 200 if raw.get("scale") is None else None
            conversion = convert_benchmark_basic_map(source, coordinate_system_override=effective_coordinates,
                                                     hex_size_m_override=default_scale)
            (destination / "native-map.json").write_bytes(conversion.native_map_payload)
            (destination / "conversion.json").write_bytes(conversion.provenance_payload)
            result["hex_size_m"] = conversion.native_map.hex_size_m
            result["hex_size_source"] = "shared_map123_default" if default_scale is not None else "source_scale"
            result["runtime"] = _runtime(conversion.native_map, source, workspace,
                                         visibility_modes=modes, mapping_source=mapping_source)
    elif kind == "native-scenario":
        bundle = ScenarioBundle.from_path(source)
        bundle.validate()
        bundle.render_map_record()
        result["scenario_ready"] = True
        result["scenario_id"] = bundle.scenario_identity.scenario_id
        scenario_path = destination / "scenario.json"
        scenario_path.write_bytes(source.read_bytes())
        result["scenario_path"] = str(scenario_path.resolve())
    if require_runtime and result["runtime"]["status"] != "ready":
        raise ValueError("full runtime lookup data is required")



def check_workspace(workspace: Path, *, source: Path | None = None, coordinate_system: str | None = None,
                    terrain_only: bool = False, require_runtime: bool = False,
                    visibility_modes: tuple[int, ...] | list[int] | None = None) -> dict:
    """Check maps independently and publish one immutable, resumable report."""
    if terrain_only and require_runtime:
        raise ValueError("--terrain-only and --require-runtime cannot be combined")
    if visibility_modes is not None:
        visibility_modes = _visibility_modes(visibility_modes, label="visibility_modes")
    _reject_symlinks(workspace)
    workspace = workspace.resolve()
    initialize_workspace(workspace)
    source = source or workspace / "inbox"
    _reject_symlinks(source)
    source = source.resolve()
    candidates = _discover(source)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    report_dir = workspace / "reports" / f"check-{stamp}-{uuid4().hex[:8]}"
    report_dir.mkdir()
    entries = []

    def check(path: Path, label: str):
        entry = {"source": label}
        try:
            if path.is_symlink():
                raise ValueError("map input symlinks are not supported")
            _check_map(path, workspace, report_dir / f"map-{len(entries):04d}",
                       coordinate_system=coordinate_system, terrain_only=terrain_only,
                       require_runtime=require_runtime, visibility_modes=visibility_modes, result=entry)
            entry["status"] = "passed"
        except (OSError, ValueError, KeyError, TypeError, OverflowError) as exc:
            entry.update(status="failed", error_code=getattr(exc, "code", type(exc).__name__), error=str(exc))
        entries.append(entry)

    for candidate in candidates:
        if candidate.suffix.lower() != ".zip":
            check(candidate, str(candidate))
            continue
        try:
            with _unpack(candidate) as unpacked:
                members = _discover(unpacked)
                if not members:
                    raise ValueError("map ZIP contains no basic.json, native-map.json, or scenario*.json")
                for member in members:
                    label = f"{candidate}!{member.relative_to(unpacked).as_posix()}"
                    if member.suffix.lower() == ".zip":
                        entries.append({"source": label, "status": "failed", "error": "nested map ZIP is not supported"})
                    else:
                        check(member, label)
        except (OSError, ValueError, zipfile.BadZipFile, RuntimeError) as exc:
            entries.append({"source": str(candidate), "status": "failed", "error": str(exc),
                            "error_code": getattr(exc, "code", type(exc).__name__)})
    report = {"schema": "native-map-workspace-check/1.0", "workspace": str(workspace),
              "input": str(source), "report": str(report_dir / "summary.json"),
              "total": len(entries), "passed": sum(e["status"] == "passed" for e in entries),
              "failed": sum(e["status"] == "failed" for e in entries), "maps": entries}
    rows = []
    for entry in entries:
        label = html.escape(str(entry.get("map_id", entry["source"])))
        if "preview" in entry:
            link = html.escape(Path(entry["preview"]).relative_to(report_dir).as_posix(), quote=True)
            label = f'<a href="{link}">{label}</a>'
        status = "通过" if entry["status"] == "passed" else "失败"
        detail = html.escape(str(entry.get("error", entry.get("runtime", {}).get("status", ""))))
        rows.append(f"<tr><td>{label}</td><td>{status}</td><td>{detail}</td></tr>")
    index = report_dir / "index.html"
    index.write_text('<!doctype html><html lang="zh-CN"><meta charset="utf-8"><title>地图加载检查</title>'
                     '<style>body{font:16px system-ui;margin:32px}td,th{padding:12px;text-align:left;border-bottom:1px solid #ddd}</style>'
                     f'<h1>地图加载检查</h1><p>通过 {report["passed"]} / {report["total"]}</p>'
                     '<table><tr><th>地图预览</th><th>结果</th><th>运行数据</th></tr>'
                     + ''.join(rows) + '</table></html>', encoding="utf-8")
    report["index"] = str(index)
    Path(report["report"]).write_text(json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8")
    return report
