"""Standalone static-map preview using the live/replay canvas renderer."""

from __future__ import annotations

from html import escape
import json
import math
from pathlib import Path
import re
from typing import Any, Mapping

from wargame_native_engine.io.structured_map import (
    CELL_COLUMNS, MAX_EXACT_INTEGER, STRUCTURED_MAP_SCHEMA,
)
from wargame_native_engine.viewer.map_config import MAX_MAP_CELLS
from wargame_native_engine.viewer.terrain_display import terrain_display_semantic

_RENDERER = Path(__file__).parents[1] / "viewer" / "static" / "map_renderer.js"


def render_map_preview(document: Mapping[str, Any], *, title: str = "地图预览") -> str:
    """Return one offline HTML file; the caller owns its output path.

    Only static cells are projected. No replay, units, scenario, or runtime
    lookup tables are needed. Rendering code is shared with the game viewer.
    """
    record = _preview_map(document)
    payload = json.dumps(record, ensure_ascii=False, allow_nan=False, separators=(",", ":"))
    # JSON sits in a raw-text script element: escape HTML delimiters, not JSON quotes.
    payload = payload.replace("&", "\\u0026").replace("<", "\\u003c").replace(">", "\\u003e")
    safe_title = escape(str(title))
    renderer = _RENDERER.read_text(encoding="utf-8")
    substitutions = {"__TITLE__": safe_title, "__MAP_JSON__": payload,
                     "__SHARED_RENDERER__": renderer}
    return re.sub(r"__TITLE__|__MAP_JSON__|__SHARED_RENDERER__",
                  lambda match: substitutions[match.group()], _HTML)


def _preview_map(document: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(document, Mapping) or document.get("schema_version") != STRUCTURED_MAP_SCHEMA:
        raise ValueError("preview requires native-structured-map/1.0")
    if document.get("cell_columns") != list(CELL_COLUMNS):
        raise ValueError("preview requires the standard structured-map cell columns")
    rows = document.get("cells")
    if not isinstance(rows, list) or not 1 <= len(rows) <= MAX_MAP_CELLS:
        raise ValueError(f"preview requires 1..{MAX_MAP_CELLS} cells")
    coordinates = document.get("coordinates")
    if not isinstance(coordinates, Mapping) or coordinates.get("system") != "odd-r":
        raise ValueError("preview requires odd-r coordinates")
    cells, positions, values = [], set(), []
    for row in rows:
        if not isinstance(row, list) or len(row) != len(CELL_COLUMNS):
            raise ValueError("invalid structured-map cell row")
        x, y, _, level, terrain, position, elevation, raw_terrain, _, roadblock, neighbors, roads, rivers = row
        if (type(x) is not int or type(y) is not int or not 0 <= x < 100 or y < 0
                or type(position) is not int or position > MAX_EXACT_INTEGER
                or position != y * 100 + x or position in positions):
            raise ValueError("invalid or duplicate structured-map coordinates")
        positions.add(position)
        if elevation is not None:
            if type(elevation) not in (int, float) or not math.isfinite(elevation):
                raise ValueError("preview elevation must be finite or null")
            values.append(elevation)
        if type(roadblock) is not bool:
            raise ValueError("preview roadblock must be boolean")
        cells.append({
            "row": y, "column": x, "position_id": str(position),
            "elevation": elevation, "terrain_level": level, "terrain_code": terrain,
            "terrain_source": raw_terrain,
            "terrain_semantic": terrain_display_semantic(raw_terrain, allow_native_strings=True),
            "roadblock": roadblock, "neighbors": neighbors,
            "roads": _edges(roads), "rivers": _edges(rivers),
        })
    xs, ys = [cell["column"] for cell in cells], [cell["row"] for cell in cells]
    elevation = document.get("elevation")
    if not isinstance(elevation, Mapping):
        raise ValueError("preview requires elevation metadata")
    return {
        "mode": "configured-hex-map", "map_id": document.get("map_id"),
        "coordinate_system": "odd-r", "orientation": "pointy-top", "hex_size": 18,
        "rows": max(ys) - min(ys) + 1, "columns": max(xs) - min(xs) + 1,
        "bounds": {"min_row": min(ys), "max_row": max(ys),
                   "min_column": min(xs), "max_column": max(xs)},
        "elevation_kind": elevation.get("kind"), "elevation_unit_m": elevation.get("unit_m"),
        "elevation": {"mode": "episode" if values else "unavailable",
                      "minimum": min(values) if values else None,
                      "maximum": max(values) if values else None, "levels": 7},
        "cells": cells,
    }


def _edges(value: Any) -> list[int]:
    if value is None:
        return [0] * 6
    if (not isinstance(value, list) or len(value) != 6
            or any(type(item) is not int or item < 0 for item in value)):
        raise ValueError("preview directional edges require six non-negative integers")
    return list(value)


_HTML = '''<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>__TITLE__</title>
<style>
*{box-sizing:border-box}body{margin:0;background:#171716;color:#e7e4d9;font:14px system-ui,sans-serif}
header{display:flex;align-items:center;gap:16px;padding:12px 16px;border-bottom:1px solid #454137;flex-wrap:wrap}
h1{font-size:17px;margin:0}button{border:1px solid #6a614e;background:#37332b;color:inherit;padding:6px 12px;border-radius:5px;cursor:pointer}
#summary{color:#c3beae}#legend{display:flex;align-items:center;gap:12px;flex-wrap:wrap;padding:10px 16px}
.elevation-colorbar{width:160px;height:12px;display:inline-block;border:1px solid #8c8169}
.coordinate-legend{color:#aaa695}canvas{width:100%;height:calc(100vh - 156px);min-height:240px;display:block}
#details{min-height:35px;padding:8px 16px;border-top:1px solid #454137;color:#d9d1bb}
</style></head><body>
<header><h1>__TITLE__</h1><span id="summary"></span><button id="fit" type="button">适应窗口</button><span>滚轮缩放 · 拖动平移</span></header>
<div id="legend"></div><canvas id="map" aria-label="地图画布"></canvas><div id="details">移动到格子上查看坐标和地形</div>
<script id="map-data" type="application/json">__MAP_JSON__</script>
<script>__SHARED_RENDERER__</script>
<script>
"use strict";
const mapData = JSON.parse(document.getElementById("map-data").textContent);
const names = {open:"平地",forest:"森林",settlement:"城镇",soft_ground:"松软地",water:"湖泊/大河",small_river:"小河",roadblock:"路障",unknown:"未知"};
const details = document.getElementById("details");
const showCell = cell => {
  if (!cell) { details.textContent = "移动到格子上查看坐标和地形"; return; }
  const unit = mapData.elevation_kind === "METERS" ? " m" : mapData.elevation_kind === "LEVEL" ? " 级" : "";
  details.textContent = `坐标 (${cell.column}, ${cell.row}) · ${names[cell.terrain_semantic] || "未知"} · 高程 ${cell.elevation ?? "未知"}${unit}${cell.roadblock ? " · 路障" : ""}`;
};
const renderer = new ReplayMapRenderer.MapRenderer(document.getElementById("map"), {onHexHovered:showCell,onHexSelected:showCell});
renderer.setMap(mapData);
renderer.render({viewer_units:[],cities:[]}, []);
ReplayMapRenderer.renderElevationLegend(document.getElementById("legend"), mapData);
document.getElementById("summary").textContent = `${mapData.columns} × ${mapData.rows} · ${mapData.cells.length} 格`;
document.getElementById("fit").addEventListener("click", () => renderer.resetView());
window.addEventListener("resize", () => renderer.resetView());
</script></body></html>
'''
