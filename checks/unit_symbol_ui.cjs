"use strict";

// Map123 is the source of the category examples, not a hand-made type mapping.
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const root = path.resolve(__dirname, "..");
const staticRoot = path.join(root, "src/wargame_native_engine/viewer/static");
const resolver = require(path.join(staticRoot, "unit_symbol_resolver.js"));
const { UnitSymbolCache, UnitSymbolRenderer } = require(
  path.join(staticRoot, "unit_symbol_renderer.js"),
);
const { MapRenderer, unitDisplayMetrics } = require(path.join(staticRoot, "map_renderer.js"));
const registry = JSON.parse(fs.readFileSync(
  path.join(root, "config/source_compat/operator_registry_v1.json"), "utf8",
));
const scenario = JSON.parse(fs.readFileSync(
  path.join(root, "examples/map123/scenario.json"), "utf8",
));
resolver.installRegistry(registry);

async function main() {
  const categories = new Set();
  for (const operator of scenario.operators) {
    const source = operator.raw_attributes.source_frame;
    // Only already-projected identity codes reach the resolver, never raw state.
    const identity = { type_code: source.type, subtype_code: source.sub_type, name: source.name };
    const symbol = resolver.resolveOperator(identity);
    assert.equal(symbol.status, "MAPPED");
    assert.equal(symbol.display_name, source.name);
    assert.deepEqual(symbol, resolver.resolveOperator({ ...identity, side: "blue" }));
    categories.add(symbol.key);
    for (const layer of resolver.resolveSvgLayers(symbol).layers) {
      const svg = fs.readFileSync(path.join(staticRoot, layer), "utf8");
      assert.match(svg, /viewBox="0 0 128 128"/);
      assert.doesNotMatch(svg, /<script|<image|https?:\/\/(?!www\.w3\.org)/);
    }
  }
  assert.deepEqual([...categories].sort(), ["1_2", "2_0", "2_1", "2_3", "3_5"]);
  const unknownVehicle = resolver.resolveOperator({ type_code: 2 });
  assert.equal(unknownVehicle.status, "UNKNOWN");
  assert.equal(unknownVehicle.base_symbol, "unknown_vehicle");
  assert.equal(resolver.resolveOperator({ name: "重型坦克", icon_key: "tank" }).key, "UNKNOWN");
  assert.equal(resolver.resolveOperator({ type_code: 2, subtype_code: 999 }).status, "UNKNOWN");
  assert.deepEqual(resolver.resolveSvgLayers({
    status: "UNKNOWN", base_symbol: "tank", modifiers: ["armor_heavy"],
  }).layers, ["assets/unit_symbols/base/unknown.svg"]);

  // A synthetic *explicit registry* exercises future medium platforms without
  // inventing an actual Source subtype for the bundled original Map123 data.
  const synthetic = resolver.create({ schema: "operator-registry/v1", operators: {
    "99_0": { identity: { type_code: 99, subtype_code: 0 }, display_name: "中型测试坦克",
      symbol: { base: "tank", modifiers: ["armor_medium"] } },
    "99_1": { identity: { type_code: 99, subtype_code: 1 }, display_name: "中型测试战车",
      symbol: { base: "armored_vehicle", modifiers: ["armor_medium"] } },
  } });
  for (const [subtype, heavySubtype, base] of [[0, 0, "tank"], [1, 1, "armored_vehicle"]]) {
    const medium = synthetic.resolveOperator({ type_code: 99, subtype_code: subtype });
    const heavy = resolver.resolveOperator({ type_code: 2, subtype_code: heavySubtype, name: heavySubtype === 0 ? "重型坦克" : "重型战车" });
    const mediumLayers = resolver.resolveSvgLayers(medium).layers;
    const heavyLayers = resolver.resolveSvgLayers(heavy).layers;
    assert.equal(mediumLayers[0], `assets/unit_symbols/base/${base}_medium.svg`);
    assert.equal(heavyLayers[0], `assets/unit_symbols/base/${base}_heavy.svg`);
    assert.notEqual(fs.readFileSync(path.join(staticRoot, mediumLayers[0]), "utf8"),
      fs.readFileSync(path.join(staticRoot, heavyLayers[0]), "utf8"));
    for (const layer of [...mediumLayers, ...heavyLayers]) {
      assert.match(fs.readFileSync(path.join(staticRoot, layer), "utf8"), /viewBox="0 0 128 128"/);
    }
  }
  assert.equal(resolver.resolveOperator({ type_code: 99, subtype_code: 0 }).status, "UNKNOWN");
  assert.equal(resolver.resolveSvgLayers({ status: "MAPPED", base_symbol: "tank",
    modifiers: ["armor_heavy", "armor_medium"] }).layers[0], "assets/unit_symbols/base/tank.svg");

  for (const [name, weight] of [["重型坦克", "heavy"], ["中型坦克", "medium"], ["轻型坦克", "light"]]) {
    const symbol = resolver.resolveOperator({type_code: 2, sub_type_code: 0, name});
    assert.equal(symbol.display_name, name);
    assert.deepEqual(symbol.modifiers, [`armor_${weight}`]);
    const layers = resolver.resolveSvgLayers(symbol).layers;
    for (const layer of layers) assert.ok(fs.existsSync(path.join(staticRoot, layer)));
  }
  for (const name of [undefined, "未知坦克", "非重型坦克", "重型坦克 1"]) {
    const symbol = resolver.resolveOperator({type_code: 2, sub_type_code: 0, name, armor: 999});
    assert.deepEqual(symbol.modifiers, []);
    assert.equal(symbol.display_name, "坦克");
  }
  assert.equal(resolver.resolveOperator({type_code: 2, sub_type_code: 999, name: "重型坦克"}).status, "UNKNOWN");

  for (const [name, weight] of [["重型战车", "heavy"], ["中型战车", "medium"], ["轻型战车", "light"]]) {
    const symbol = resolver.resolveOperator({type_code: 2, sub_type_code: 1, name});
    assert.equal(symbol.display_name, name);
    assert.deepEqual(symbol.modifiers, [`armor_${weight}`]);
    for (const layer of resolver.resolveSvgLayers(symbol).layers) assert.ok(fs.existsSync(path.join(staticRoot, layer)));
  }
  for (const name of [undefined, "未知战车", "非重型战车", "重型坦克", "<script>alert(1)</script>"]) {
    const symbol = resolver.resolveOperator({type_code: 2, sub_type_code: 1, name});
    assert.deepEqual(symbol.modifiers, []);
    assert.equal(symbol.display_name, "战车");
  }

  // UNKNOWN is semantic identity status, independent of alive/destroyed state.
  const map = Object.create(MapRenderer.prototype);
  map.selectedUnitIds = new Set();
  map.hitTargets = [];
  map.unitSymbolResolver = resolver;
  map.setActionTargets();
  let delivered;
  map.unitSymbolRenderer = { drawUnitSymbol(_context, _x, _y, descriptor) {
    delivered = descriptor;
    return true;
  } };
  const labels = [];
  const rings = [];
  const context = {
    save() {}, restore() {}, translate() {}, beginPath() {},
    arc(_x, _y, radius) { this.radius = radius; },
    fill() {}, stroke() { rings.push({ radius: this.radius, color: this.strokeStyle }); },
    moveTo() {}, lineTo() {},
    fillText(text) { labels.push(text); },
  };
  map._drawUnit(context, { operator_id: 101, type_code: 2, side: "blue" }, 0, 0);
  assert.equal(delivered.status, "UNKNOWN");
  assert.equal(delivered.unit_state, "alive");
  map.selectedUnitIds.add("102");
  map._drawUnit(context, {
    operator_id: 102, type_code: 2, subtype_code: 0, side: "blue", name: "重型坦克",
  }, 0, 0);
  assert.equal(labels.at(-1), "102 · 重型坦克");

  // Actual screen-space hex size drives LOD, not world coordinates or IDs.
  const overview = unitDisplayMetrics(10, 0.2);
  const closeup = unitDisplayMetrics(10, 3);
  const focus = unitDisplayMetrics(10, 0.2, true);
  assert.equal(overview.showGlyph, false);
  assert.equal(overview.showDetails, false);
  assert.equal(overview.symbolSize, 4);
  assert.equal(overview.hitRadius, 3);
  assert.equal(closeup.symbolSize, 34);
  assert.equal(closeup.showDetails, true);
  assert.equal(focus.symbolSize, overview.symbolSize * 1.12);
  assert.equal(focus.showDetails, false);
  assert.equal(focus.showGlyph, false);
  assert.equal(focus.showLabel, true);
  for (const zoom of [0.01, 0.2, 0.8, 1, 2, 3, 10]) {
    const normal = unitDisplayMetrics(10, zoom);
    const selected = unitDisplayMetrics(10, zoom, true);
    assert.ok(Math.abs(selected.symbolSize / normal.symbolSize - 1.12) < 1e-12);
    assert.equal(selected.showGlyph, normal.showGlyph, "Selection must preserve LOD");
    assert.equal(selected.showDetails, normal.showDetails);
  }
  assert.ok(overview.stackScale < closeup.stackScale);
  assert.deepEqual(unitDisplayMetrics(20, 0.1), overview);
  map.map = { hex_size: 10 };
  map.scale = 0.2;
  const healthBars = [];
  context.fillRect = (...args) => healthBars.push(args);
  const unit = {
    operator_id: 103, type_code: 2, subtype_code: 0, side: "blue",
    health_ratio: 1, stopped: true,
  };
  const labelsBefore = labels.length;
  delivered = null;
  map._drawUnit(context, unit, 10, 20);
  assert.equal(delivered, null, "Overview dots must not load unreadable SVGs");
  assert.equal(labels.length, labelsBefore);
  assert.equal(healthBars.length, 0);
  assert.equal(map.hitTargets.at(-1).radius, overview.hitRadius);
  map.scale = 3;
  map._drawUnit(context, unit, 10, 20);
  assert.equal(labels.at(-1), "103");
  assert.equal(healthBars.length, 3);
  map.scale = 0.2;
  map.selectedUnitIds.add("103");
  map._drawUnit(context, unit, 10, 20);
  assert.equal(labels.at(-1), "103 · 坦克");
  assert.equal(map.hitTargets.at(-1).radius, focus.hitRadius);
  assert.equal(rings.at(-1).color, "#ffe15a");
  assert.ok(rings.at(-1).radius < 3, "Overview selection must not become a giant marker");

  const order = [];
  const originalDraw = map._drawUnit;
  map._drawUnit = (_context, value) => order.push(value.operator_id);
  map._screen = (point) => point;
  map._drawUnits(context, [
    { operator_id: 103, row: 1, column: 1 },
    { operator_id: 104, row: 1, column: 2 },
  ]);
  assert.deepEqual(order, [104, 103], "Focused glyphs must draw above overview dots");
  map._drawUnit = originalDraw;

  // Shared immutable layer images load once per side, even for multiple units.
  let fetches = 0;
  let revoked = 0;
  class ImageStub {
    set src(_value) { queueMicrotask(() => this.onload()); }
  }
  const cache = new UnitSymbolCache({
    fetchImpl: async () => {
      fetches += 1;
      return { ok: true, text: async () => '<svg stroke="currentColor"/>' };
    },
    ImageClass: ImageStub,
    BlobClass: class {},
    URLApi: { createObjectURL: () => "blob:test", revokeObjectURL() { revoked += 1; } },
  });
  const tank = { ...resolver.resolveOperator({ type_code: 2, subtype_code: 0, name: "重型坦克" }), side: "red" };
  const first = cache.get(tank);
  assert.equal(first, cache.get(tank));
  await new Promise((resolve) => setImmediate(resolve));
  assert.equal(first.status, "READY");
  assert.equal(fetches, 2);
  assert.equal(revoked, 2);
  assert.match(cache.symbolKey({ ...tank, unit_state: "destroyed" }), /^DESTROYED:/);
  const drawn = [];
  const renderer = new UnitSymbolRenderer({ cache });
  assert.equal(renderer.drawUnitSymbol({
    save() {}, restore() {}, translate() {}, drawImage(...args) { drawn.push(args); },
  }, 0, 0, tank, 34), true);
  assert.equal(drawn.length, 2);
  assert.equal(renderer.drawUnitSymbol(context, 0, 0, tank, -1), false);
  console.log("Map123 unit symbol and rendering checks passed (49 operators, 5 categories).");
}
main().catch((error) => { console.error(error); process.exitCode = 1; });
