"use strict";

// Exercise the actual controller functions without a browser or extra packages.
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

const staticRoot = path.join(
  __dirname, "../src/wargame_native_engine/viewer/static",
);
const elements = new Map();
const sandbox = {
  URLSearchParams,
  performance: { now: () => 0 },
  crypto: { randomUUID: () => "ui-test" },
  location: { search: "?token=ui-test" },
  addEventListener() {},
  setInterval() {},
  document: {
    getElementById(id) {
      if (!elements.has(id)) {
        elements.set(id, { textContent: "", addEventListener() {} });
      }
      return elements.get(id);
    },
  },
};
sandbox.window = sandbox;
vm.createContext(sandbox);
vm.runInContext(
  fs.readFileSync(path.join(staticRoot, "capability_ui.js"), "utf8"),
  sandbox,
);
// The final declarations replace only automatic refresh and DOM rendering.
// Selection, target projection, map rendering, and preview request logic stay real.
vm.runInContext(
  fs.readFileSync(path.join(staticRoot, "dual_control.js"), "utf8")
    + "\nfunction refresh() {}\nfunction render() {}\n",
  sandbox,
);
const run = (code) => vm.runInContext(code, sandbox);
const read = (code) => JSON.parse(run(`JSON.stringify(${code})`));

async function main() {
  run(`
    current = {
      side: "red",
      product_status: { can_operate: true },
      scene: {
        frame: {
          viewer_units: [
            { operator_id: 1, side: "red", category: "operator" },
            { operator_id: 2, side: "red", category: "operator" },
            { operator_id: 3, side: "red", category: "operator" },
            { operator_id: 10, side: "blue", category: "operator" },
            { operator_id: 11, side: "blue", category: "operator" },
          ],
        },
      },
    };
    capabilities = {
      capabilities: [
        { capability_type: "DIRECT_FIRE", operation_type: "SHOOT",
          status: "AVAILABLE" },
      ],
      direct_fire: {
        selectable_target_ids: [10, 11],
        units: [
          { unit_id: 1, weapons: [
            { weapon_id: 101, legal_target_ids: [10] },
            { weapon_id: 102, legal_target_ids: [] },
          ] },
          { unit_id: 2, weapons: [
            { weapon_id: 201, legal_target_ids: [10] },
          ] },
        ],
      },
    };
    globalThis.requests = [];
    requestJson = async (url, options) => {
      requests.push({ url, body: JSON.parse(options.body) });
      return { can_execute: true, per_unit_results: [] };
    };
    globalThis.mapState = {};
    mapRenderer = {
      setIndirectFireAreas() {},
      setTacticalOverlay() {},
      setActionTargets(value) { mapState.targets = value; },
      render(frame, actors) { mapState.actors = actors; },
    };
    const fixtureCapabilities = capabilities;
    loadCapabilities = async () => {
      // Model a fresh server projection for the newly selected actors.
      capabilities = { ...fixtureCapabilities, direct_fire: {
        ...fixtureCapabilities.direct_fire,
        units: fixtureCapabilities.direct_fire.units.filter(
          (unit) => selectedUnitIds.has(unit.unit_id)),
      } };
    };
    loadTacticalOverlay = async () => {};
  `);

  // A plain click, Shift-add, and Ctrl-toggle retain intentional multiselect.
  await run("handleUnitSelection(1)");
  await run("handleUnitSelection(2, { shiftKey: true })");
  assert.deepEqual(read("selectedIds()"), [1, 2]);
  await run("handleUnitSelection(3, { ctrlKey: true })");
  assert.deepEqual(read("selectedIds()"), [1, 2, 3]);
  await run("handleUnitSelection(3, { ctrlKey: true })");
  assert.deepEqual(read("selectedIds()"), [1, 2]);

  await run('activateCapability("DIRECT_FIRE")');
  await run("selectDirectFireWeapon(1, 101)");
  assert.deepEqual(read("activeShootTargetIds()"), []);
  await run("selectDirectFireWeapon(2, 201)");
  assert.deepEqual(read("activeShootTargetIds()"), [10, 11]);
  assert.deepEqual(read("selectedIds()"), [1, 2]);
  assert.equal(read("targetUnitId"), null);
  assert.equal(read("requests.length"), 0);
  const instruction = elements.get("tool-instruction").textContent;
  assert.equal(instruction, "选择目标或空射格");

  // The candidate outside both legal-target annotations stays selectable.
  assert.deepEqual(
    read("capabilityUI.weaponSelectableTargetIds(capabilities, 1, 101)"),
    [10, 11],
  );
  run("renderMap()");
  assert.deepEqual(read("mapState.actors"), [1, 2]);
  assert.deepEqual(read("mapState.targets.shoot_target_ids"), [10, 11]);
  assert.equal(read("mapState.targets.preview_shoot_target_id"), null);
  await run("handleUnitSelection(11)");
  assert.deepEqual(read("selectedIds()"), [1, 2]);
  assert.equal(read("targetUnitId"), 11);
  assert.deepEqual(read("requests[0].body.actor_ids"), [1, 2]);
  assert.equal(read("requests[0].body.target_id"), 11);
  assert.deepEqual(
    read("requests[0].body.weapon_choices"), { 1: 101, 2: 201 },
  );
  run("renderMap()");
  assert.deepEqual(read("mapState.targets.shoot_target_ids"), [10, 11]);
  assert.equal(read("mapState.targets.preview_shoot_target_id"), 11);

  // Unknown enemies cannot be selected; choosing another candidate replaces
  // the single target, rather than selecting or attacking every highlighted unit.
  await run("handleUnitSelection(999)");
  assert.equal(read("requests.length"), 1);
  await run("handleUnitSelection(10)");
  assert.equal(read("targetUnitId"), 10);
  assert.equal(read("requests.length"), 2);
  assert.deepEqual(read("selectedIds()"), [1, 2]);

  // Switching the weapon clears only the target/draft, not selected actors.
  await run("selectDirectFireWeapon(1, 102)");
  assert.equal(read("targetUnitId"), null);
  assert.equal(read("operationPreview"), null);
  assert.deepEqual(read("selectedIds()"), [1, 2]);
  assert.deepEqual(read("activeShootTargetIds()"), [10, 11]);
  await run('activateCapability("DIRECT_FIRE")');
  assert.equal(elements.get("tool-instruction").textContent, instruction);

  // Guided fire still uses only its supplied legal targets.
  run(`
    activeTool = "GUIDED_FIRE";
    selectedGuidedPlatform = {
      shooter_id: 1, weapon_id: 101, legal_target_ids: [10],
    };
  `);
  assert.deepEqual(read("activeGuidedTargetIds()"), [10]);
  await run("handleUnitSelection(11)");
  assert.equal(read("requests.length"), 2);
  assert.equal(read("targetUnitId"), null);

  // Position-only direct fire clears the prior target, and reselecting an
  // enemy clears the coordinate. Occupants of a clicked hex are never inferred.
  run(`
    window.ReplayMapRenderer = { formatEdgeFacts: () => "none" };
    activeTool = "SHOOT";
    targetUnitId = 10;
    requests.length = 0;
  `);
  await run("handleHexSelection({ position_id: 104, units: [{ operator_id: 10 }] })");
  assert.equal(read("targetUnitId"), null);
  assert.equal(read("targetHex"), 104);
  assert.equal(read("requests[0].body.target_hex"), 104);
  assert.equal(read("requests[0].body.target_id ?? null"), null);
  await run("handleUnitSelection(10)");
  assert.equal(read("targetHex"), null);
  assert.equal(read("requests[1].body.target_id"), 10);
  assert.equal(read("requests[1].body.target_hex ?? null"), null);

  // Indirect unit selection binds a visible ID explicitly; a subsequent hex
  // click removes it even when the coordinate contains that same unit.
  run(`
    activeTool = "INDIRECT_FIRE";
    selectedIndirectOption = { weapon_id: 72 };
    current.scene.frame.viewer_units[3].current_hex = 104;
    requests.length = 0;
  `);
  await run("handleUnitSelection(10)");
  assert.equal(read("requests[0].body.target_id"), 10);
  assert.equal(read("requests[0].body.target_hex"), 104);
  await run("handleHexSelection({ position_id: 104, units: [{ operator_id: 10 }] })");
  assert.equal(read("targetUnitId"), null);
  assert.equal(read("requests[1].body.target_id ?? null"), null);
  assert.equal(read("requests[1].body.target_hex"), 104);

  // Verify that the actual renderer gives candidates, the chosen target,
  // and selected friendly actors distinct rings.
  const { MapRenderer, unitDisplayMetrics } = require(path.join(staticRoot, "map_renderer.js"));
  const renderer = Object.create(MapRenderer.prototype);
  renderer.selectedUnitIds = new Set(["1", "2"]);
  renderer.selectedUnitId = null;
  renderer.hitTargets = [];
  renderer._resolveUnitSymbol = () => ({});
  renderer.unitSymbolRenderer = { drawUnitSymbol: () => true };
  renderer.setActionTargets({
    shoot_target_ids: [10, 11], preview_shoot_target_id: 11,
  });
  const rings = [];
  const context = {
    dash: [],
    save() {}, restore() {}, translate() {}, fillText() {}, fill() {},
    beginPath() {},
    arc(_x, _y, radius) { this.radius = radius; },
    setLineDash(value) { this.dash = value; },
    stroke() {
      rings.push({
        radius: this.radius, color: this.strokeStyle,
        width: this.lineWidth, dash: this.dash.slice(),
      });
    },
  };
  for (const unit of [
    { operator_id: 1, side: "red" },
    { operator_id: 3, side: "red" },
    { operator_id: 10, side: "blue" },
    { operator_id: 11, side: "blue" },
  ]) renderer._drawUnit(context, unit, 0, 0);
  const focusedRadius = unitDisplayMetrics(undefined, undefined, true).radius;
  assert.deepEqual(rings, [
    { radius: focusedRadius + 2, color: "#ffe15a", width: 3, dash: [] },
    { radius: 24, color: "#ff5b62", width: 2, dash: [4, 3] },
    { radius: focusedRadius + 8, color: "#ffc857", width: 4, dash: [] },
  ]);

  // The sidebar must use the same authoritative category registry as the glyph.
  vm.runInContext(fs.readFileSync(path.join(staticRoot, "unit_symbol_resolver.js"), "utf8"), sandbox);
  sandbox.UnitSymbolResolver.installRegistry(JSON.parse(
    fs.readFileSync(path.join(__dirname, "../config/source_compat/operator_registry_v1.json"), "utf8"),
  ));
  sandbox.document.createElement = () => ({ textContent: "" });
  const summary = sandbox.document.getElementById("selection-summary");
  summary.replaceChildren = () => { summary.children = []; };
  summary.append = (...items) => summary.children.push(...items);
  summary.appendChild = (item) => summary.children.push(item);
  run(`
    current.scene.frame.viewer_units = [{ operator_id: 1, side: "red",
      type_code: 2, sub_type_code: 0, operator_type: "tank" }];
    selectedUnitIds.clear(); selectedUnitIds.add(1);
    capabilities = null;
    renderSelection();
  `);
  assert.equal(summary.children[1].textContent, "坦克 × 1");
  run(`current.scene.frame.viewer_units[0].name = "重型坦克"; renderSelection();`);
  assert.equal(summary.children[1].textContent, "重型坦克 × 1");
  run(`current.scene.frame.viewer_units[0].name = "中型坦克"; renderSelection();`);
  assert.equal(summary.children[1].textContent, "中型坦克 × 1");
  run(`current.scene.frame.viewer_units[0].name = "轻型坦克"; renderSelection();`);
  assert.equal(summary.children[1].textContent, "轻型坦克 × 1");
  run(`
    current.scene.frame.viewer_units[0].sub_type_code = 999;
    current.scene.frame.viewer_units[0].name = "重型坦克";
    renderSelection();
  `);
  assert.equal(summary.children[1].textContent, "车辆算子（未细分） × 1");

  console.log("Attack selection UI checks passed.");
}

main().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});
