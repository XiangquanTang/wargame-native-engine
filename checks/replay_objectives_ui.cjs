"use strict";
// Real shared renderer with a Canvas command recorder, not browser pixel QA.
// Input may be projected public test frames or a read-only user-archive probe.
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const { MapRenderer, hexToPixel } = require(path.resolve(__dirname,
  "../src/wargame_native_engine/viewer/static/map_renderer.js"));
const fixture = JSON.parse(fs.readFileSync(process.argv[2], "utf8"));
const colors = { "-1": "#d7c66b", 0: "#df3d45", 1: "#357edb" };

function recordingContext() {
  const stack = [];
  const context = {
    draws: [], points: [], tx: 0, ty: 0, fillStyle: "", strokeStyle: "",
    save() { stack.push([this.tx, this.ty, this.fillStyle, this.strokeStyle]); },
    restore() { [this.tx, this.ty, this.fillStyle, this.strokeStyle] = stack.pop(); },
    translate(x, y) { this.tx += x; this.ty += y; },
    beginPath() { this.points = []; },
    moveTo(x, y) { this.points.push(["M", x + this.tx, y + this.ty]); },
    lineTo(x, y) { this.points.push(["L", x + this.tx, y + this.ty]); },
    arc(x, y, r) { this.points.push(["A", x + this.tx, y + this.ty, r]); },
    rect(x, y, w, h) { this.points.push(["R", x + this.tx, y + this.ty, w, h]); },
    closePath() { this.points.push(["Z"]); },
    fill() { this.draws.push({ kind: "fill", color: this.fillStyle, points: this.points.slice() }); },
    stroke() { this.draws.push({ kind: "stroke", color: this.strokeStyle, points: this.points.slice() }); },
    fillText() { this.draws.push({ kind: "text" }); },
    fillRect() { this.draws.push({ kind: "rect", color: this.fillStyle }); },
    clearRect() { this.draws = []; },
    measureText(text) { return { width: String(text).length * 6 }; },
    setLineDash() {},
  };
  return context;
}

const renderer = Object.create(MapRenderer.prototype);
renderer.map = fixture.map;
renderer.canvas = { clientWidth: 1600, clientHeight: 1000 };
renderer.context = recordingContext();
renderer.cellByKey = new Map();
renderer.hasFitted = true;
renderer.unitSymbolResolver = null;
renderer.unitSymbolRenderer = null;
renderer.indirectFireAreas = [];
renderer._resizeCanvas = () => false;
// Terrain is unchanged; exercise the real dynamic pass including static city
// overlay, area selection, unit plates/labels/hit targets and selected hex.
renderer._drawMap = () => {};
renderer.setTacticalOverlay();
renderer.setActionTargets();
const triangle = [["M", -3, -13], ["L", 10, -9], ["L", -3, -4], ["Z"]];
const square = [["M", -3, -13], ["L", 10, -13], ["L", 10, 0], ["L", -3, 0], ["Z"]];
function relative(points, center) {
  return points.map(([kind, x, y, ...rest]) => kind === "Z" ? [kind]
    : [kind, Math.round((x - center.x) * 1e8) / 1e8,
      Math.round((y - center.y) * 1e8) / 1e8, ...rest]);
}
let renders = 0, flags = 0, coincidentUnits = 0;
const original = JSON.stringify(fixture);
// Forward polling, duplicate redraw, backwards seek and changing perspectives
// all repaint from the supplied frame, never the static scenario's owner.
const sequence = [...fixture.frames, fixture.frames.at(-1), ...fixture.frames.slice().reverse()];
for (const scale of [0.25, 1, 4]) {
  renderer.scale = scale;
  renderer.offsetX = 12;
  renderer.offsetY = -8;
  for (const frame of sequence) {
    assert(frame.cities.length > 0, "Fixture must include recorded objective evidence");
    const first = frame.cities[0];
    renderer.selectedHex = { row: Math.floor(first.coord / 100), column: first.coord % 100 };
    renderer.hoveredHexKey = `${renderer.selectedHex.row}:${renderer.selectedHex.column}`;
    renderer.setActionTargets({ preview_area_position: first.coord });
    const selected = frame.viewer_units.find(unit => unit.row === renderer.selectedHex.row
      && unit.column === renderer.selectedHex.column && unit.map_visible !== false);
    renderer.render(frame, selected?.operator_id ?? null);
    const draws = renderer.context.draws;
    const fills = draws.filter(draw => draw.kind === "fill");
    const flagFills = fills.slice(-frame.cities.length);
    assert.equal(flagFills.length, frame.cities.length);
    frame.cities.forEach((city, index) => {
      const center = renderer._screen(hexToPixel(Math.floor(city.coord / 100), city.coord % 100,
        renderer.map.hex_size));
      const flag = flagFills[index];
      assert.equal(flag.color, colors[city.flag] ?? colors[-1]);
      assert.deepEqual(relative(flag.points, center), city.type === 0 ? square : triangle,
        "The final fill layer must be current-frame main/secondary flags, after units");
      const flagIndex = draws.indexOf(flag);
      assert(draws.slice(flagIndex + 1).every(draw => draw.kind !== "rect" && draw.kind !== "text"),
        "Unit plates, health bars and labels cannot paint over flags");
      const plate = draws.find(draw => draw.kind === "fill"
        && draw.color === "rgba(15,23,42,0.92)"
        && draw.points.some(([kind, x, y]) => kind === "A"
          && Math.abs(x - center.x) < 1e-8 && Math.abs(y - center.y) < 1e-8));
      if (plate) {
        coincidentUnits++;
        assert(draws.indexOf(plate) < flagIndex);
        assert(flag.points.every(([kind, , y]) => kind === "Z" || y <= center.y),
          "Flag cloth leaves the lower half of the unit visible");
        assert(renderer._unitAtScreenPoint(center), "Flags must not consume unit hit targets");
      }
      flags++;
    });
    assert.equal(draws.at(-1).kind, "stroke", "Selected hex adds an outline, not an occluding fill");
    renders++;
  }
}
assert.equal(JSON.stringify(fixture), original, "Drawing never mutates the recorded frame");

async function checkPlayerSeeks() {
  const byIndex = new Map();
  for (const frame of fixture.frames) {
    if (!byIndex.has(frame.frame_index)) byIndex.set(frame.frame_index, frame);
  }
  const indices = [...byIndex.keys()].sort((a, b) => a - b);
  const elements = new Map(), timers = new Map();
  let timerId = 0, seeks = 0;
  const sandbox = { console, AbortController, objectiveRenderer: renderer, indices,
    document: { getElementById(id) {
      if (!elements.has(id)) elements.set(id, { textContent: "", setAttribute() {} });
      return elements.get(id);
    } },
    ViewerUrls: { resolve: route => route },
    window: { setTimeout(callback) { timers.set(++timerId, callback); return timerId; },
      clearTimeout(id) { timers.delete(id); } },
    fetch: async route => {
      const match = route.match(/^\/api\/frame-bundle\/(\d+)/);
      assert(match, `Unexpected route ${route}`);
      const frame = byIndex.get(Number(match[1]));
      assert(frame);
      return { ok: true, json: async () => ({ frame, analysis: null }) };
    },
  };
  vm.createContext(sandbox);
  const app = fs.readFileSync(path.resolve(__dirname,
    "../src/wargame_native_engine/viewer/static/app.js"), "utf8");
  // Load unmodified navigation/render functions, excluding only page bootstrap.
  vm.runInContext(app.slice(0, app.indexOf("async function initialize()")), sandbox);
  const run = code => vm.runInContext(code, sandbox);
  run(`mapRenderer = objectiveRenderer;
    configureFrameSequence({frame_count: indices.length, frame_indices: indices});
    renderFactors = () => {};`);
  function check(index) {
    const expected = byIndex.get(index);
    assert.equal(run("currentFrameIndex"), index);
    assert.equal(renderer.frame, expected);
    assert.deepEqual(renderer.context.draws.filter(draw => draw.kind === "fill")
      .slice(-expected.cities.length).map(draw => draw.color),
    expected.cities.map(city => colors[city.flag] ?? colors[-1]),
    "App commit, cached reverse seek and playback must repaint this frame's owner");
    seeks++;
  }
  for (const index of [...indices, ...indices.slice().reverse(), indices.at(-1)]) {
    assert.equal(await run(`setCurrentFrameIndex(${index})`), true);
    check(index);
  }
  await run("startPlayback()");
  check(indices[0]);
  for (let ordinal = 1; ordinal < indices.length; ordinal++) {
    const tick = timers.get(run("playbackTimer"));
    assert(tick);
    await tick();
    check(indices[ordinal]);
  }
  run("pausePlayback()");
  assert.equal(JSON.stringify(fixture), original);
  console.log(JSON.stringify({ renders, flags, coincident_units: coincidentUnits,
    player_seeks_and_ticks: seeks, qa: "Canvas command trace; not browser pixel QA" }));
}
checkPlayerSeeks().catch(error => { console.error(error); process.exitCode = 1; });
