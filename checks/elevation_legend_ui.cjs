"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const root = path.resolve(__dirname, "..");
const staticRoot = path.join(root, "src/wargame_native_engine/viewer/static");
const { elevationColor, elevationLegend, renderElevationLegend } = require(
  path.join(staticRoot, "map_renderer.js"),
);
const native = JSON.parse(fs.readFileSync(
  path.join(root, "examples/map123/scenario.json"), "utf8",
)).map;
const elevations = native.hexes.map((cell) => cell.elev).filter(Number.isFinite);
const map = Object.freeze({
  elevation_kind: native.elevation_kind,
  elevation_unit_m: native.elevation_unit_m,
  elevation: Object.freeze({ mode: "episode", minimum: Math.min(...elevations),
    maximum: Math.max(...elevations), levels: 7 }),
});
const snapshot = JSON.stringify(map);
const legend = elevationLegend(map);
assert.equal(legend.title, "高程");
assert.equal(legend.minimum, "最低 0 m");
assert.equal(legend.maximum, "最高 70 m");
assert.equal(legend.step, "步长 10 m");
assert.deepEqual(legend.bands.map((band) => band.color), [
  "#f7e7b2", "#e7c96f", "#d1a64a", "#8d6124", "#6a431c", "#4a3217", "#332611",
]);
assert.equal(legend.bands[0].start, 0);
assert.equal(legend.bands.at(-1).end, 100);
assert.equal(elevationColor(0, map), legend.bands[0].color);
assert.equal(elevationColor(70, map), legend.bands.at(-1).color);

// The declared metre step is independent of the display bin count/range.
for (const levels of [2, 7, 8, 100, 0, undefined]) {
  const value = { ...map, elevation_unit_m: 3,
    elevation: { ...map.elevation, minimum: -35, maximum: 65, levels } };
  const result = elevationLegend(value);
  assert.equal(result.step, "步长 3 m");
  for (const band of result.bands) {
    const midpoint = -35 + (band.start + band.end) / 2;
    assert.equal(band.color, elevationColor(midpoint, value));
  }
}
const levelMap = { ...map, elevation_kind: "LEVEL",
  elevation: { ...map.elevation, minimum: 2, maximum: 7 } };
const levelLegend = elevationLegend(levelMap);
assert.equal(levelLegend.minimum, "最低 2 级");
assert.equal(levelLegend.maximum, "最高 7 级");
assert.equal(levelLegend.step, "步长 10 m/级");
assert.equal(elevationLegend({ ...levelMap, elevation_unit_m: null }).step, "步长 UNKNOWN");
for (const step of [null, undefined, NaN, Infinity, 0, -10, "10", true]) {
  assert.equal(elevationLegend({ ...map, elevation_unit_m: step }).step, "步长 UNKNOWN");
}
const unknown = elevationLegend({ elevation: map.elevation,
  cells: [{ elevation: 0 }, { elevation: 10 }, { elevation: 70 }] });
assert.equal(unknown.title, "高程（单位 UNKNOWN）");
assert.equal(unknown.minimum, "最低 0");
assert.equal(unknown.maximum, "最高 70");
assert.equal(unknown.step, "步长 UNKNOWN", "Do not derive steps from cell differences");
for (const elevation of [undefined, { mode: "unavailable", minimum: 0, maximum: 70 },
  { minimum: null, maximum: null }, { minimum: 70, maximum: 0 },
  { minimum: "0", maximum: 70 }, { minimum: 0, maximum: Infinity },
  { minimum: NaN, maximum: 70 }]) {
  const result = elevationLegend({ ...map, elevation });
  assert.equal(result.available, false);
  assert.deepEqual(result.bands, []);
  assert.equal(result.minimum, "高程 UNKNOWN");
}
const flat = { ...map, elevation: { ...map.elevation, minimum: 30, maximum: 30 } };
const flatLegend = elevationLegend(flat);
assert.equal(flatLegend.minimum, "最低 30 m");
assert.equal(flatLegend.maximum, "最高 30 m");
assert.deepEqual(flatLegend.bands, [{ color: elevationColor(30, flat), start: 0, end: 100 }]);
assert.equal(JSON.stringify(map), snapshot, "Presentation must not mutate map facts");

const document = {
  createElement(tagName) {
    return { tagName, style: {}, attributes: {},
      setAttribute(name, value) { this.attributes[name] = value; } };
  },
};
const element = { ownerDocument: document, children: [],
  replaceChildren(...children) { this.children = children; } };
const labels = () => element.children.map((node) => node.textContent || "").join(" · ");
const bars = () => element.children.filter((node) => node.className === "elevation-colorbar");
renderElevationLegend(element, map);
assert.match(labels(), /最低 0 m.*最高 70 m.*步长 10 m/);
assert.match(labels(), /坐标：\(横,纵\)，横=列、纵=行/);
assert.equal(bars().length, 1);
assert.match(bars()[0].style.background, /^linear-gradient\(90deg, #f7e7b2 0%/);
assert.match(bars()[0].style.background, /#332611 100%\)$/);
assert.equal(bars()[0].attributes["aria-hidden"], "true");
renderElevationLegend(element, flat);
assert.equal(bars().length, 1, "Repeated map installs replace, not duplicate, the legend");
assert.equal(bars()[0].style.background, elevationColor(30, flat));
renderElevationLegend(element, { mode: "coordinate-fallback" });
assert.equal(bars().length, 0, "Missing elevation clears the previous map's colorbar");
assert.match(labels(), /高程 UNKNOWN.*步长 UNKNOWN/);
assert.doesNotMatch(labels(), /最低|最高|\bNaN\b|Infinity/);
renderElevationLegend(null, map);

for (const [html, script] of [["index.html", "app.js"],
  ["live_index.html", "live_app.js"], ["dual_control_index.html", "dual_control.js"]]) {
  const page = fs.readFileSync(path.join(staticRoot, html), "utf8");
  const code = fs.readFileSync(path.join(staticRoot, script), "utf8");
  assert.equal((page.match(/id="elevation-legend"/g) || []).length, 1);
  assert.equal((code.match(/ReplayMapRenderer\.renderElevationLegend\(/g) || []).length, 1);
  assert.doesNotMatch(page, /class="elevation-swatch"/);
  assert.doesNotMatch(code, /\$\("map-elevation"\)/);
}
console.log("Shared elevation legend: palette, metadata, LEVEL, unknown, flat, replacement and all pages pass");
