"use strict";
const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");
const path = require("node:path");
const root = path.resolve(__dirname, "..");
const chart = require(path.join(root, "src/wargame_native_engine/viewer/static/timeline_chart.js"));

const model = chart.buildModel({frame_indices: [0, 1, 2], red_effective_power: [10, 10, 10], blue_effective_power: [5, 5, 5], normalized_advantages: [1/3, 1/3, 1/3]});
assert.equal(model.powerMaximum, 10.5); // One shared zero-based domain, not independent min/max.
assert.equal(model.hasPower, true);
assert.equal(chart.buildModel({red_effective_power: [null], blue_effective_power: [null]}).hasPower, false);
assert.deepEqual(chart.segments([1, null, 3, 4, null, 0], [0, 1, 2, 3, 4, 5], 1), [[[0, 1]], [[2, 3], [3, 4]], [[5, 0]]]);
const dense = Array.from({length: 10000}, (_, i) => i === 5012 ? 999 : i % 2);
const reduced = chart.segments(dense, dense.map((_, i) => i), 100)[0];
assert(reduced.some(([index, value]) => index === 5012 && value === 999));
assert(reduced.length < 410);
assert.equal(chart.nearestIndex([0, 3, 8], 4), 1);
assert.equal(chart.nearestIndex([0, 3, 8], 7), 2);
assert.equal(chart.nearestIndex([], 0), -1);

let backgroundLines = 0, blits = 0;
const chartLabels = [];
function context(background) {
  return new Proxy({}, {get(target, key) {
    if (key === 'lineTo') return () => { if (background) backgroundLines++; };
    if (key === 'drawImage') return () => blits++;
    if (key === 'fillText') return (text) => chartLabels.push(text);
    return target[key] || (() => {});
  }, set(target, key, value) { target[key] = value; return true; }});
}
global.window = {devicePixelRatio: 1};
global.document = {createElement: () => ({width: 0, height: 0, getContext: () => context(true)})};
const canvas = {width: 0, height: 0, addEventListener() {}, getBoundingClientRect: () => ({width: 800, height: 350, left: 0}), getContext: () => context(false)};
const controller = new chart.TimelineChart(canvas, {textContent: ''}, () => {});
controller.setData({frame_indices: dense.map((_, i) => i), red_effective_power: dense, blue_effective_power: dense, normalized_advantages: dense.map(() => 0)});
const initialLines = backgroundLines;
for (let frame = 0; frame < 100; frame++) controller.setFrame(frame);
assert.equal(backgroundLines, initialLines, 'cursor updates must not redraw all time-series samples');
assert.equal(blits, 101);
assert(chartLabels.includes('有效力量'));
assert(chartLabels.includes('相对优势'));
controller.setData({frame_indices: [0], red_effective_power: [null], blue_effective_power: [null], normalized_advantages: [null]});
assert(chartLabels.includes('力量数据：UNKNOWN'));
assert(chartLabels.includes('相对优势：UNKNOWN'));
assert(!chartLabels.some((text) => /不是胜率|不补零|不插值|正值偏红方|红蓝共用刻度/.test(text)));
const replayHtml = fs.readFileSync(path.join(root, 'src/wargame_native_engine/viewer/static/index.html'), 'utf8');
assert(replayHtml.includes('八因子力量与相对优势曲线 · 全局'));
assert(!/单体力量为八因子乘积|不是胜率|不补零|不插值|id="strict-evidence-note"/.test(replayHtml));

function element() {
  return {value: '0', textContent: '', hidden: true, checked: true, handlers: {}, children: [], attributes: {},
    setAttribute(name, value) {this.attributes[name] = value;},
    addEventListener(name, handler) {this.handlers[name] = handler;},
    replaceChildren(...nodes) {this.children = nodes;}, append(...nodes) {this.children.push(...nodes);}, appendChild(node) {this.children.push(node);}};
}
const elements = new Map();
const calls = [], pending = new Map(), timers = new Map(), timerDelays = new Map();
let timerSequence = 0;
const metadata = {frame_count: 40, strict_replay_evidence_status: 'Available', strict_replay_evidence_note: 'legacy supplementary note', model_provenance: {model_id: 'test', model_version: '1'}};
const availability = {GREEN: {status: 'available'}, RED: {status: 'available', reason: null}, BLUE: {status: 'available', reason: null}};
const factorNames = ['base_value', 'health', 'weapon', 'ammo', 'readiness', 'mobility', 'information', 'status'];
function bundle(index) {
  return {frame: {time: {cur_step: index}, actions: ['GLOBAL_ACTION_SECRET'], judge_info: ['GLOBAL_JUDGE_SECRET'], viewer_units: [
    {operator_id: 1, side: 'red', name: 'global-red', current_hex: 101, blood: 10},
    {operator_id: 2, side: 'blue', name: 'global-blue-secret', blood: 99, movement_target_hex: 101, movement_progress: 0.5},
    {operator_id: 3, side: 'neutral'},
  ]}, analysis: {unit_analyses: [{operator_id: 2, side: 'blue', effective_power: 987654321, valid: true, observed_factor_count: 8,
    factor_values: factorNames.map((factor_name) => ({factor_name, value: 4321, status: 'OBSERVED', formula_id: 'PRIVATE_FORMULA', warnings: [], missing_fields: []})),
  }], warnings: ['GLOBAL_WARNING_SECRET'], side_analyses: [{side: 'red', effective_power: 10}, {side: 'blue', effective_power: 20}],
  advantage: {normalized_advantage: null, raw_advantage: null, label: 'UNKNOWN'}}};
}
function ownAnalysis(operator_id, side, effective_power = 100 + operator_id) {
  return {operator_id, side, effective_power, valid: true, observed_factor_count: 8,
    factor_values: factorNames.map((factor_name) => ({factor_name, value: 123, status: 'OBSERVED', formula_id: 'OWN_MODEL_FORMULA', warnings: [], missing_fields: []}))};
}
function sideBundle(index, perspective, visibilityStatus = 'recorded') {
  const own = perspective === 'RED' ? {operator_id: 1, side: 'red', name: 'recorded-red'} : {operator_id: 2, side: 'blue', name: 'recorded-blue'};
  const enemy = perspective === 'RED'
    ? {operator_id: 2, side: 'blue', name: index === 2 ? 'scenario-blue-static' : null, blood: null, max_blood: null, movement_target_hex: null, movement_progress: null, identity_source: index === 2 ? 'SCENARIO_DERIVED_IDENTITY' : 'UNKNOWN'}
    : {operator_id: 1, side: 'red', name: null, blood: null, max_blood: null, identity_source: 'UNKNOWN'};
  const visible = perspective === 'RED' ? index % 2 === 0 : index % 2 === 1;
  return {frame: {frame_index: index, perspective, visibility_status: visibilityStatus, time: {cur_step: index}, viewer_units: visible ? [own, enemy] : [own]}, analysis: null,
    own_analysis_source: 'GLOBAL_MODEL_DERIVED_OWN_FACTORS', own_unit_analyses: [ownAnalysis(own.operator_id, own.side, 100 + own.operator_id + index)]};
}
function deepFreeze(value) {
  if (value && typeof value === 'object') {
    Object.values(value).forEach(deepFreeze); Object.freeze(value);
  }
  return value;
}
const globalZero = deepFreeze(bundle(0));
const originalGlobalZero = JSON.stringify(globalZero);
const sandbox = {
  console, AbortController, Map, Number, JSON,
  document: {getElementById(id) {if (!elements.has(id)) elements.set(id, element()); return elements.get(id);}, createElement: element, createTextNode: (text) => ({textContent: text})},
  ViewerUrls: {resolve: (url) => url},
  window: {ReplayTimelineChart: {TimelineChart: class {
    setData() {} setFrame(frame) {this.frame = frame; this.requestedFrame = frame;}
    setRequestedFrame(frame) {this.requestedFrame = frame;} setVisible() {}
  }},
    addEventListener() {}, setTimeout(callback, delay) {const id = ++timerSequence; timers.set(id, callback); timerDelays.set(id, delay); return id;}, clearTimeout(id) {timers.delete(id);}},
  fetch(url, options) {
    calls.push({url, signal: options.signal});
    if (url === '/api/replay') return Promise.resolve({ok: true, json: async () => metadata});
    if (url === '/api/frame-bundle/0') return Promise.resolve({ok: true, json: async () => globalZero});
    return new Promise((resolve) => {
      if (!pending.has(url)) pending.set(url, []);
      pending.get(url).push((body, status = 200) => resolve({ok: status < 400, status, statusText: 'test response', json: async () => body}));
    });
  },
};
vm.createContext(sandbox);
vm.runInContext(fs.readFileSync(path.join(root, 'src/wargame_native_engine/viewer/static/app.js'), 'utf8'), sandbox);
const flush = () => new Promise((resolve) => setImmediate(resolve));
const run = (code) => vm.runInContext(code, sandbox);
function respond(url, body, status) {
  assert(pending.get(url)?.length, `expected pending ${url}`);
  pending.get(url).shift()(body, status);
}
const requested = (url) => calls.filter((call) => call.url === url);
const ids = (frame) => Array.from(frame?.viewer_units || [], (unit) => unit.operator_id);
let completed = false;
process.on('beforeExit', () => {
  if (!completed) {
    console.error('Replay UI checks did not finish; an expected response may be missing.');
    process.exitCode = 1;
  }
});
function displayedFrame() {
  return {
    frame: run('currentFrame'), index: run('currentFrameIndex'), analysis: run('currentAnalysis'),
    own: run('currentOwnUnitAnalyses'), selected: run('selectedUnitId'),
    frameLabel: elements.get('frame-index').textContent, step: elements.get('cur-step').textContent,
    position: elements.get('playback-position').textContent, slider: elements.get('frame-slider').value,
    jump: elements.get('frame-jump').value, cursor: run('timelineChart.frame'),
    viewStatus: elements.get('replay-view-status').textContent,
    power: elements.get('effective-power').textContent, summary: elements.get('red-power').textContent,
    factors: elements.get('factor-table').children,
  };
}
(async () => {
  await flush();
  assert.equal(elements.get('frame-index').textContent, '0');
  assert.equal(elements.get('compatibility').children[1].textContent, 'Strict replay evidence: Available');
  assert.equal(elements.get('compatibility').children[1].title, undefined);
  assert(!elements.has('strict-evidence-note'));
  assert(pending.has('/api/timeline') && pending.has('/api/map') && pending.has('/api/perspectives'), 'first frame must not wait for map, timeline or availability');
  respond('/api/perspectives', availability); await flush();
  const rendered = [];
  sandbox.renderer = {hoveredHexKey: '0:0', selectedHex: {row: 0, column: 0}, render(frame, selected) {
    this.frame = frame; rendered.push({frame, selected, index: run('currentFrameIndex'),
      label: elements.get('frame-index').textContent, own: run('currentOwnUnitAnalyses')});
  }};
  run('mapRenderer = renderer; renderBattlefield()');
  assert.equal(run('replayView'), 'all');
  assert.deepEqual(ids(rendered.at(-1).frame), [1, 2, 3]);
  const cachedFrame = rendered.at(-1).frame;
  run('selectedUnitId = 2; renderFactors()');
  assert.equal(elements.get('effective-power').textContent, '987654321');
  assert.match(elements.get('actions').textContent, /GLOBAL_ACTION_SECRET/);
  sandbox.document.getElementById('hex-info').textContent = 'units=2';

  const red = elements.get('replay-view-red').handlers.click();
  assert.equal(typeof red.then, 'function', 'view switch returns a promise');
  assert.equal(rendered.at(-1).frame, null, 'switch clears GREEN map synchronously before response');
  assert.equal(rendered.at(-1).selected, null);
  assert.equal(sandbox.renderer.hoveredHexKey, null);
  assert.equal(sandbox.renderer.selectedHex, null);
  assert(!elements.get('hex-info').textContent.includes('units=2'));
  assert.equal(elements.get('factor-table').children.length, 0);
  assert.equal(elements.get('replay-view-red').attributes['aria-pressed'], 'true');
  assert.equal(elements.get('replay-view-all').attributes['aria-pressed'], 'false');
  const redZero = sideBundle(0, 'RED');
  // Defense in depth: a mistakenly included global analysis cannot enter side cache/details.
  redZero.analysis = globalZero.analysis;
  redZero.own_unit_analyses.push(ownAnalysis(2, 'blue', 9001), ownAnalysis(2, 'red', 9002), ownAnalysis(1, 'blue', 9003), ownAnalysis(999, 'red', 9004));
  deepFreeze(redZero);
  const originalRedZero = JSON.stringify(redZero);
  respond('/api/frame-bundle/0?perspective=RED', redZero); assert.equal(await red, true);
  assert.deepEqual(ids(rendered.at(-1).frame), [1, 2], 'RED sees own red and recorded visible blue');
  assert.equal(run('currentAnalysis'), null);
  assert.equal(run('frameCache.get(frameCacheKey("red", 0)).analysis'), null);
  assert.deepEqual(Array.from(run('currentOwnUnitAnalyses'), (unit) => [unit.operator_id, unit.side]), [[1, 'red']], 'reject enemy, spoofed-side, mismatched-side and non-present IDs from own-only array');
  run('selectedUnitId = 1; renderFactors()');
  assert.equal(elements.get('effective-power').textContent, '101');
  assert.match(elements.get('unit-validity').textContent, /己方因子来源：全局模型/);
  for (const row of elements.get('factor-table').children) {
    assert.equal(row.children[1].children[0].textContent, '123');
    assert.match(row.children[2].textContent, /己方因子来源：全局模型/);
    assert.match(row.children[2].textContent, /OWN_MODEL_FORMULA/);
  }
  assert.equal(elements.get('red-power').textContent, '10', 'explicitly global same-frame summary can reuse cached GREEN analysis');
  run('selectedUnitId = 2; renderFactors()');
  assert.equal(elements.get('effective-power').textContent, 'UNKNOWN');
  assert.match(elements.get('unit-validity').textContent, /生命 UNKNOWN\/UNKNOWN/);
  assert.match(elements.get('unit-validity').textContent, /身份来源：UNKNOWN/);
  assert(!elements.get('unit-validity').textContent.includes('global-blue-secret'));
  assert.match(elements.get('unit-validity').textContent, /八因子：UNKNOWN/);
  assert.equal(elements.get('factor-table').children.length, 8);
  for (const row of elements.get('factor-table').children) {
    assert.equal(row.children[1].children[0].textContent, 'UNKNOWN');
    assert.equal(row.children[2].textContent, '八因子：UNKNOWN');
  }
  for (const id of ['actions', 'judge-info', 'warnings']) assert(!elements.get(id).textContent.includes('SECRET'), `${id} must not leak GREEN evidence`);
  assert.equal(JSON.stringify(redZero), originalRedZero);
  assert.equal(JSON.stringify(globalZero), originalGlobalZero);

  const blue = run('setReplayView("blue")');
  assert.equal(rendered.at(-1).frame, null);
  assert.equal(run('currentOwnUnitAnalyses.length'), 0, 'switch clears friendly factors immediately too');
  const unscopedBlue = sideBundle(0, 'BLUE'); unscopedBlue.own_analysis_source = 'UNKNOWN';
  respond('/api/frame-bundle/0?perspective=BLUE', deepFreeze(unscopedBlue)); await blue;
  run('selectedUnitId = 2; renderFactors()');
  assert.equal(elements.get('effective-power').textContent, 'UNKNOWN', 'own factor array requires its explicit source marker, never GREEN fallback');
  assert.equal(run('currentOwnUnitAnalyses.length'), 0);
  assert.deepEqual(ids(rendered.at(-1).frame), [2], 'same index uses separate BLUE visibility and cache entry');
  assert.equal(run('frameCache.has(frameCacheKey("all", 0)) && frameCache.has(frameCacheKey("red", 0)) && frameCache.has(frameCacheKey("blue", 0))'), true);
  const beforeCachedSwitch = calls.length;
  await run('setReplayView("red")');
  assert.deepEqual(ids(rendered.at(-1).frame), [1, 2]);
  run('selectedUnitId = 1; renderFactors()');
  assert.equal(elements.get('effective-power').textContent, '101', 'cached side restores only its own scoped factors');
  await run('setReplayView("all")');
  assert.equal(rendered.at(-1).frame, cachedFrame, 'legacy GREEN frame identity and content remain unchanged');
  assert.equal(calls.length, beforeCachedSwitch, 'same frame/side cache is reused without cross-side substitution');
  assert.equal(JSON.stringify(globalZero), originalGlobalZero);
  assert.match(elements.get('actions').textContent, /GLOBAL_ACTION_SECRET/);

  await run('setReplayView("red")');
  run('selectedUnitId = 2; mapRenderer.hoveredHexKey = "contact"; mapRenderer.selectedHex = {row: 0}; renderFactors()');
  const committedRedZero = displayedFrame(), beforeRedOne = rendered.length;
  const redOne = run('setCurrentFrameIndex(1)');
  assert.deepEqual(displayedFrame(), committedRedZero, 'loading retains the complete committed frame, labels, summary and factors');
  assert.equal(run('requestedFrameIndex'), 1);
  assert.equal(run('timelineChart.requestedFrame'), 1, 'navigation target is separate from displayed cursor');
  assert.equal(rendered.length, beforeRedOne, 'same-view loading must not render an intermediate empty battlefield');
  assert.equal(sandbox.renderer.hoveredHexKey, 'contact');
  assert.match(elements.get('loading-status').textContent, /Frame 1/);
  await flush();
  assert.deepEqual(displayedFrame(), committedRedZero, 'slow network must not advance labels before valid evidence arrives');
  respond('/api/frame-bundle/1?perspective=RED', deepFreeze(sideBundle(1, 'RED'))); await redOne;
  assert.equal(rendered.length, beforeRedOne + 1, 'commit replaces the old frame directly, with no blank render');
  assert.deepEqual(ids(rendered.at(-1).frame), [1], 'enemy disappears when no longer recorded visible');
  assert.equal(run('selectedUnitId'), null, 'invisible enemy selection clears at commit, not after rendering');
  assert.equal(rendered.at(-1).selected, null);
  assert.equal(rendered.at(-1).label, '1');
  assert.equal(rendered.at(-1).index, 1);
  assert.equal(rendered.at(-1).own[0].effective_power, 102);
  assert.equal(sandbox.renderer.hoveredHexKey, null);
  assert.equal(sandbox.renderer.selectedHex, null);
  assert.equal(elements.get('factor-table').children.length, 0);
  assert.equal(elements.get('red-power').textContent, 'UNKNOWN', 'do not keep prior frame global statistics');
  run('selectedUnitId = 1; renderFactors()');
  const committedRedOne = displayedFrame();
  const redTwo = run('setCurrentFrameIndex(2)');
  assert.deepEqual(displayedFrame(), committedRedOne);
  respond('/api/frame-bundle/2?perspective=RED', deepFreeze(sideBundle(2, 'RED'))); await redTwo;
  assert.deepEqual(ids(rendered.at(-1).frame), [1, 2], 'enemy reappears only when this frame records it');
  assert.equal(run('selectedUnitId'), 1, 'still-visible own selection survives the replacement');
  assert.equal(elements.get('effective-power').textContent, '103', 'own factors advance with their frame');
  run('selectedUnitId = 2; renderFactors()');
  assert.equal(elements.get('effective-power').textContent, 'UNKNOWN');
  assert.match(elements.get('unit-validity').textContent, /scenario-blue-static/);
  assert.match(elements.get('unit-validity').textContent, /身份来源：场景静态标签/);
  sandbox.mismatchedOwn = [ownAnalysis(1, 'blue', 9003), ownAnalysis(2, 'red', 9002)];
  sandbox.savedOwn = run('currentOwnUnitAnalyses');
  run('currentOwnUnitAnalyses = mismatchedOwn; selectedUnitId = 1; renderFactors()');
  assert.equal(elements.get('effective-power').textContent, 'UNKNOWN', 'selection rechecks own analysis side independently');
  run('selectedUnitId = 2; renderFactors()');
  assert.equal(elements.get('effective-power').textContent, 'UNKNOWN', 'selection rechecks current projected unit side independently');
  run('currentOwnUnitAnalyses = savedOwn');
  const redThree = run('setCurrentFrameIndex(3)');
  assert.equal(rendered.at(-1).frame.frame_index, 2);
  const blueThree = run('setReplayView("blue")');
  assert.equal(rendered.at(-1).frame, null, 'cross-perspective requests still clear immediately while a same-view request is pending');
  assert.equal(run('currentOwnUnitAnalyses.length'), 0);
  assert.equal(run('selectedUnitId'), null);
  assert.equal(requested('/api/frame-bundle/3?perspective=RED').at(-1).signal.aborted, true);
  respond('/api/frame-bundle/3?perspective=BLUE', sideBundle(3, 'BLUE')); await blueThree;
  respond('/api/frame-bundle/3?perspective=RED', sideBundle(3, 'RED')); await redThree;
  assert.deepEqual(ids(rendered.at(-1).frame), [2, 1], 'late RED response must not overwrite BLUE contacts');
  assert.equal(rendered.at(-1).frame.perspective, 'BLUE');
  run('selectedUnitId = 2; renderFactors()');
  assert.equal(elements.get('effective-power').textContent, '105', 'BLUE retains its own factor values after stale RED response');
  assert.equal(run('frameCache.has(frameCacheKey("red", 3))'), false, 'stale responses cannot repopulate cache');
  assert.equal(elements.get('perspective-heading').textContent, 'LOCAL · READ ONLY · BLUE');

  // Rapid seeks still accumulate requested positions atomically, now within a side.
  const committedBlueThree = displayedFrame(), beforeRapidSeek = rendered.length;
  const fourth = elements.get('next-frame').handlers.click();
  const fifth = elements.get('next-frame').handlers.click();
  assert.deepEqual(displayedFrame(), committedBlueThree);
  assert.equal(rendered.length, beforeRapidSeek);
  assert(pending.has('/api/frame-bundle/4?perspective=BLUE') && pending.has('/api/frame-bundle/5?perspective=BLUE'));
  assert.equal(requested('/api/frame-bundle/4?perspective=BLUE').at(-1).signal.aborted, true);
  respond('/api/frame-bundle/5?perspective=BLUE', sideBundle(5, 'BLUE')); await fifth;
  respond('/api/frame-bundle/4?perspective=BLUE', sideBundle(4, 'BLUE')); await fourth;
  assert.equal(rendered.length, beforeRapidSeek + 1, 'neither superseded seek nor its late response may render');
  assert.equal(elements.get('frame-index').textContent, '5');
  assert.equal(run('frameCache.has(frameCacheKey("blue", 4))'), false);
  assert.deepEqual(ids(rendered.at(-1).frame), [2, 1]);

  const committedBlueFive = displayedFrame();
  const missing = run('setCurrentFrameIndex(6)');
  assert.deepEqual(displayedFrame(), committedBlueFive);
  respond('/api/frame-bundle/6?perspective=BLUE', {error: '此帧缺少蓝方可见记录'}, 409);
  assert.equal(await missing, false);
  assert.equal(rendered.at(-1).frame, null, 'missing evidence must not restore old side or global frame');
  assert.equal(run('currentAnalysis'), null);
  assert.equal(run('selectedUnitId'), null);
  assert.equal(elements.get('frame-index').textContent, '6');
  assert.match(elements.get('loading-status').textContent, /此帧缺少蓝方可见记录/);
  assert.equal(elements.get('replay-view-status').textContent, '蓝方视角不可用：此帧缺少蓝方可见记录');
  assert.equal(requested('/api/frame-bundle/6').length, 0, 'no fallback GREEN fetch');
  assert.equal(run('frameCache.has(frameCacheKey("blue", 6))'), false);
  const globalSix = run('setReplayView("all")');
  respond('/api/frame-bundle/6', bundle(6)); await globalSix;
  assert.deepEqual(ids(rendered.at(-1).frame), [1, 2, 3], 'explicit return to all restores omniscient mode');

  // Known-unavailable archive gives a localized empty state without requesting GREEN.
  run('replayPerspectives.RED = {status: "unavailable", reason: "本复盘没有红方可见证据"}');
  const beforeUnavailable = calls.length;
  assert.equal(await run('setReplayView("red")'), false);
  assert.equal(calls.length, beforeUnavailable);
  assert.equal(rendered.at(-1).frame, null);
  assert.match(elements.get('replay-view-status').textContent, /本复盘没有红方可见证据/);
  run('replayPerspectives.RED = {status: "available"}');
  const wrongScope = run('setReplayView("red")');
  respond('/api/frame-bundle/6?perspective=RED', bundle(6));
  assert.equal(await wrongScope, false, 'unexpected GREEN-shaped response fails closed');
  assert.equal(rendered.at(-1).frame, null);
  assert.equal(run('frameCache.has(frameCacheKey("red", 6))'), false);
  const derived = run('setReplayView("red")');
  respond('/api/frame-bundle/6?perspective=RED', sideBundle(6, 'RED', 'derived_from_recorded_visibility')); await derived;
  assert.equal(elements.get('replay-view-status').textContent, '红方视角 · 来源：可见记录重建');
  timers.clear(); // Ignore error-banner expiry timers when checking playback scheduling.

  run('selectedUnitId = 1; renderFactors()');
  const committedRedSix = displayedFrame(), beforePlayback = rendered.length;
  await run('startPlayback()');
  const [tickId, tick] = Array.from(timers).at(-1); timers.delete(tickId);
  assert.equal(timerDelays.get(tickId), 1000, 'fix must not slow playback to hide loading flicker');
  const loading = tick(); await flush();
  assert.equal(timers.size, 0, 'slow fetch must not queue subsequent playback ticks');
  assert.deepEqual(displayedFrame(), committedRedSix, 'playback keeps the previous frame fully labeled until the next frame validates');
  assert.equal(rendered.length, beforePlayback, 'delayed playback must never flash empty units, flags or summaries');
  respond('/api/frame-bundle/7?perspective=RED', sideBundle(7, 'RED')); await loading;
  assert.equal(rendered.length, beforePlayback + 1);
  assert.equal(elements.get('frame-index').textContent, '7');
  assert.equal(elements.get('cur-step').textContent, '7');
  assert.equal(elements.get('frame-slider').value, '7');
  assert.equal(elements.get('frame-jump').value, '7');
  assert.equal(run('timelineChart.frame'), 7);
  assert.equal(elements.get('effective-power').textContent, '108');
  assert.deepEqual(ids(rendered.at(-1).frame), [1], 'playback honors frame-specific disappearing visibility');
  assert.equal(timers.size, 1, 'schedule next tick only after committed frame');
  const switchDuringPlayback = run('setReplayView("blue")');
  assert.equal(run('playbackActive'), false, 'switch pauses playback and cancels timer');
  assert.equal(timers.size, 0);
  respond('/api/frame-bundle/7?perspective=BLUE', sideBundle(7, 'BLUE')); await switchDuringPlayback;
  assert.deepEqual(ids(rendered.at(-1).frame), [2, 1]);

  // Cross-view in-flight GREEN is also discarded, not flashed into the side map.
  const staleGlobal = run('setReplayView("all")');
  const savedBlue = run('setReplayView("blue")'); await savedBlue;
  respond('/api/frame-bundle/7', bundle(7)); await staleGlobal;
  assert.equal(rendered.at(-1).frame.perspective, 'BLUE');
  assert.equal(run('frameCache.has(frameCacheKey("all", 7))'), false);
  assert.equal(requested('/api/frame-bundle/7').at(-1).signal.aborted, true);

  // A late failure is also stale, not a reason to erase the newly selected side.
  const staleFailure = run('setCurrentFrameIndex(8)');
  const redEight = run('setReplayView("red")');
  respond('/api/frame-bundle/8?perspective=RED', sideBundle(8, 'RED')); await redEight;
  respond('/api/frame-bundle/8?perspective=BLUE', {error: 'stale side failure'}, 409); await staleFailure;
  assert.equal(rendered.at(-1).frame.perspective, 'RED');
  assert.equal(elements.get('error-banner').hidden, true);
  assert(!elements.get('loading-status').textContent.includes('stale side failure'));
  const blueEight = run('setReplayView("blue")');
  respond('/api/frame-bundle/8?perspective=BLUE', sideBundle(8, 'BLUE')); await blueEight;

  // Unknown visibility provenance cannot silently authorize a side projection.
  const unknownEvidence = run('setCurrentFrameIndex(9)');
  respond('/api/frame-bundle/9?perspective=BLUE', sideBundle(9, 'BLUE', 'unknown'));
  assert.equal(await unknownEvidence, false);
  assert.equal(rendered.at(-1).frame, null);
  assert.equal(run('frameCache.has(frameCacheKey("blue", 9))'), false);

  // A same-side response must prove both its frame index and visibility shape.
  const wrongIndex = sideBundle(10, 'BLUE');
  const missingIndex = sideBundle(9, 'BLUE'); delete missingIndex.frame.frame_index;
  const malformedUnits = sideBundle(9, 'BLUE'); malformedUnits.frame.viewer_units = null;
  for (const malformed of [wrongIndex, missingIndex, malformedUnits, null]) {
    await run('setCurrentFrameIndex(8)');
    const committedBlueEight = displayedFrame(), beforeMalformed = rendered.length;
    const invalid = run('setCurrentFrameIndex(9)');
    assert.deepEqual(displayedFrame(), committedBlueEight);
    respond('/api/frame-bundle/9?perspective=BLUE', malformed);
    assert.equal(await invalid, false, 'malformed evidence must not commit with a new frame label');
    assert.equal(rendered.length, beforeMalformed + 1);
    assert.equal(rendered.at(-1).frame, null, 'only failure, not pending loading, clears the frame');
    assert.equal(run('currentOwnUnitAnalyses.length'), 0);
    assert.equal(run('selectedUnitId'), null);
    assert.equal(run('frameCache.has(frameCacheKey("blue", 9))'), false);
    assert.equal(requested('/api/frame-bundle/9').length, 0, 'malformed evidence never triggers GREEN fallback');
  }
  await run('setCurrentFrameIndex(8)');
  sandbox.invalidCachedBundle = wrongIndex;
  run('frameCache.set(frameCacheKey("blue", 9), invalidCachedBundle)');
  const beforeInvalidCached = calls.length;
  assert.equal(await run('setCurrentFrameIndex(9)'), false, 'cache hits must pass the same frame/scope validation');
  assert.equal(calls.length, beforeInvalidCached);
  assert.equal(rendered.at(-1).frame, null);
  assert.equal(run('frameCache.has(frameCacheKey("blue", 9))'), false, 'invalid cache entry is evicted rather than reused');

  // Cache stays bounded across both frame and side dimensions without mutating sources.
  for (let index = 8; index <= 24; index++) {
    const loading = run(`setCurrentFrameIndex(${index})`);
    if (pending.get(`/api/frame-bundle/${index}?perspective=BLUE`)?.length) {
      respond(`/api/frame-bundle/${index}?perspective=BLUE`, deepFreeze(sideBundle(index, 'BLUE')));
    }
    await loading;
    assert(run('frameCache.size <= FRAME_CACHE_LIMIT'));
  }
  assert.equal(run('frameCache.size'), 16);
  assert.equal(run('frameCache.has(frameCacheKey("blue", 0))'), false);
  const beforeCachedSeek = calls.length, beforeCachedRender = rendered.length;
  run('selectedUnitId = 2; renderFactors()');
  await run('setCurrentFrameIndex(23)');
  assert.equal(calls.length, beforeCachedSeek);
  assert.equal(rendered.length, beforeCachedRender + 1, 'cached same-view seek also replaces directly without an empty render');
  assert.equal(rendered.at(-1).selected, 2);
  assert.equal(elements.get('effective-power').textContent, '125');
  assert.deepEqual(ids(rendered.at(-1).frame), [2, 1]);
  assert.equal(JSON.stringify(globalZero), originalGlobalZero);
  assert.equal(JSON.stringify(redZero), originalRedZero);

  // A paused in-flight tick may finish its current frame, but must never restart playback.
  timers.clear();
  run('selectedUnitId = 1; renderFactors()');
  await run('setCurrentFrameIndex(24)');
  assert.equal(run('selectedUnitId'), null, 'cached replacement clears an enemy absent from its recorded visibility');
  assert.equal(elements.get('factor-table').children.length, 0);
  await run('startPlayback()');
  const [pauseTickId, pauseTick] = Array.from(timers).at(-1); timers.delete(pauseTickId);
  const pausedLoad = pauseTick();
  const beforePause = displayedFrame();
  run('pausePlayback()');
  assert.deepEqual(displayedFrame(), beforePause);
  respond('/api/frame-bundle/25?perspective=BLUE', sideBundle(25, 'BLUE')); await pausedLoad;
  assert.equal(elements.get('frame-index').textContent, '25');
  assert.equal(run('playbackActive'), false);
  assert.equal(timers.size, 0, 'completion after pause must not enqueue another playback tick');

  // Restarting while a tick loads invalidates only that playback generation's timer.
  await run('startPlayback()');
  const [oldTickId, oldTick] = Array.from(timers).at(-1); timers.delete(oldTickId);
  const oldLoad = oldTick();
  run('playbackRate = 2'); await run('startPlayback()');
  const [newTickId, newTick] = Array.from(timers).at(-1);
  assert.equal(timerDelays.get(newTickId), 500);
  timers.delete(newTickId);
  const newLoad = newTick();
  const concurrent = pending.get('/api/frame-bundle/26?perspective=BLUE');
  assert.equal(concurrent.length, 2);
  assert.equal(requested('/api/frame-bundle/26?perspective=BLUE').at(-2).signal.aborted, true);
  const staleResponse = concurrent.shift(), activeResponse = concurrent.shift();
  activeResponse(sideBundle(26, 'BLUE')); await newLoad;
  assert.equal(timers.size, 1);
  const committedBlueTwentySix = displayedFrame(), beforeStaleTick = rendered.length;
  staleResponse(sideBundle(26, 'BLUE')); await oldLoad;
  assert.deepEqual(displayedFrame(), committedBlueTwentySix);
  assert.equal(rendered.length, beforeStaleTick);
  assert.equal(timers.size, 1, 'superseded playback generation must not queue a second timer');
  run('pausePlayback()');

  // Debounced seeks retain the committed frame, and a perspective switch cancels them.
  elements.get('frame-slider').handlers.input({target: {value: '27'}});
  const firstSliderId = run('sliderTimer');
  elements.get('frame-slider').handlers.input({target: {value: '28'}});
  assert.equal(timers.has(firstSliderId), false);
  assert.equal(timerDelays.get(run('sliderTimer')), 70);
  assert.equal(elements.get('frame-index').textContent, '26');
  const [sliderId, sliderTick] = Array.from(timers).at(-1); timers.delete(sliderId);
  const sliderLoad = sliderTick();
  assert.equal(requested('/api/frame-bundle/27?perspective=BLUE').length, 0);
  assert.equal(elements.get('frame-index').textContent, '26');
  respond('/api/frame-bundle/28?perspective=BLUE', sideBundle(28, 'BLUE')); await sliderLoad;
  assert.equal(elements.get('frame-index').textContent, '28');
  elements.get('frame-slider').handlers.input({target: {value: '29'}});
  const pendingSliderId = run('sliderTimer');
  const redTwentyEight = run('setReplayView("red")');
  assert.equal(timers.has(pendingSliderId), false);
  assert.equal(rendered.at(-1).frame, null);
  respond('/api/frame-bundle/28?perspective=RED', sideBundle(28, 'RED')); await redTwentyEight;
  assert.equal(requested('/api/frame-bundle/29?perspective=BLUE').length, 0);

  // Original global destroyed-factor presentation and audit data remain immutable.
  const restoreGlobal = run('setReplayView("all")');
  respond('/api/frame-bundle/28', bundle(28)); await restoreGlobal;
  sandbox.fixture = {
    frame: {viewer_units: [{operator_id: 64, alive: false, blood: 0}]},
    analysis: {unit_analyses: [{operator_id: 64, side: 'red', effective_power: null, valid: false, observed_factor_count: 5,
      factor_values: factorNames.map((factor_name, index) => ({factor_name, value: index === 0 ? 7 : index === 1 ? 0 : null, status: index < 2 ? 'OBSERVED' : 'UNKNOWN', formula_id: 'fixture', warnings: [], missing_fields: index < 2 ? [] : [factor_name]}))}]},
  };
  const original = JSON.stringify(sandbox.fixture.analysis);
  run('currentFrame = fixture.frame; currentAnalysis = fixture.analysis; selectedUnitId = 64; renderFactors()');
  assert.equal(elements.get('effective-power').textContent, '0');
  assert.match(elements.get('dead-unit-note').textContent, /已阵亡/);
  assert.equal(elements.get('factor-table').children.length, 8);
  for (const row of elements.get('factor-table').children) {
    assert.equal(row.children[1].children[0].textContent, '0');
    assert.equal(row.children[1].children[2].textContent, '不参与');
    assert.equal(row.children[2].children[0].children[0].textContent, '原始因子审计');
  }
  assert.equal(JSON.stringify(sandbox.fixture.analysis), original, 'display must not mutate UNKNOWN or original factors');
  assert.equal(run('isDestroyedUnit({viewer_units: []}, 64)'), false);
  completed = true;
  console.log('Replay chart domains/gaps/caching, recorded side visibility, atomic non-blank frame commits, cross-side isolation, private enemy analysis suppression, scoped own factors, static identity provenance, malformed evidence, seek cancellation, bounded cache, serialized playback, pause generations, slider debounce, and immutable legacy rendering: passed');
})().catch((error) => {console.error(error); process.exitCode = 1;});
