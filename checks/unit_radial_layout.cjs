"use strict";
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const sandbox = {};
vm.createContext(sandbox);
vm.runInContext(fs.readFileSync(path.join(__dirname,
  "../src/wargame_native_engine/viewer/static/unit_radial_actions.js"), "utf8"), sandbox);
const layout = sandbox.UnitRadialActions.layout;
const buttons = Array.from({length: 12}, () => ({style: {}}));
const element = {style: {}, hidden: false, querySelectorAll: () => buttons};
const renderer = {canvas: {clientWidth: 600, clientHeight: 400},
  hitTargets: [{operatorId: 1, x: 300, y: 200}]};
layout(element, renderer, [1]);
assert.equal(element.hidden, false);
// Panning/zooming or moving an actor offscreen must not move/hide actions.
assert.deepEqual(element.style, {});
renderer.hitTargets[0].x = -100;
renderer.canvas.clientWidth = 180;
layout(element, renderer, [1]);
assert.equal(element.hidden, false);
assert.deepEqual(element.style, {});
layout(element, null, [1]);
assert.equal(element.hidden, false);
layout(element, renderer, []);
assert.equal(element.hidden, true);
const page = fs.readFileSync(path.join(__dirname,
  "../src/wargame_native_engine/viewer/static/dual_control_index.html"), "utf8");
assert.ok(page.indexOf('id="capability-panel"') > page.indexOf('id="operation-dock"'));
assert.match(page, /class="unit-radial-actions docked-actions"/);
console.log("Docked action layout checks passed.");
