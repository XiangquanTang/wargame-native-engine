"use strict";

// Exercise the shared live/replay renderer against actual Map123 metadata.
const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");
const path = require("node:path");
const root = path.resolve(__dirname, "..");
const { MapRenderer, hexToPixel, summarizeMapLayers, formatEdgeFacts } = require(path.join(
  root, "src/wargame_native_engine/viewer/static/map_renderer.js",
));
const scenario = JSON.parse(fs.readFileSync(
  path.join(root, "examples/map123/scenario.json"), "utf8",
));

function recordingContext() {
  const stack = [];
  return {
    paths: [], fills: [], strokes: [], labels: [], fillStyle: "initial-fill",
    strokeStyle: "initial-stroke", lineWidth: 7, lineJoin: "miter", lineCap: "butt",
    font: "initial-font", textAlign: "initial-align", textBaseline: "initial-baseline", lineDash: [],
    save() {
      stack.push([this.fillStyle, this.strokeStyle, this.lineWidth, this.lineJoin,
        this.lineCap, this.font, this.textAlign, this.textBaseline, this.lineDash.slice()]);
    },
    restore() {
      [this.fillStyle, this.strokeStyle, this.lineWidth, this.lineJoin,
        this.lineCap, this.font, this.textAlign, this.textBaseline, this.lineDash] = stack.pop();
    },
    beginPath() { this.paths = []; },
    moveTo(x, y) { this.paths.push(["M", x, y]); },
    lineTo(x, y) { this.paths.push(["L", x, y]); },
    arc(x, y, radius) { this.paths.push(["A", x, y, radius]); },
    closePath() { this.paths.push(["Z"]); },
    fill() { this.fills.push({ path: this.paths.slice(), color: this.fillStyle }); },
    stroke() {
      this.strokes.push({ path: this.paths.slice(), color: this.strokeStyle,
        width: this.lineWidth, dash: this.lineDash.slice() });
    },
    setLineDash(values) { this.lineDash = values.slice(); },
    measureText(text) { return { width: String(text).length * 5.4 }; },
    fillText(text, x, y) { this.labels.push({ text, x, y, color: this.fillStyle }); },
    strokeText(text, x, y) {
      this.labels.push({ text, x, y, color: this.strokeStyle, width: this.lineWidth });
    },
  };
}

const map = Object.create(MapRenderer.prototype);
map.map = { hex_size: 18 };
map.scale = 1;
map.offsetX = 0;
map.offsetY = 0;
map.hitTargets = [{ id: "existing-unit", x: 10, y: 20, radius: 5 }];
const hitTargets = JSON.stringify(map.hitTargets);
function relativePath(commands, center) {
  return commands.map(([command, x, y, ...rest]) => command === "Z" ? [command]
    : [command, Math.round((x - center.x) * 1e9) / 1e9,
      Math.round((y - center.y) * 1e9) / 1e9, ...rest]);
}
function pathPoints(commands) {
  return commands.flatMap(([command, x, y, radius]) => command === "Z" ? []
    : command === "A" ? Array.from({ length: 24 }, (_, index) => ["P",
      x + Math.cos(index * Math.PI / 12) * radius,
      y + Math.sin(index * Math.PI / 12) * radius]) : [[command, x, y]]);
}
const triangle = [["M", -3, -13], ["L", 10, -9], ["L", -3, -4], ["Z"]];
const square = [["M", -3, -13], ["L", 10, -13], ["L", 10, 0],
  ["L", -3, 0], ["Z"]];
const cities = scenario.metadata.source_cities;
assert.deepEqual(cities.map((city) => city.type), [0, 0, 0, 1, 1, 1, 1]);
assert.equal(cities.filter((city) => city.type === 0).every(
  (city) => city.name.startsWith("主要")), true);
assert.equal(cities.filter((city) => city.type === 1).every(
  (city) => city.name.startsWith("次要")), true);
for (const scale of [0.25, 1, 4]) {
  map.scale = scale;
  map.offsetX = 12;
  map.offsetY = -8;
  for (const source of cities) {
    for (const [flag, color] of [[-1, "#d7c66b"], [0, "#df3d45"], [1, "#357edb"]]) {
      const city = Object.freeze({ ...source, flag });
      const center = map._screen(hexToPixel(
        Math.floor(city.coord / 100), city.coord % 100, map.map.hex_size,
      ));
      const context = recordingContext();
      map._drawObjectives(context, [city]);
      assert.equal(context.fills.length, 1);
      assert.equal(context.fills[0].color, color);
      assert.deepEqual(relativePath(context.fills[0].path, center),
        city.type === 0 ? square : triangle);
      assert.deepEqual(relativePath(context.strokes[0].path, center),
        [["M", -4, 8], ["L", -4, -14]], "Flagpole is unchanged");
      assert.equal(context.strokes[1].color, "#fff3ad");
      assert.equal(context.fillStyle, "initial-fill");
      assert.equal(context.lineWidth, 7);
    }
  }
}
for (const type of [undefined, null, "0", false, 2, -1, "native_objective"]) {
  const context = recordingContext();
  // A major-looking name, score, or position must not invent a major type.
  const city = { ...cities[0], type, flag: 0 };
  map._drawObjectives(context, [city]);
  const center = map._screen(hexToPixel(34, 31, map.map.hex_size));
  assert.deepEqual(relativePath(context.fills[0].path, center), triangle);
}
const invalid = recordingContext();
map._drawObjectives(invalid, [{ coord: -1, type: 0 }, { coord: "3431", type: 0 }]);
assert.equal(invalid.fills.length, 0);
assert.equal(JSON.stringify(map.hitTargets), hitTargets, "Flags do not replace hit targets");

const town = scenario.map.hexes.find((cell) => cell.cond === 2);
for (const scale of [0.005, 0.02, 0.06, 0.15, 0.5, 1, 4]) {
  map.scale = scale;
  const cell = { row: Math.floor(town.pos / 100), column: town.pos % 100,
    terrain_semantic: "settlement" };
  const center = map._screen(hexToPixel(cell.row, cell.column, map.map.hex_size));
  const context = recordingContext();
  map._drawTerrainSymbol(context, cell);
  assert.equal(context.fills.length, 2, "Towns use two compact roofed buildings");
  assert.equal(context.strokes.length, 2, "Both buildings have a contrasting outline");
  const vertices = context.fills.flatMap((fill) => relativePath(fill.path, center))
    .filter(([command]) => command !== "Z");
  const width = Math.max(...vertices.map((p) => p[1]))
    - Math.min(...vertices.map((p) => p[1]));
  const height = Math.max(...vertices.map((p) => p[2]))
    - Math.min(...vertices.map((p) => p[2]));
  assert.ok(width > 0 && width <= 13.5);
  assert.ok(height <= 15, "Buildings stay smaller than units and flags");
  const screenHexRadius = map.map.hex_size * scale;
  const outlineWidth = context.strokes[0].width;
  assert.ok(width + outlineWidth <= Math.sqrt(3) * screenHexRadius * 0.5,
    "Even extreme-overview town outlines stay within half a hex's width");
  assert.ok(height + outlineWidth <= screenHexRadius,
    "Dense towns cannot merge through a fixed minimum glyph or outline size");
  assert.equal(outlineWidth, Math.min(1, Math.min(5, screenHexRadius * 0.28) * 0.35));
  assert.equal(context.fills[0].color, "#5d4135");
  assert.equal(context.strokes[0].color, "#fff0c2");
  assert.equal(context.fillStyle, "initial-fill");
  assert.equal(context.strokeStyle, "initial-stroke");
  assert.equal(context.lineWidth, 7, "Town styles cannot leak into roads or railways");
}
for (const semantic of ["open", "unknown", "roadblock", "railway", "rough", "water", undefined]) {
  const context = recordingContext();
  map._drawTerrainSymbol(context, { row: 1, column: 12, terrain_semantic: semantic });
  assert.equal(context.fills.length, 0, "No invented towns on other terrain");
  assert.equal(context.strokes.length, 0);
}
assert.equal(JSON.stringify(map.hitTargets), hitTargets);
// All supported ground-cover glyphs use a bounded footprint. These
// checks include zoom far below the former fixed 1.5px forest/mud radius.
const colors = {
  forest: ["#326b35", null],
  settlement: ["#5d4135", "#fff0c2"],
  soft_ground: [null, "#665d3f"],
  small_river: [null, "#d9f1ff"],
};
function assertStylesRestored(context) {
  assert.equal(context.fillStyle, "initial-fill");
  assert.equal(context.strokeStyle, "initial-stroke");
  assert.equal(context.lineWidth, 7);
  assert.equal(context.lineJoin, "miter");
  assert.equal(context.lineCap, "butt");
  assert.equal(context.font, "initial-font");
  assert.equal(context.textAlign, "initial-align");
  assert.equal(context.textBaseline, "initial-baseline");
}
function assertInsideHex(context, center, screenRadius) {
  for (const draw of [...context.fills, ...context.strokes]) {
    const pad = (draw.width || 0) / 2;
    for (const [command, x, y] of pathPoints(relativePath(draw.path, center))) {
      assert.ok(Math.abs(x) + pad <= Math.sqrt(3) * screenRadius / 2 + 1e-8);
      assert.ok(Math.abs(y) + pad + (Math.abs(x) + pad) / Math.sqrt(3)
        <= screenRadius + 1e-8, "Glyph stroke stays inside its own hex");
    }
  }
}
for (const scale of [0.001, 0.005, 0.02, 0.06, 0.15, 0.5, 1, 4, 20]) {
  map.scale = scale;
  for (const semantic of Object.keys(colors)) {
    const cell = Object.freeze({ row: 12, column: 31, terrain_semantic: semantic });
    const center = map._screen(hexToPixel(cell.row, cell.column, map.map.hex_size));
    const context = recordingContext();
    map._drawTerrainSymbol(context, cell);
    assert.ok(context.strokes.length + context.fills.length > 0,
      `${semantic} has a distinguishing symbol`);
    const [fill, stroke] = colors[semantic];
    if (fill) assert.equal(context.fills[0].color, fill);
    if (stroke) assert.equal(context.strokes.at(-1).color, stroke);
    else assert.equal(context.strokes.length, 0, "v10 forest has no invented outlines");
    const vertices = [...context.fills, ...context.strokes]
      .flatMap((draw) => pathPoints(relativePath(draw.path, center)));
    const width = Math.max(...vertices.map((p) => p[1]))
      - Math.min(...vertices.map((p) => p[1]));
    const height = Math.max(...vertices.map((p) => p[2]))
      - Math.min(...vertices.map((p) => p[2]));
    const outline = Math.max(0, ...context.strokes.map((draw) => draw.width));
    const screenRadius = map.map.hex_size * scale;
    assert.ok(width + outline <= Math.sqrt(3) * screenRadius * 0.5 + 1e-8,
      `${semantic} remains within half a hex width, including contrast outline`);
    assert.ok(height + outline <= screenRadius + 1e-8);
    assertInsideHex(context, center, screenRadius);
    assertStylesRestored(context);
  }
  for (const semantic of ["open", "forest", "settlement", "unknown", "roadblock"]) {
    const context = recordingContext();
    const cell = Object.freeze({ row: 12, column: 31, terrain_semantic: semantic,
      roadblock: true });
    const center = map._screen(hexToPixel(cell.row, cell.column, map.map.hex_size));
    map._drawRoadblock(context, cell);
    assert.equal(context.fills.length, 1);
    assert.equal(context.fills[0].color, "#9b5b2d");
    assert.equal(context.strokes.length, 4, "Roadblock uses posts, crossbar and stripes");
    assertInsideHex(context, center, map.map.hex_size * scale);
    assertStylesRestored(context);
  }
}
for (const roadblock of [undefined, null, false, 0, 1, "true", "ROADBLOCK"]) {
  const context = recordingContext();
  map._drawRoadblock(context, { row: 1, column: 12, terrain_semantic: "unknown", roadblock });
  assert.equal(context.fills.length, 0, "Do not infer facilities from truthy values");
  assert.equal(context.strokes.length, 0);
}
const semanticBlock = recordingContext();
map._drawRoadblock(semanticBlock, { row: 1, column: 12, terrain_semantic: "roadblock" });
assert.equal(semanticBlock.fills.length, 1, "Explicit roadblock terrain needs no separate flag");

map.map.elevation = { minimum: 0, maximum: 70, levels: 8 };
map.scale = 2;
for (const elevation of [0, 70]) {
  for (const semantic of ["open", "forest", "settlement", "soft_ground", "water",
    "small_river", "roadblock", "unknown"]) {
    const cell = Object.freeze({ row: 12, column: 31, terrain_semantic: semantic,
      elevation, roads: [0, 0, 0, 0, 0, 0], rivers: [0, 0, 0, 0, 0, 0] });
    const context = recordingContext();
    map._drawCell(context, cell, true);
    assert.equal(context.fills[0].color, elevation === 0 ? "#f7e7b2" : "#332611",
      "All terrain retains the v10 elevation palette");
    const waterFill = context.fills.filter((draw) => draw.color === "#397fc0dd");
    assert.equal(waterFill.length, semantic === "water" ? 1 : 0,
      "Only confirmed large-water terrain receives the existing water fill");
    const center = map._screen(hexToPixel(cell.row, cell.column, map.map.hex_size));
    assert.equal(context.labels.length, 2, "Coordinates have a contrasting halo and true value");
    assert.equal(context.labels[0].color, "#fff0c2");
    assert.equal(context.labels[1].color, "#101010");
    assert.equal(context.labels[1].text, "(31,12)", "Coordinates are (column,row), not elevation");
    assert.equal(context.labels[1].y,
      center.y + Math.max(14, map.map.hex_size * map.scale * 0.46));
  }
}
for (const scale of [0.005, 0.5, 1, 1.3]) {
  map.scale = scale;
  const context = recordingContext();
  map._drawCoordinateLabel(context, { row: 1, column: 2, elevation: 70 });
  assert.equal(context.labels.length, 0, "Overview does not add coordinate clutter");
}
map.scale = 2;
for (const elevation of [null, undefined, "70", NaN, Infinity]) {
  const context = recordingContext();
  map._drawCoordinateLabel(context, { row: 1, column: 2, elevation });
  assert.equal(context.labels.at(-1).text, "(2,1)", "Coordinates do not depend on elevation");
}
const labelContext = recordingContext();
map._drawCoordinateLabel(labelContext, { row: 1, column: 2, elevation: 30 });
assertStylesRestored(labelContext);

// The close-up canopy exactly matches v10: two lower circles and one upper
// circle, not the v11 conifers. All offsets shrink at overview zoom.
map.scale = 4;
const forestCell = { row: 12, column: 31, terrain_semantic: "forest" };
const forestCenter = map._screen(hexToPixel(12, 31, map.map.hex_size));
const forestContext = recordingContext();
map._drawTerrainSymbol(forestContext, forestCell);
assert.deepEqual(forestContext.fills.map((draw) => relativePath(draw.path, forestCenter)), [
  [["A", -5, 1, 5]], [["A", 5, 2, 5]], [["A", 0, -5, 5]],
]);
assert.equal(forestContext.strokes.length, 0);
const lakeContext = recordingContext();
map._drawTerrainSymbol(lakeContext, { ...forestCell, terrain_semantic: "water" });
assert.equal(lakeContext.fills.length + lakeContext.strokes.length, 0,
  "Lakes retain their v10 flat blue cell fill, with no wave glyph");
assertStylesRestored(lakeContext);

// Both axes are explicit even at the origin, and missing elevation or a pos ID
// must not substitute a different number. Invalid coordinates are not invented.
for (const [row, column] of [[0, 0], [1, 2], [12, 31], [123, 456]]) {
  const context = recordingContext();
  map._drawCoordinateLabel(context, { row, column, position_id: "9999", elevation: 70 });
  assert.equal(context.labels.at(-1).text, `(${column},${row})`);
}
for (const [row, column] of [[null, 2], [1, null], [-1, 2], [1, -2],
  [1.5, 2], [1, "2"], [NaN, 2], [1, Infinity]]) {
  const context = recordingContext();
  map._drawCoordinateLabel(context, { row, column, elevation: 70 });
  assert.equal(context.labels.length, 0);
  assertStylesRestored(context);
}
for (const scale of [0.001, 0.5, 1, 1.5, 2, 3, 4, 20]) {
  map.scale = scale;
  for (const [row, column] of [[0, 0], [12, 31], [123, 456], [12345, 67890]]) {
    const context = recordingContext();
    map._drawCoordinateLabel(context, { row, column });
    assertStylesRestored(context);
    for (const label of context.labels) {
      const center = map._screen(hexToPixel(row, column, map.map.hex_size));
      const halfWidth = context.measureText(label.text).width / 2 + 1;
      const radius = map.map.hex_size * scale;
      assert.ok(halfWidth <= Math.sqrt(3) * radius / 2 - 1);
      assert.ok(Math.abs(label.y - center.y) + 6 + halfWidth / Math.sqrt(3)
        <= radius - 1 + 1e-8, "Coordinate text and halo remain within their own hex");
      assert.ok(label.y - center.y - 6 >= 8,
        "The label leaves the terrain/flag center clear");
    }
  }
}
map.scale = 2;
const longLabel = recordingContext();
map._drawCoordinateLabel(longLabel, { row: 123, column: 456 });
assert.equal(longLabel.labels.length, 0, "Long labels wait for sufficient zoom");

const coordinateCell = { row: 12, column: 31 };
const unchangedUnits = Object.freeze([{ row: 12, column: 31, operator_id: 7 }]);
map.frame = { viewer_units: unchangedUnits };
map._prepareCoordinateLabels();
const occupiedLabel = recordingContext();
map._drawCoordinateLabel(occupiedLabel, coordinateCell);
assert.equal(occupiedLabel.labels.length, 0, "Coordinates never paint through a unit");
assert.equal(map.frame.viewer_units, unchangedUnits, "Label clearance does not alter units");
map.scale = 5;
map._prepareCoordinateLabels();
const occupiedCloseup = recordingContext();
map._drawCoordinateLabel(occupiedCloseup, coordinateCell);
assert.equal(occupiedCloseup.labels.at(-1).text, "(31,12)",
  "Coordinates return when enough space exists below the unit");
map.scale = 2;
map.frame = { viewer_units: [{ row: 12, column: 30, operator_id: 7,
  movement_target_hex: 1231, movement_progress: 1 }] };
map._prepareCoordinateLabels();
const movingLabel = recordingContext();
map._drawCoordinateLabel(movingLabel, coordinateCell);
assert.equal(movingLabel.labels.length, 0, "Moving units also reserve their display position");
map.frame = { viewer_units: [{ ...unchangedUnits[0], map_visible: false }] };
map._prepareCoordinateLabels();
const hiddenUnitLabel = recordingContext();
map._drawCoordinateLabel(hiddenUnitLabel, coordinateCell);
assert.equal(hiddenUnitLabel.labels.at(-1).text, "(31,12)",
  "An invisible unit must not leak its position by hiding coordinates");
map.frame = null;
map._prepareCoordinateLabels();
assert.deepEqual(map.coordinateLabelUnitBounds, [], "No stale keepouts remain after a frame clears");
map.frame = { viewer_units: unchangedUnits };
map.scale = 0.5;
map._prepareCoordinateLabels();
assert.deepEqual(map.coordinateLabelUnitBounds, [], "Overview skips unit clearance setup");
map.scale = 2;
map.frame = null;
assert.equal(JSON.stringify(map.hitTargets), hitTargets, "Labels never change unit hit targets");

const originalMap = map.map;
let preparations = 0;
let drawnCells = 0;
map.map = { ...originalMap, cells: [coordinateCell, coordinateCell, coordinateCell] };
map._prepareCoordinateLabels = function prepareLabels() {
  preparations += 1;
  MapRenderer.prototype._prepareCoordinateLabels.call(this);
};
map._drawCell = () => { drawnCells += 1; };
map.frame = { viewer_units: unchangedUnits };
map._drawMap(recordingContext());
assert.equal(preparations, 1, "Unit clearance is prepared once per render, not per cell");
assert.equal(drawnCells, 3);
assert.equal(map.coordinateLabelUnitBounds.length, 1);
map.frame = { viewer_units: [{ ...unchangedUnits[0], map_visible: false }] };
map._drawMap(recordingContext());
assert.equal(preparations, 2);
assert.deepEqual(map.coordinateLabelUnitBounds, [], "The next render reflects visibility changes");
delete map._prepareCoordinateLabels;
delete map._drawCell;
map.map = originalMap;
map.frame = null;

// The public river-valley fixture exercises real soft-ground cells absent from
// Map123, rather than pretending Map123 contains every supported terrain.
const riverValley = JSON.parse(fs.readFileSync(
  path.join(root, "examples/public_river_valley_map.json"), "utf8"));
const softGround = riverValley.map_data.flat().filter((cell) => cell?.cond === 3);
assert.equal(softGround.length, 91);
for (const source of softGround) {
  const context = recordingContext();
  map._drawTerrainSymbol(context, { row: Math.floor(source.pos / 100),
    column: source.pos % 100, terrain_semantic: "soft_ground" });
  assert.equal(context.strokes.length, 2);
  assert.equal(context.strokes[0].path.filter((point) => point[0] === "M").length, 6);
}

// The static roadblock count is additive metadata, not a replacement terrain.
assert.deepEqual(summarizeMapLayers({ cells: [
  { terrain_semantic: "forest", roadblock: true },
  { terrain_semantic: "roadblock" },
  { terrain_semantic: "roadblock", roadblock: true },
  { terrain_semantic: "unknown", roadblock: "true" },
] }), {
  configured_cell_count: 4, elevation_cell_count: 0,
  terrain_counts: { forest: 1, roadblock: 2, unknown: 1 },
  road_edge_count: 0, road_types: [], roadblock_cell_count: 3,
  river_edge_count: 0, river_types: [],
});
const hoverCell = { row: 1, column: 2, position_id: "102", terrain_semantic: "forest",
  terrain_code: null, terrain_source: "FOREST", roadblock: true, elevation: 20 };
map.map.cells = [hoverCell];
map.map.bounds = { min_row: 1, max_row: 1, min_column: 2, max_column: 2 };
map.cellByKey = new Map([["1:2", hoverCell]]);
const hover = map._hexAtScreenPoint(map._screen(hexToPixel(1, 2, map.map.hex_size)));
assert.equal(hover.terrain_semantic, "forest");
assert.equal(hover.terrain_source, "FOREST");
assert.equal(hover.roadblock, true);
assert.equal(JSON.stringify(map.hitTargets), hitTargets);
// Execute each real hover formatter, without its page boot/network side effects.
// Named Native terrain and independent static facilities must survive all three
// shared-renderer frontends, while Source integer codes keep their old display.
function functionSource(source, name) {
  const start = source.indexOf(`function ${name}(`);
  assert.ok(start >= 0, `${name} exists`);
  const end = source.indexOf("\n}\n", start);
  assert.ok(end > start);
  return source.slice(start, end + 3);
}
const replaySource = fs.readFileSync(path.join(root,
  "src/wargame_native_engine/viewer/static/app.js"), "utf8");
for (const filename of ["app.js", "live_app.js", "dual_control.js"]) {
  const source = fs.readFileSync(path.join(root,
    "src/wargame_native_engine/viewer/static", filename), "utf8");
  const target = { textContent: "" };
  const sandbox = { $: () => target, window: { ReplayMapRenderer: { formatEdgeFacts } } };
  vm.createContext(sandbox);
  vm.runInContext(functionSource(replaySource, "formatNumber") + "\n"
    + functionSource(source, "renderHexInfo"), sandbox);
  for (const [terrain_code, terrain_source, expected] of [
    [0, "FOREST", "0"], [3, null, "3"], [5, undefined, "5"],
    [null, "SOFT_GROUND", "SOFT_GROUND"], [undefined, "FOREST", "FOREST"],
    [null, "3", "3"], [99, null, "99"], [null, "not-a-terrain", "not-a-terrain"],
    [null, null, "UNKNOWN"],
  ]) {
    for (const roadblock of [true, false, undefined, null, 1, "true"]) {
      const cell = { row: 1, column: 2, position_id: "102", terrain_semantic: "soft_ground",
        terrain_code, terrain_source, roadblock, elevation: 20, terrain_level: 2,
        roads: [1, 0, 0, 0, 0, 0], rivers: [0, 2, 0, 0, 0, 0],
        units: [{ operator_id: 7 }] };
      sandbox.renderHexInfo(Object.freeze(cell), "hover");
      assert.ok(target.textContent.includes(`terrain=soft_ground (${expected})`), filename);
      assert.equal(target.textContent.includes("roadblock=yes"), roadblock === true, filename);
      assert.ok(target.textContent.includes("row=1 column=2 position=102"));
      assert.ok(target.textContent.includes("roads=E(d0)=type1"));
      assert.ok(target.textContent.includes("rivers=NE(d1)=type2"));
      assert.ok(target.textContent.includes("units=7"));
    }
  }
  sandbox.renderHexInfo(null, "hover");
  assert.ok(!target.textContent.includes("roadblock=yes"), "Clearing hover clears facilities");
}
console.log("Map123 flags, v10 forest/lakes/towns/palette, coordinate labels and clearance, zoom bounds, roadblocks and all hover formats passed.");
