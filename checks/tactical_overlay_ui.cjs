"use strict";
// Independent state-machine tests. Only painting and automatic polling are stubbed;
// actual selection, capability activation, targeting, preview and submit code runs.
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const staticRoot = path.join(__dirname, "../src/wargame_native_engine/viewer/static");
const elements = new Map();
const windowEvents = new Map();
function element(id = "") {
  return {id, hidden:false, checked:false, disabled:false, textContent:"", innerHTML:"",
    children:[], tagName:"BUTTON", style:{}, dataset:{}, classList:{add(){},remove(){},toggle(){}},
    addEventListener(type, callback) {this[type] = callback;},
    replaceChildren(...children){this.children=children;},
    append(...children){this.children=(this.children||[]).concat(children);},
    appendChild(child){this.append(child);}, setAttribute(){},
    getBoundingClientRect(){return {left:0,top:0,width:1000,height:700};},
    querySelector(){return null;}, querySelectorAll(selector){return this.children.filter(child=>selector==="button" ? child.tagName==="BUTTON" : selector==="[data-capability]" ? !!child.dataset.capability : false);}};
}
const sandbox = {URLSearchParams, performance:{now:()=>0}, crypto:{randomUUID:()=>"independent"},
  location:{search:"?token=independent"}, setInterval(){}, setTimeout(){}, clearTimeout(){},
  Element:class {}, addEventListener(type, callback){windowEvents.set(type,callback);},
  confirm:()=>true, document:{getElementById(id){if(!elements.has(id))elements.set(id,element(id));return elements.get(id);},
    createElement(){return element();}},
  ReplayMapRenderer:{formatEdgeFacts:()=>""}};
sandbox.window=sandbox;
vm.createContext(sandbox);
vm.runInContext(fs.readFileSync(path.join(staticRoot,"capability_ui.js"),"utf8"),sandbox);
vm.runInContext(fs.readFileSync(path.join(staticRoot,"unit_radial_actions.js"),"utf8"),sandbox);
let controller=fs.readFileSync(path.join(staticRoot,"dual_control.js"),"utf8");
controller=controller.replace(/^refresh\(\);\s*$/m, "");
vm.runInContext(controller + '\nconst actualRefresh = refresh; const actualLoadCapabilities = loadCapabilities; const actualLoadDisplayState = loadDisplayState; refresh = async () => {}; render = () => {};',sandbox);
const run=code=>vm.runInContext(code,sandbox);
const read=code=>JSON.parse(run(`JSON.stringify(${code})`));

function reset() {
  run(`current={session_id:"s1",side:"red",observation_version:10,
    product_status:{can_operate:true,frame_index:7},scene:{frame:{viewer_units:[]}}};
    selectedUnitIds.clear(); selectedUnitIds.add(1); selectedWeaponIds.clear();
    capabilities={direct_fire:{units:[{unit_id:1,weapons:[{weapon_id:4},{weapon_id:5}]}]}};
    selectedCapability=null; activeTool=null; draftOperation=null; operationPreview=null;
    visibilityOverlayEnabled=true; weaponEnvelopeEnabled=true;
    invalidateTacticalOverlay({clearCache:true});
    globalThis.reply={unit_id:1,observation_version:10,frame_index:7,
      visibility_field:{hex_ids:[102,205]},confirmed_contacts:{hex_ids:[205]},
      weapon_envelope:{hex_ids:[102,205,999],weapons:[
        {weapon_id:4,hex_ids:[102,205]},{weapon_id:5,hex_ids:[999]}]}};
    globalThis.calls=0; requestJson=async()=>{calls++;return reply;};`);
}
async function main() {
  reset(); await run('loadTacticalOverlay()');
  assert.deepEqual(read('tacticalOverlayHexes("visibility")'),[102,205]);
  assert.deepEqual(read('tacticalOverlayHexes("weapon")'),[],"no all-weapon union");
  await run('selectDirectFireWeapon(1,4)');
  assert.deepEqual(read('tacticalOverlayHexes("weapon")'),[102,205]);
  await run('selectDirectFireWeapon(1,5)');
  assert.deepEqual(read('tacticalOverlayHexes("weapon")'),[999]);
  assert.equal(read('calls'),1,"weapon filtering uses authoritative result, no extra LOS query");
  run('capabilities.direct_fire.units[0].locked_weapon_id=4');
  assert.deepEqual(read('tacticalOverlayHexes("weapon")'),[],"stale locked-out selection never paints");
  run('selectedWeaponIds.clear()');
  assert.deepEqual(read('tacticalOverlayHexes("weapon")'),[102,205],"sole locked weapon selected implicitly");
  // Renderer receives precisely the same side-bound cells, not a radius.
  run(`mapRenderer={setIndirectFireAreas(){},setActionTargets(){},render(){},
    setTacticalOverlay(value){globalThis.painted=value;}};renderMap();`);
  assert.deepEqual(read('painted.visibility_hex_ids'),[102,205]);
  assert.deepEqual(read('painted.weapon_envelope_hex_ids'),[102,205]);
  assert.equal(read('painted.side'),"red");
  run('mapRenderer=null');
  // Full-array endpoint is not paginated: no frontend first-page clipping.
  run('reply.visibility_field.hex_ids=Array.from({length:5000},(_,i)=>i)');
  await run('loadTacticalOverlay({force:true})');
  assert.equal(read('tacticalOverlayHexes("visibility").length'),5000);
  // Request racing a cache hit cannot replace the chosen cached overlay.
  run('requestJson=()=>new Promise(resolve=>globalThis.resolvePending=resolve)');
  const slow=run('loadTacticalOverlay({force:true})');
  assert.equal(read('overlayLoading'),true);
  assert.deepEqual(read('tacticalOverlayHexes("visibility")'),[]);
  await run('loadTacticalOverlay()');
  run('resolvePending({...reply,visibility_field:{hex_ids:[9999]}})'); await slow;
  assert.equal(read('tacticalOverlayHexes("visibility").length'),5000);
  // Hide cancels a request; it cannot resurrect a stale painted region.
  reset(); run('requestJson=()=>new Promise(resolve=>globalThis.resolvePending=resolve)');
  const cancel=run('loadTacticalOverlay()');
  run('visibilityOverlayEnabled=false;weaponEnvelopeEnabled=false');
  await run('loadTacticalOverlay()');run('resolvePending(reply)');await cancel;
  assert.equal(read('tacticalOverlay'),null);
  // Same version/actor in another session or side must never reuse cached data.
  reset(); await run('loadTacticalOverlay()');
  run('current.session_id="s2"');await run('loadTacticalOverlay()');
  run('current.side="blue"');await run('loadTacticalOverlay()');
  assert.equal(read('calls'),3);
  reset();run('requestJson=()=>new Promise(resolve=>globalThis.resolvePending=resolve)');
  const stale=run('loadTacticalOverlay()');run('current.session_id="changed";resolvePending(reply)');await stale;
  assert.equal(read('tacticalOverlay'),null);
  reset();run('requestJson=()=>new Promise(resolve=>globalThis.resolvePending=resolve)');
  const actorChanged=run('loadTacticalOverlay()');
  run('selectedUnitIds.clear();selectedUnitIds.add(2);invalidateTacticalOverlay();resolvePending(reply)');
  await actorChanged;assert.equal(read('tacticalOverlay'),null);
  // Wrong actor/version metadata and backend LOS failures are unavailable, not empty visibility.
  reset();run('reply.unit_id=2');await run('loadTacticalOverlay()');
  assert.equal(read('tacticalOverlay'),null);assert.match(read('overlayError'),/范围不可用/);
  reset();run('requestJson=async()=>{throw new Error("missing visibility channel")};');
  await run('loadTacticalOverlay()');assert.match(read('overlayError'),/missing visibility channel/);
  assert.equal(read('overlayLoading'),false);
  // A slow link can advance the server between every standalone read. Recover
  // once with a scene + capability + overlay snapshot, never with stale cells.
  function recoverySetup() {
    reset();
    run(`current.control_mode="realtime";lastFrameIndex=7;
      current.scene={map:{map_id:"test"},frame:{viewer_units:[
        {operator_id:1,side:"red",category:"operator"}]}};
      cachedStaticMap=()=>({map_id:"test"});
      globalThis.recovered={...current,observation_version:16,
        product_status:{can_operate:true,frame_index:10,observation_version:16},
        scene:{...current.scene,frame:{...current.scene.frame,frame_index:10}},
        tactical_overlay:{...reply,observation_version:16,frame_index:10,
          visibility_field:{hex_ids:[777]}},
        capabilities:{...capabilities,observation_version:16,frame_index:10}};
      requestJson=async(path)=>{calls++;
        if(path.includes("/bootstrap"))return recovered;
        const error=new Error("version advanced during transport");
        error.code="tactical_overlay_observation_stale";throw error;};`);
  }
  recoverySetup();
  run('activeTool="MOVE";selectedCapability="MOVE";draftOperation={destination_hex:205};operationPreview={preview_token:"route"};recovered.capabilities.capabilities=[{capability_type:"MOVE",operation_type:"MOVE",status:"AVAILABLE"}];');
  await run('loadTacticalOverlay()');
  assert.equal(read('calls'),2,"exactly one atomic recovery, no retry loop");
  assert.equal(read('current.observation_version'),16);
  assert.equal(read('lastFrameIndex'),10);
  assert.deepEqual(read('tacticalOverlayHexes("visibility")'),[777]);
  assert.equal(read('capabilities.observation_version'),16);
  assert.equal(read('operationPreview.preview_token'),"route");
  assert.equal(read('previewNeedsRefresh'),true,"pinned intent is preserved but revalidated");
  recoverySetup();run('recovered.tactical_overlay.visibility_field.hex_ids=[]');
  await run('loadTacticalOverlay()');
  assert.deepEqual(read('tacticalOverlayHexes("visibility")'),[]);
  assert.equal(read('overlayError'),null,"valid empty mask is not a load failure");
  run('renderToolbar()');
  assert.match(elements.get('overlay-status').textContent,/可见 0 格/);
  // Malformed or mismatched composite reads must not update the displayed scene.
  for (const mutation of [
    'recovered.side="blue"', 'recovered.session_id="another"',
    'recovered.tactical_overlay.unit_id=2',
    'recovered.tactical_overlay.frame_index=11',
    'recovered.capabilities.observation_version=17',
    'recovered.scene.frame.frame_index=11',
    'recovered.product_status.observation_version=17',
  ]) {
    recoverySetup();run(mutation);await run('loadTacticalOverlay()');
    assert.equal(read('current.observation_version'),10);
    assert.equal(read('tacticalOverlay'),null);
    assert.match(read('overlayError'),/范围不可用/);
  }
  // Hide, actor switch, seat switch and session reset cancel an in-flight recovery.
  for (const mutation of [
    'visibilityOverlayEnabled=false;weaponEnvelopeEnabled=false;invalidateTacticalOverlay()',
    'selectedUnitIds.clear();selectedUnitIds.add(2);invalidateTacticalOverlay()',
    'current.side="blue"', 'current.session_id="new-session"',
  ]) {
    recoverySetup();run(`requestJson=async(path)=>{calls++;
      if(path.includes("/bootstrap"))return new Promise(resolve=>globalThis.resolveRecovery=resolve);
      const error=new Error("stale");error.code="tactical_overlay_observation_stale";throw error;};`);
    const pending=run('loadTacticalOverlay()');await Promise.resolve();
    run(mutation+';resolveRecovery(recovered)');await pending;
    assert.equal(read('tacticalOverlay'),null);
    assert.equal(read('current.observation_version'),10);
    assert.equal(read('overlayLoading'),false);
  }
  recoverySetup();run(`requestJson=async()=>{calls++;const error=new Error("stale");
    error.code="tactical_overlay_observation_stale";throw error;};`);
  await run('loadTacticalOverlay()');
  assert.equal(read('calls'),2);assert.match(read('overlayError'),/stale/);
  // Steady polling keeps the old coherent scene+mask visible until its new
  // combined response arrives; no intermediate blank mask or extra POST.
  recoverySetup();
  run(`tacticalOverlay=reply;isRefreshing=false;
    loadDisplayState=()=>new Promise(resolve=>globalThis.resolveDisplay=resolve);
    requestJson=async(path)=>{calls++;
      if(path.includes("/status"))return {frame_index:8,observation_version:12,perspective:"RED"};
      throw new Error("combined polling must not make a second request");};`);
  const combinedPoll=run('actualRefresh()');await Promise.resolve();
  assert.deepEqual(read('tacticalOverlayHexes("visibility")'),[102,205]);
  run('resolveDisplay(recovered)');await combinedPoll;
  assert.equal(read('calls'),1);
  assert.deepEqual(read('tacticalOverlayHexes("visibility")'),[777]);
  assert.equal(read('current.observation_version'),16);
  // Closing while that poll is in flight must not revive its returned region.
  recoverySetup();run(`tacticalOverlay=reply;isRefreshing=false;
    loadDisplayState=()=>new Promise(resolve=>globalThis.resolveDisplay=resolve);
    requestJson=async()=>({frame_index:8,observation_version:12,perspective:"RED"});`);
  const hiddenPoll=run('actualRefresh()');await Promise.resolve();
  run('visibilityOverlayEnabled=false;weaponEnvelopeEnabled=false;invalidateTacticalOverlay();resolveDisplay(recovered)');
  await hiddenPoll;assert.equal(read('tacticalOverlay'),null);
  // The real display reader requests the combined endpoint only for a selected
  // realtime actor; a removed actor gets one ordinary scene read to reconcile.
  recoverySetup();run(`requestJson=async(path)=>{calls++;
    if(path.includes("overlay_unit_id=1")){const error=new Error("removed");error.code="actor_not_owned";throw error;}
    return current;};`);
  await run('actualLoadDisplayState()');assert.equal(read('calls'),2);
  for (const lifecycle of ["ended", "stopping", "published", "not_accepting", "failed_reset_required", "closed"]) {
    recoverySetup();run(`requestJson=async(path)=>{calls++;
      if(path.includes("overlay_unit_id=1")){const error=new Error("lifecycle");error.code="realtime_session_${lifecycle}";throw error;}
      return {...current,observation_version:16,product_status:{can_operate:false,frame_index:10}};};`);
    const stopped=await run('actualLoadDisplayState()');
    assert.equal(stopped.product_status.can_operate,false);
    assert.equal(read('calls'),2,"one plain scene fallback for lifecycle transition");
  }
  // Old independent capability replies (or failures) cannot unlock a weapon
  // after the coherent recovery has installed its newer capability snapshot.
  for (const rejectOld of [false, true]) {
    recoverySetup();
    run(`recovered.capabilities={...recovered.capabilities,direct_fire:{units:[
      {unit_id:1,locked_weapon_id:5,weapons:[{weapon_id:4},{weapon_id:5}]}]}};
      globalThis.oldCapabilities=capabilities;
      requestJson=async(path)=>{
        if(path.includes("/capabilities"))return new Promise((resolve,reject)=>{
          globalThis.finishOldCapabilities=()=>${rejectOld ? 'reject(new Error("old failure"))' : 'resolve(oldCapabilities)'};});
        if(path.includes("/bootstrap"))return recovered;
        const error=new Error("stale");error.code="observation_version_stale";throw error;};`);
    const oldCapabilities=run('actualLoadCapabilities()');
    await run('loadTacticalOverlay()');
    run('finishOldCapabilities()');await oldCapabilities;
    assert.equal(read('capabilities.observation_version'),16);
    assert.deepEqual(read('directFireWeapons().map(value=>value.weapon_id)'),[5]);
    assert.equal(read('lastError'),null);
  }
  // A successful recovery must not later be rolled back by an older poll.
  recoverySetup();
  run(`globalThis.older={...current};isRefreshing=false;
    loadDisplayState=()=>new Promise(resolve=>globalThis.resolveDisplay=resolve);
    requestJson=async(path)=>{
      if(path.includes("/status"))return {frame_index:8,observation_version:12,perspective:"RED"};
      if(path.includes("/bootstrap"))return recovered;
      const error=new Error("stale");error.code="tactical_overlay_observation_stale";throw error;};`);
  const oldPoll=run('actualRefresh()');await Promise.resolve();
  await run('loadTacticalOverlay()');run('resolveDisplay(older)');await oldPoll;
  assert.equal(read('current.observation_version'),16);
  assert.deepEqual(read('tacticalOverlayHexes("visibility")'),[777]);
  // Observation counters can restart in a new session; even a same-frame
  // lightweight status response must be scoped to the context that requested it.
  recoverySetup();run(`isRefreshing=false;
    requestJson=()=>new Promise(resolve=>globalThis.resolveStatus=resolve);`);
  const oldStatus=run('actualRefresh()');
  run('current.session_id="replacement";resolveStatus({frame_index:7,observation_version:10,perspective:"RED",can_operate:false})');
  await oldStatus;assert.equal(read('current.product_status.can_operate'),true);
  // Read-only weapon choice must preserve an already pinned movement route.
  reset();run('activeTool="MOVE";selectedCapability="MOVE";draftOperation={destination_hex:205};operationPreview={preview_token:"route"};');
  await run('selectDirectFireWeapon(1,5)');
  assert.equal(read('operationPreview.preview_token'),"route");
  assert.equal(read('draftOperation.destination_hex'),205);assert.equal(read('calls'),0);
  // Real refresh at the same frame must still reload changed observation cells.
  reset(); await run('loadTacticalOverlay()');
  run(`lastFrameIndex=7;isRefreshing=false;
    current.scene.frame.viewer_units=[{operator_id:1,side:"red",category:"operator"}];
    cachedStaticMap=()=>({});
    loadDisplayState=async()=>({...current,observation_version:11});
    loadCapabilities=async()=>{};
    reply={...reply,observation_version:11,visibility_field:{hex_ids:[333]}};
    requestJson=async(path)=>path.includes("/status")
      ? {frame_index:7,observation_version:11,perspective:"RED"} : reply;`);
  await run('actualRefresh()');
  assert.deepEqual(read('tacticalOverlayHexes("visibility")'),[333]);
  // Observation-only refresh must invalidate overlays, not wait for the next frame.
  const controllerText=fs.readFileSync(path.join(staticRoot,"dual_control.js"),"utf8");
  assert.match(controllerText,/if \(frameChanged \|\| observationChanged \|\| contextChanged\)/);
  const page=fs.readFileSync(path.join(staticRoot,"dual_control_index.html"),"utf8");
  assert.ok(page.indexOf('id="capability-panel"')<page.indexOf('id="selection-summary"'));
  assert.ok(page.indexOf('id="toggle-visibility-overlay"')>page.indexOf('id="operation-dock"'));
  const css=fs.readFileSync(path.join(staticRoot,"live_styles.css"),"utf8");
  assert.match(css,/\.operation-dock \.docked-actions[^}]*margin-top: 0/);
  console.log("Tactical overlay UI checks passed: actual cells, weapon locks, full arrays, stale/session/cancel/error guards, pinned route, top-right controls.");
}
main().catch(error=>{console.error(error);process.exitCode=1;});
