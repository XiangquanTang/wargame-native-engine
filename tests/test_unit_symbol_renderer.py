from __future__ import annotations

import json
from pathlib import Path
import shutil
import subprocess

import pytest

from wargame_native_engine.viewer.operator_registry import load_operator_registry
from wargame_native_engine.viewer.static_resources import (
    load_symbol_static_resource,
)


ROOT = Path(__file__).resolve().parents[1]
STATIC = ROOT / "src/wargame_native_engine/viewer/static"
RESOLVER = STATIC / "unit_symbol_resolver.js"
RENDERER = STATIC / "unit_symbol_renderer.js"
MAP_RENDERER = STATIC / "map_renderer.js"


def test_registry_descriptor_supplies_tank_base_and_heavy_modifier() -> None:
    descriptor = load_operator_registry().resolve_operator(2, 0)

    assert descriptor.symbol.base == "tank"
    assert descriptor.symbol.modifiers == ("armor_heavy",)


def test_resolver_builds_base_and_modifier_svg_layer_paths() -> None:
    source = RESOLVER.read_text(encoding="utf-8")

    assert "resolveSvgLayers(descriptor)" in source
    assert "assets/unit_symbols/base/${base}.svg" in source
    assert "assets/unit_symbols/modifiers/${modifier}.svg" in source
    assert "SAFE_LAYER_NAME" in source


def test_unknown_symbol_uses_only_safe_coarse_or_unknown_svg() -> None:
    source = RESOLVER.read_text(encoding="utf-8")

    assert 'base_symbol: baseSymbol' in source
    assert 'const SAFE_UNKNOWN_BASES = new Set([' in source
    assert 'const baseSymbol = typeCode === 1' in source
    assert '? "infantry"' in source
    assert 'typeCode === 2 ? "unknown_vehicle" : "unknown"' in source
    assert 'value.status === "UNKNOWN" ? [] : requestedModifiers' in source
    assert '!SAFE_UNKNOWN_BASES.has(requestedBase)' in source


@pytest.mark.skipif(shutil.which("node") is None, reason="Node.js is not installed")
def test_resolver_renders_known_personnel_domain_without_guessing_subtype() -> None:
    script = r"""
const resolver = require(process.argv[1]);
const active = resolver.create({schema: "operator-registry/v1", operators: {}});
const personnel = active.resolveOperator({type_code: 1, sub_type_code: 0});
const unknown = active.resolveOperator({type_code: 999, sub_type_code: 999});
if (personnel.status !== "UNKNOWN") process.exit(10);
if (personnel.base_symbol !== "infantry") process.exit(11);
if (personnel.modifiers.length !== 0) process.exit(12);
if (!active.resolveSvgLayers(personnel).layers[0].endsWith("/infantry.svg")) {
  process.exit(13);
}
if (unknown.base_symbol !== "unknown") process.exit(14);
if (!active.resolveSvgLayers(unknown).layers[0].endsWith("/unknown.svg")) {
  process.exit(15);
}
"""
    result = subprocess.run(
        ["node", "-e", script, str(RESOLVER.resolve())],
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr


def test_map_renderer_retains_canvas_fallback_when_svg_is_unavailable() -> None:
    source = MAP_RENDERER.read_text(encoding="utf-8")

    assert "const UNIT_SYMBOL_SCREEN_SIZE = 34" in source
    assert "const SELECTED_UNIT_SYMBOL_SCREEN_SIZE = 40" in source
    assert "const svgDrawn = Boolean" in source
    assert "if (!svgDrawn)" in source
    assert 'symbol.base_symbol === "infantry" ? "infantry" : "unknown"' in source
    assert "this._unitShape(context, fallbackKind, radius)" in source
    assert "this._drawTacticalOverlays(context)" in source
    assert "this.hitTargets.push" in source


def test_unit_symbol_cache_uses_svg_text_blob_image_and_draw_image() -> None:
    source = RENDERER.read_text(encoding="utf-8")

    assert "class UnitSymbolCache" in source
    assert "this.symbols = new Map()" in source
    assert "source.replace(/currentColor/g, color)" in source
    assert "new this.BlobClass" in source
    assert "this.URLApi.createObjectURL(blob)" in source
    assert "new this.ImageClass()" in source
    assert "context.drawImage(image" in source
    assert 'RED: "#d33"' in source
    assert 'BLUE: "#36f"' in source
    assert 'DESTROYED: "#777777"' in source


def test_symbol_static_resource_serves_only_registry_and_whitelisted_svg(
    tmp_path: Path,
) -> None:
    static_root = tmp_path / "static"
    base = static_root / "assets/unit_symbols/base"
    base.mkdir(parents=True)
    svg = b'<svg xmlns="http://www.w3.org/2000/svg"></svg>'
    (base / "tank.svg").write_bytes(svg)
    registry_path = tmp_path / "registry.json"
    registry_path.write_text(
        json.dumps({"schema": "operator-registry/v1"}), encoding="utf-8",
    )

    asset = load_symbol_static_resource(
        "/static/assets/unit_symbols/base/tank.svg",
        static_root=static_root,
        registry_path=registry_path,
    )
    registry = load_symbol_static_resource(
        "/static/operator_registry_v1.json",
        static_root=static_root,
        registry_path=registry_path,
    )

    assert asset is not None
    assert asset.payload == svg
    assert asset.content_type == "image/svg+xml; charset=utf-8"
    assert registry is not None
    assert registry.content_type == "application/json; charset=utf-8"
    assert load_symbol_static_resource(
        "/static/assets/unit_symbols/base/../secret.svg",
        static_root=static_root,
    ) is None
    assert load_symbol_static_resource(
        "/static/assets/unit_symbols/base/tank.png",
        static_root=static_root,
    ) is None


def test_all_viewer_pages_load_symbol_layers_before_map_renderer() -> None:
    for name in ("index.html", "live_index.html", "dual_control_index.html"):
        source = (STATIC / name).read_text(encoding="utf-8")
        resolver = source.index('/static/unit_symbol_resolver.js')
        renderer = source.index('/static/unit_symbol_renderer.js')
        map_renderer = source.index('/static/map_renderer.js')
        assert resolver < renderer < map_renderer


def test_every_registry_symbol_layer_has_a_supplied_svg_asset() -> None:
    registry = load_operator_registry()
    bases = {descriptor.symbol.base for descriptor in registry.operators.values()}
    modifiers = {
        modifier
        for descriptor in registry.operators.values()
        for modifier in descriptor.symbol.modifiers
    }

    assert not [
        base for base in sorted(bases)
        if not (STATIC / f"assets/unit_symbols/base/{base}.svg").is_file()
    ]
    assert not [
        modifier for modifier in sorted(modifiers)
        if not (
            STATIC / f"assets/unit_symbols/modifiers/{modifier}.svg"
        ).is_file()
    ]
    assert (STATIC / "assets/unit_symbols/base/unknown.svg").is_file()
    assert (STATIC / "assets/unit_symbols/base/unknown_vehicle.svg").is_file()
