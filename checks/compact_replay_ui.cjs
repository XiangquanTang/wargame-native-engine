"use strict";
// Real HTTP loader + app/controller + MapRenderer; canvas drawing is stubbed.
// This checks render data and navigation, not browser pixels or layout.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const base = process.argv[2];
if (!base) throw new Error('usage: node compact_replay_ui.cjs http://127.0.0.1:PORT');
const calls = [], elements = new Map(), timers = new Map();
let gate = null;
let completed = false;
process.on('beforeExit', () => {
  if (!completed) {console.error('Compact UI checks did not complete; a gated request or response remained unresolved.'); process.exitCode = 1;}
});
function bounded(promise, message) {
  return new Promise((resolve, reject) => {
    const timer = setTimeout(() => reject(new Error(message)), 15000);
    promise.then(value => {clearTimeout(timer); resolve(value);}, error => {clearTimeout(timer); reject(error);});
  });
}
const FACTOR_PAIRS = [['V', 'base_value'], ['H', 'health'], ['W', 'weapon'], ['A', 'ammo'],
  ['R', 'readiness'], ['M', 'mobility'], ['I', 'information'], ['S', 'status']];
let sequence = 0, document;
function context() {
  const strokes = []; let points = 0;
  return new Proxy({strokes}, {get(target, key) {
    if (key === 'beginPath') return () => {points = 0;};
    if (key === 'lineTo') return () => {points++;};
    if (key === 'stroke') return () => {strokes.push({color: target.strokeStyle, points});};
    if (key === 'measureText') return text => ({width: String(text).length * 6});
    if (key === 'createLinearGradient') return () => ({addColorStop() {}});
    return target[key] ?? (() => {});
  }, set(target, key, value) {target[key] = value; return true;}});
}
function element() {
  const ctx = context();
  const value = {ownerDocument: document, style: {}, handlers: {}, children: [], value: '0', textContent: '', hidden: true,
    clientWidth: 1000, clientHeight: 700, width: 1000, height: 700,
    setAttribute() {}, setPointerCapture() {}, getContext: () => ctx,
    getBoundingClientRect: () => ({left: 0, top: 0, width: 1000, height: 700}),
    addEventListener(name, callback) {this.handlers[name] = callback;},
    append(...nodes) {this.children.push(...nodes);}, appendChild(node) {this.children.push(node);},
    replaceChildren(...nodes) {this.children = nodes;}};
  Object.defineProperty(value, 'id', {set(id) {elements.set(id, value);}, configurable: true});
  return value;
}
document = {getElementById(id) {if (!elements.has(id)) elements.set(id, element()); return elements.get(id);},
  createElement: element, createTextNode: text => ({textContent: text})};
const sandbox = {console, document, AbortController, Map, Number, JSON,
  ViewerUrls: {resolve: route => base + route},
  fetch: async (url, options) => {
    const route = url.slice(base.length); calls.push(route);
    if (gate?.route === route) {
      const held = gate; gate = null;
      // Emulate a network response that ignores advisory AbortSignal cancellation.
      const response = await fetch(url, {...options, signal: undefined});
      held.ready();
      await held.released;
      return response;
    }
    return fetch(url, options);
  },
  window: {devicePixelRatio: 1, addEventListener() {},
    setTimeout(callback) {const id = ++sequence; timers.set(id, callback); return id;},
    clearTimeout(id) {timers.delete(id);}},
};
vm.createContext(sandbox);
const root = path.resolve(__dirname, '../src/wargame_native_engine/viewer/static');
for (const file of ['map_renderer.js', 'timeline_chart.js', 'app.js']) {
  vm.runInContext(fs.readFileSync(path.join(root, file), 'utf8'), sandbox, {filename: file});
}
const run = code => vm.runInContext(code, sandbox);
async function ready() {
  const deadline = Date.now() + 120000;
  while (!run('currentFrame && mapRenderer?.frame && timelineData && replayPerspectives')) {
    if (Date.now() > deadline) throw new Error('Player initialization timeout: ' + elements.get('error-banner').textContent);
    await new Promise(resolve => setTimeout(resolve, 10));
  }
}
function number(value) {
  if (value === null || value === undefined || !Number.isFinite(value)) return 'UNKNOWN';
  return Number.isInteger(value) ? String(value) : value.toFixed(4).replace(/0+$/, '').replace(/\.$/, '');
}
function selectAndCheckFactors(id, expected, enemy = false) {
  sandbox.selectedId = id;
  run('selectedUnitId = selectedId; renderFactors()');
  assert.match(elements.get('selected-unit').textContent, /Unit/);
  const state = Array.from(run('currentFrame.viewer_units')).find(unit => unit.operator_id === id);
  const dead = !!expected && (state.alive === false || state.destroyed === true ||
    typeof state.blood === 'number' && state.blood <= 0);
  assert.equal(elements.get('effective-power').textContent, dead ? '0' : number(expected?.effective_power));
  const rows = elements.get('factor-table').children;
  assert.equal(rows.length, 8, 'selected recorded unit always has eight labeled rows');
  for (let index = 0; index < FACTOR_PAIRS.length; index++) {
    const [code, name] = FACTOR_PAIRS[index], row = rows[index];
    assert.equal(row.children[0].children[0].textContent, code);
    assert.equal(row.children[0].children[2].textContent, name);
    const factor = expected?.factor_values.find(item => item.factor_name === name);
    const expectedValue = dead ? '0' : factor?.status === 'OBSERVED' ? number(factor.value) : 'UNKNOWN';
    assert.equal(row.children[1].children[0].textContent, expectedValue, `${code}/${name} must match its actual factor`);
    if (dead) {
      assert.equal(row.children[1].children[2].textContent, '不参与');
      assert.equal(row.children[2].children[0].children[0].textContent, '原始因子审计');
    } else if (enemy) {
      assert.equal(row.children[2].textContent, '八因子：UNKNOWN', 'enemy factor evidence must not be present');
    } else if (factor) {
      assert(row.children[2].textContent.includes(factor.formula_id));
      assert(row.children[2].textContent.includes(JSON.stringify(factor.missing_fields)));
    }
  }
  if (dead) assert.match(elements.get('dead-unit-note').textContent, /已阵亡/);
}
function checkGreenAnalysis(index) {
  const analysis = run('currentAnalysis');
  assert(analysis && analysis.provenance.frame_index === index, 'GREEN analysis belongs to the displayed frame');
  assert.equal(analysis.provenance.frame_hash, null, 'compact factors do not invent per-frame digest evidence');
  const data = run('timelineData');
  const ordinal = Array.from(data.frame_indices).indexOf(index);
  for (const side of ['red', 'blue']) {
    const sideAnalysis = analysis.side_analyses.find(value => value.side === side);
    assert.equal(data[side + '_effective_power'][ordinal], sideAnalysis?.effective_power ?? null);
  }
  assert.equal(data.normalized_advantages[ordinal], analysis.advantage.normalized_advantage);
  assert.equal(data.raw_advantages[ordinal], analysis.advantage.raw_advantage);
  const before = JSON.stringify(analysis);
  for (const unit of analysis.unit_analyses) selectAndCheckFactors(unit.operator_id, unit);
  assert.equal(JSON.stringify(analysis), before, 'table rendering must not rewrite facts or UNKNOWN');
}
(async () => {
  await ready();
  const indices = Array.from(run('frameIndices'));
  const first = indices[0], last = indices.at(-1);
  const requestedSeeks = process.argv[3] ? process.argv[3].split(',').map(Number) : [];
  const seekRecords = [];
  assert.equal(run('currentFrameIndex'), first);
  assert.equal(run('mapRenderer.frame.frame_index'), first);
  assert(run('currentAnalysis'), 'compact replay must derive GREEN analysis without a sidecar');
  assert.equal(elements.get('frame-slider').value, '0');
  assert.equal(elements.get('frame-slider').max, String(indices.length - 1));
  assert(!/可选分析不可用/.test(elements.get('timeline-status').textContent));
  assert.match(elements.get('analysis-coverage').textContent, /全局有效单位覆盖率/);
  const data = run('timelineData'), chart = run('timelineChart');
  assert.deepEqual(Array.from(chart.model.indices), indices);
  for (const [name, chartName] of [['red_effective_power', 'red'], ['blue_effective_power', 'blue'], ['normalized_advantages', 'advantage']]) {
    assert.equal(data[name].length, indices.length);
    assert.deepEqual(Array.from(chart.model[chartName]), Array.from(data[name]), 'plot series retain exact derived values and UNKNOWN gaps');
  }
  assert.equal(chart.model.hasPower, [...data.red_effective_power, ...data.blue_effective_power].some(Number.isFinite));
  assert.equal(chart.model.hasAdvantage, data.normalized_advantages.some(Number.isFinite));
  const strokes = chart.background.getContext('2d').strokes;
  for (const [name, color] of [['red_effective_power', '#ff5b62'], ['blue_effective_power', '#45a8ff'], ['normalized_advantages', '#ffc857']]) {
    if (data[name].some((value, index) => index && Number.isFinite(value) && Number.isFinite(data[name][index - 1]))) {
      assert(strokes.some(stroke => stroke.color === color && stroke.points > 0), `${name} generates a plotted line`);
    }
  }
  checkGreenAnalysis(first);
  for (const index of requestedSeeks) {
    assert(indices.includes(index), 'requested integration seek must exist');
    assert.equal(await run(`setCurrentFrameIndex(${index})`), true);
    assert.equal(run('mapRenderer.frame.frame_index'), index);
    checkGreenAnalysis(index);
    seekRecords.push(JSON.parse(run('JSON.stringify({frame_index: currentFrame.frame_index, time: currentFrame.time, actions: currentFrame.actions, units: currentFrame.viewer_units.length})')));
  }
  assert.equal(await run(`setCurrentFrameIndex(${last})`), true);
  assert.equal(run('mapRenderer.frame.frame_index'), last);
  checkGreenAnalysis(last);
  assert.equal(run('mapRenderer.frame.time.cur_step'), run('currentFrame.time.cur_step'));
  assert.equal(elements.get('frame-slider').value, String(indices.length - 1));
  assert.equal(elements.get('frame-jump').value, String(last));
  const renderedIds = Array.from(run('mapRenderer.frame.viewer_units'), unit => unit.operator_id);
  assert(renderedIds.length > 0);
  assert(run('mapRenderer.hitTargets.length > 0'), 'real renderer generated unit hit targets');
  // Reverse seek must use the actual previous recorded name, never filename-1.
  await elements.get('previous-frame').handlers.click();
  assert.equal(run('currentFrameIndex'), indices[Math.max(0, indices.length - 2)]);
  if (indices.some((value, ordinal) => ordinal > 0 && value !== indices[ordinal - 1] + 1)) {
    assert.match(elements.get('playback-position').textContent, /非连续记录/);
    const absent = first + 1;
    assert(!indices.includes(absent));
    const before = calls.length;
    assert.equal(await run(`setCurrentFrameIndex(${absent})`), false);
    assert.equal(calls.length, before, 'missing frame never makes a fetch');
  }
  // The ordinal slider ends at the actual last filename.
  elements.get('frame-slider').handlers.input({target: {value: String(indices.length - 1)}});
  const slider = run('sliderTimer');
  await timers.get(slider)(); timers.delete(slider);
  assert.equal(run('currentFrameIndex'), last);
  // Play from end restarts at first, then advances only one recorded ordinal.
  timers.clear();
  await run('startPlayback()');
  assert.equal(run('currentFrameIndex'), first);
  const tickId = run('playbackTimer');
  const tick = timers.get(tickId); timers.delete(tickId); await tick();
  assert.equal(run('currentFrameIndex'), indices[Math.min(1, indices.length - 1)]);
  run('pausePlayback()');
  // Side requests are either safe recorded projection or an explicit empty state.
  for (const side of ['red', 'blue']) {
    const available = run(`replayPerspectives.${side.toUpperCase()}.status === 'available'`);
    const loaded = await run(`setReplayView('${side}')`);
    assert.equal(loaded, available);
    if (available) {
      assert.equal(run('mapRenderer.frame.perspective'), side.toUpperCase());
      assert.equal(run('currentAnalysis'), null);
      for (const index of [run('currentFrameIndex'), ...requestedSeeks]) {
        assert.equal(await run(`setCurrentFrameIndex(${index})`), true);
        assert.equal(run('mapRenderer.frame.frame_index'), index);
        const own = Array.from(run('currentOwnUnitAnalyses'));
        assert(own.every(unit => unit.side === side), 'side analysis must contain no enemy or third-side unit');
        for (const unit of run('mapRenderer.frame.viewer_units')) {
          if (unit.side !== side) {
            assert.equal(unit.blood, null); assert.equal(unit.movement_target_hex, null);
            assert(!own.some(factorUnit => factorUnit.operator_id === unit.operator_id));
            selectAndCheckFactors(unit.operator_id, null, true);
          } else {
            const factorUnit = own.find(value => value.operator_id === unit.operator_id);
            assert(factorUnit, 'recorded friendly unit receives only its own derived factors');
            selectAndCheckFactors(unit.operator_id, factorUnit);
          }
        }
        for (const id of ['actions', 'judge-info', 'warnings']) {
          assert.equal(elements.get(id).textContent, '全局记录未显示');
        }
      }
    } else assert.equal(run('mapRenderer.frame'), null);
  }
  await run("setReplayView('all')");
  assert(run('mapRenderer.frame.viewer_units.length > 0'));
  checkGreenAnalysis(run('currentFrameIndex'));
  // Exercise a real late GREEN HTTP response across a side change. It must not
  // replace the scoped frame, repopulate its factors, or enter the GREEN cache.
  const safeSide = ['red', 'blue'].find(side => run(`replayPerspectives.${side.toUpperCase()}.status === 'available'`));
  if (safeSide) {
    run('frameCache.clear()');
    let readyGate, releaseGate;
    const blocked = new Promise(resolve => {readyGate = resolve;});
    const released = new Promise(resolve => {releaseGate = resolve;});
    const modelSuffix = run('analysisModel') ? `?analysis_model=${run('analysisModel')}` : '';
    gate = {route: `/api/frame-bundle/${first}${modelSuffix}`, ready: readyGate, released};
    const stale = run(`setCurrentFrameIndex(${first})`);
    await bounded(blocked, 'Expected gated HTTP response never arrived');
    const changed = run(`setReplayView('${safeSide}')`);
    assert.equal(run('currentFrame'), null, 'side change synchronously clears GREEN while its response is held');
    assert.equal(run('currentOwnUnitAnalyses.length'), 0);
    assert.equal(await changed, true);
    const committed = JSON.stringify(run('currentFrame'));
    releaseGate();
    assert.equal(await stale, false);
    assert.equal(JSON.stringify(run('currentFrame')), committed);
    assert.equal(run('currentAnalysis'), null);
    assert.equal(run(`frameCache.has(frameCacheKey('all', ${first}))`), false);
    assert(run(`currentOwnUnitAnalyses.every(unit => unit.side === '${safeSide}')`));
    await run("setReplayView('all')");
    checkGreenAnalysis(first);
  }
  const models = Array.from(run('replayMetadata.analysis_models || []'));
  const modelResults = [];
  if (models.includes('source') && models.includes('native')) {
    const originalModel = run('analysisModel');
    const initialIndex = run('currentFrameIndex');
    for (const model of ['source', 'native', originalModel]) {
      if (run('analysisModel') !== model) {
        const changing = run(`setAnalysisModel('${model}')`);
        assert.equal(run('currentFrame'), null, 'changing models synchronously clears prior factors');
        assert.equal(run('timelineData'), null, 'changing models synchronously clears prior curve');
        assert.equal(await changing, true);
        await ready();
      }
      assert.equal(run('currentFrameIndex'), initialIndex, 'model choice cannot alter selected frame');
      const provenance = run('currentAnalysis.provenance');
      assert.equal(provenance.model_id, model === 'native' ? 'native-state-eight-factor' : 'native-eight-factor');
      checkGreenAnalysis(initialIndex);
      const badge = elements.get('compatibility').children.at(-1).textContent;
      assert(badge.includes(provenance.model_id), 'model badge follows actual displayed factors, not initial metadata');
      const unknown = Array.from(run('currentAnalysis.unit_analyses')).reduce((total, unit) =>
        total + unit.factor_values.filter(factor => factor.status === 'UNKNOWN').length, 0);
      const note = elements.get('analysis-model-note').textContent;
      assert(note.includes(model === 'native' ? 'Native' : 'Source'));
      if (model === 'source' && unknown > 0) assert(/Source 字段缺失，可切换 Native 派生/.test(note), 'UNKNOWN Source results explain the available recorded-facts alternative');
      if (model === 'native') assert(/派生|录制声明/.test(note));
      const unknownNames = {};
      for (const unit of run('currentAnalysis.unit_analyses')) for (const factor of unit.factor_values)
        if (factor.status === 'UNKNOWN') unknownNames[factor.factor_name] = (unknownNames[factor.factor_name] || 0) + 1;
      modelResults.push({model, model_id: provenance.model_id, unknown_factor_count: unknown, unknown_factor_names: unknownNames, model_note: note,
        power_available: run('timelineChart.model.hasPower'), advantage_available: run('timelineChart.model.hasAdvantage')});
      assert(run(`Array.from(frameCache.keys()).every(key => key.startsWith('${model}:'))`), 'model cache cannot reuse another model');
    }
    // Late Source curve cannot replace a subsequently selected Native curve.
    if (run('analysisModel') !== 'native') {await run("setAnalysisModel('native')"); await ready();}
    let readyGate, releaseGate;
    const blocked = new Promise(resolve => {readyGate = resolve;});
    const released = new Promise(resolve => {releaseGate = resolve;});
    gate = {route: '/api/timeline?analysis_model=source', ready: readyGate, released};
    const sourceSwitch = run("setAnalysisModel('source')");
    await bounded(blocked, 'Expected gated HTTP response never arrived'); assert.equal(await sourceSwitch, true);
    assert.equal(run('currentAnalysis.provenance.model_id'), 'native-eight-factor');
    assert.equal(run('timelineData'), null);
    assert.equal(await run("setAnalysisModel('native')"), true); await ready();
    const nativeTimeline = JSON.stringify(run('timelineData'));
    releaseGate();
    await new Promise(resolve => setImmediate(resolve));
    await new Promise(resolve => setImmediate(resolve));
    assert.equal(JSON.stringify(run('timelineData')), nativeTimeline, 'stale Source timeline must not replace Native curve');
    checkGreenAnalysis(initialIndex);
    // Rapid Source -> Native while the Source frame is in flight must not commit
    // factors from the superseded model even if the network ignores abort.
    run('frameCache.clear()');
    let frameReady, releaseFrame;
    const frameBlocked = new Promise(resolve => {frameReady = resolve;});
    const frameReleased = new Promise(resolve => {releaseFrame = resolve;});
    gate = {route: `/api/frame-bundle/${initialIndex}?analysis_model=source`, ready: frameReady, released: frameReleased};
    const staleSource = run("setAnalysisModel('source')");
    await bounded(frameBlocked, 'Expected gated model-frame response never arrived');
    assert.equal(run('currentFrame'), null);
    assert.equal(await run("setAnalysisModel('native')"), true); await ready();
    releaseFrame();
    assert.equal(await staleSource, false);
    assert.equal(run('currentAnalysis.provenance.model_id'), 'native-state-eight-factor');
    assert(run("Array.from(frameCache.keys()).every(key => key.startsWith('native:'))"));
    checkGreenAnalysis(initialIndex);
    if (originalModel !== 'native') {await run(`setAnalysisModel('${originalModel}')`); await ready();}
  }
  const fetched = calls.filter(value => value.startsWith('/api/frame-bundle/'));
  for (const route of fetched) assert(indices.includes(Number(route.split('/').at(-1).split('?')[0])));
  completed = true;
  console.log(JSON.stringify({passed: true, frame_indices: indices, fetched_frames: fetched,
    render_units_last: renderedIds.length, selected_seek_frames: seekRecords, factor_codes: FACTOR_PAIRS.map(pair => pair[0]),
    timeline_frames: data.frame_indices.length, model_switches: modelResults, plotted_power: chart.model.hasPower, plotted_advantage: chart.model.hasAdvantage, canvas: 'stubbed; render data only, not browser pixels'}));
})().catch(error => {console.error(error); process.exitCode = 1;});
