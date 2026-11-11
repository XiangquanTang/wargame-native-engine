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
vm.runInContext(controller + '\nconst actualRefresh = refresh; const actualLoadCapabilities = loadCapabilities; refresh = async () => {}; render = () => {};',sandbox);
const run=code=>vm.runInContext(code,sandbox);
const read=code=>JSON.parse(run(`JSON.stringify(${code})`));
function reset() {
  run(`
    current={side:"red",control_mode:"realtime",observation_version:10,
      product_status:{can_operate:true,frame_index:10},scene:{frame:{viewer_units:[
        {operator_id:1,side:"red",category:"operator",current_hex:101},
        {operator_id:2,side:"red",category:"operator",current_hex:101},
        {operator_id:10,side:"blue",category:"operator",current_hex:102},
        {operator_id:11,side:"blue",category:"operator",current_hex:103,destroyed:true}]}}};
    selectedUnitIds.clear(); selectedUnitIds.add(1);
    activeTool=null; selectedCapability=null; targetHex=null; targetUnitId=null;
    selectedWeaponIds.clear(); selectedGuidedPlatform=null; selectedIndirectOption=null;
    operationPreview=null; draftOperation=null; previewNeedsRefresh=false; pendingSubmission=null; radialOpen=true; executeRequestId=null; isPlanning=false; isExecuting=false; isRefreshing=false;
    lastError=null; lastNotice=null; lastFrameIndex=10; staticMapContext=null;
    capabilities={capabilities:[
      ["MOVE","MOVE"],["DIRECT_FIRE","SHOOT"],["GUIDED_FIRE","GUIDED_FIRE"],
      ["INDIRECT_FIRE","INDIRECT_FIRE"],["BOARD","BOARD"],["UNLOAD","UNLOAD"],
      ["CAPTURE","CAPTURE"],["ENTER_CONCEALMENT","ENTER_CONCEALMENT"],
      ["STOP_MOVE","STOP_MOVE"],["WEAPON_UNFOLD","WEAPON_UNFOLD"],
      ["WEAPON_LOCK","WEAPON_LOCK"],["WEAPON_UNLOCK","WEAPON_UNLOCK"],["WAIT","WAIT"]
    ].map(([capability_type,operation_type])=>({capability_type,operation_type,status:"AVAILABLE"})),
      direct_fire:{selectable_target_ids:[10],units:[{unit_id:1,lock_weapon_ids:[4],weapons:[{weapon_id:4,legal_target_ids:[10]}]}]},
      guided_fire:{options:[{guider_id:1,shooter_id:2,weapon_id:29,target_id:10}]},
      indirect_fire:{options:[{shooter_id:1,weapon_id:72}]},
      boarding:{options:[{operation_type:"BOARD",actor_id:1,target_id:2},{operation_type:"UNLOAD",actor_id:1,target_id:2}]},
      capture:{options:[{actor_id:1,objective_id:7,coord:101}]}};
    globalThis.calls=[];
    requestJson=async(url,options)=>{calls.push({url,body:options?.body?JSON.parse(options.body):null});
      return url.endsWith("preview")?{can_execute:true,preview_token:"bound-preview",per_unit_results:[]}:{accepted:true,target_tick:11};};
    loadTacticalOverlay=async()=>{};
    loadCapabilities=async()=>{};
  `);
  elements.get("confirm-partial").checked=false;
}
async function main(){
  // All twelve legal operation families still use the existing authoritative preview route.
  for(const [cap,kind] of [["MOVE","MOVE"],["DIRECT_FIRE","SHOOT"],["GUIDED_FIRE","GUIDED_FIRE"],
    ["INDIRECT_FIRE","INDIRECT_FIRE"],["BOARD","BOARD"],["UNLOAD","UNLOAD"],["CAPTURE","CAPTURE"],
    ["ENTER_CONCEALMENT","ENTER_CONCEALMENT"],["STOP_MOVE","STOP_MOVE"],["WEAPON_UNFOLD","WEAPON_UNFOLD"],
    ["WEAPON_LOCK","WEAPON_LOCK"],["WEAPON_UNLOCK","WEAPON_UNLOCK"],["WAIT","WAIT"]]){
    reset();
    await run(`activateCapability(${JSON.stringify(cap)})`);
    if(cap==="MOVE")await run('handleHexSelection({position_id:205})');
    if(cap==="DIRECT_FIRE"){await run('selectDirectFireWeapon(1,4)');await run('handleUnitSelection(10)');}
    if(cap==="WEAPON_LOCK"){assert.equal(read("calls.length"),0,"LOCK requires explicit weapon choice");await run("selectDirectFireWeapon(1,4)");}
    if(cap==="GUIDED_FIRE")await run('handleUnitSelection(10)');
    if(cap==="INDIRECT_FIRE")await run('handleHexSelection({position_id:205})');
    assert.equal(read('calls.length'),1,`${cap} previews once`);
    assert.equal(read('calls[0].url'),'/api/product/preview');
    assert.equal(read('calls[0].body.operation_type'),kind);
    assert.deepEqual(read('calls[0].body.actor_ids'),[1]);
    assert.equal(read('calls[0].body.observation_version'),10);
    if(cap==="MOVE")assert.equal(read('calls[0].body.destination_hex'),205);
    if(cap==="DIRECT_FIRE"){assert.equal(read('calls[0].body.target_id'),10);assert.equal(read('calls[0].body.weapon_choices[1]'),4);}
    if(cap==="GUIDED_FIRE"){assert.equal(read('calls[0].body.guider_id'),1);assert.equal(read('calls[0].body.shooter_id'),2);}
    if(cap==="INDIRECT_FIRE"){assert.equal(read('calls[0].body.target_hex'),205);assert.equal(read('calls[0].body.target_id??null'),null);}
    await run('executeOperation()');
    assert.equal(read('calls.length'),2,`${cap} submits only after confirmation`);
    assert.equal(read('calls[1].url'),'/api/product/execute');
    assert.equal(read('calls[1].body.preview_token'),'bound-preview');
    assert.equal(read('operationPreview'),null);
    assert.equal(read('activeTool'),null);
  }
  // A bound lock filters shooting even after stopping; missing legacy ID fails closed.
  reset();run('capabilities.direct_fire.units[0].weapons.push({weapon_id:5});capabilities.direct_fire.units[0].locked_weapon_id=4;');
  assert.deepEqual(read('capabilityUI.directFireWeapons(capabilities,1).map(w=>w.weapon_id)'),[4]);
  await run('activateCapability("DIRECT_FIRE");selectDirectFireWeapon(1,5)');
  assert.equal(read('selectedWeaponIds.has(1)'),false);
  run('capabilities.direct_fire.units[0].locked_weapon_id=null;capabilities.direct_fire.units[0].weapon_locked=true;');
  assert.deepEqual(read('capabilityUI.directFireWeapons(capabilities,1)'),[]);
  // A capabilities poll may change lock legality, never the pinned SHOOT intent.
  reset(); await run('activateCapability("DIRECT_FIRE");selectDirectFireWeapon(1,4);handleUnitSelection(10)');
  const pinnedDraft=read('draftOperation');
  run('capabilities.direct_fire.units[0].weapons.push({weapon_id:5});capabilities.direct_fire.units[0].locked_weapon_id=5;requestJson=async()=>capabilities;');
  await run('actualLoadCapabilities()');
  assert.equal(read('selectedWeaponIds.get(1)'),4);
  assert.equal(read('targetUnitId'),10);
  assert.deepEqual(read('draftOperation'),pinnedDraft);
  assert.equal(read('previewNeedsRefresh'),true);
  assert.equal(read('executeRequestId'),null);
  assert.match(elements.get('tool-instruction').textContent,/锁定限制/);
  assert.deepEqual(read('capabilityUI.directFireWeapons(capabilities,1).map(w=>w.weapon_id)'),[5]);
  run('calls=[];requestJson=async(url,options)=>{calls.push({url,body:JSON.parse(options.body)});return {can_execute:false,per_unit_results:[]};};');
  await run('executeOperation()');
  assert.equal(read('calls.length'),1,'Old token must repreview and never submit rejected intent');
  assert.equal(read('calls[0].body.weapon_choices[1]'),4);
  assert.equal(read('calls[0].body.target_id'),10);
  // Actual carried weapon removal still invalidates the draft as in v18.
  run('capabilities.direct_fire.units[0].weapons=capabilities.direct_fire.units[0].weapons.filter(w=>w.weapon_id!==4);requestJson=async()=>capabilities;');
  await run('actualLoadCapabilities()');
  assert.equal(read('selectedWeaponIds.has(1)'),false);
  assert.equal(read('targetUnitId'),null);
  assert.equal(read('draftOperation'),null);
  // Unavailable capabilities and enemy/destroyed selections never start a command.
  reset(); run('capabilities.capabilities.forEach(c=>c.status="UNAVAILABLE");');
  await run('activateCapability("MOVE")');assert.equal(read('calls.length'),0);assert.equal(read('activeTool'),null);
  await run('handleUnitSelection(10)');assert.deepEqual(read('selectedIds()'),[1]);
  run('current.scene.frame.viewer_units[1].destroyed=true;');
  await run('handleUnitSelection(2)');assert.deepEqual(read('selectedIds()'),[1]);
  // A cancel while preview is in-flight invalidates its result without submitting.
  reset();
  let resolvePreview; sandbox.deferredPreview=new Promise(resolve=>{resolvePreview=resolve;});
  run('requestJson=async()=>deferredPreview;');
  const pending=run('previewOperation("MOVE",{destination_hex:205})');
  run('clearOperation()'); resolvePreview({can_execute:true,preview_token:'obsolete'}); await pending;
  assert.equal(read('operationPreview'),null);assert.equal(read('executeRequestId'),null);
  // Two rapid confirmations cannot create two execute requests.
  reset(); await run('activateCapability("WEAPON_LOCK"); selectDirectFireWeapon(1,4)');
  let resolveExecute; sandbox.deferredExecute=new Promise(resolve=>{resolveExecute=resolve;});
  run('requestJson=async(url,options)=>{calls.push({url,body:JSON.parse(options.body)});return deferredExecute;};');
  const first=run('executeOperation()');await run('executeOperation()');
  assert.equal(read('calls.filter(c=>c.url.endsWith("execute")).length'),1);
  resolveExecute({accepted:true,target_tick:11});await first;
  // Partial operations require explicit opt-in before confirmation.
  reset();run('operationPreview={can_execute:true,partial_confirmation_required:true,preview_token:"partial"};executeRequestId="once";');
  await run('executeOperation()');assert.equal(read('calls.length'),0);
  elements.get('confirm-partial').checked=true;await run('executeOperation()');assert.equal(read('calls.length'),1);
  // Escape clears draft first, then selection; losing an actor also clears its draft.
  reset();await run('activateCapability("MOVE")');windowEvents.get('keydown')({key:'Escape'});
  assert.equal(read('activeTool'),null);assert.deepEqual(read('selectedIds()'),[1]);
  windowEvents.get('keydown')({key:'Escape'});assert.deepEqual(read('selectedIds()'),[]);
  reset();await run('activateCapability("WEAPON_LOCK")');run('current.scene.frame.viewer_units[0].destroyed=true;reconcileInteractionDraft();');
  assert.deepEqual(read('selectedIds()'),[]);assert.equal(read('operationPreview'),null);
  // Real polling invalidates the old confirmation token without losing the intended move.
  reset(); await run('activateCapability("MOVE")'); await run('handleHexSelection({position_id:205})');
  run('loadDisplayState=async()=>({...current,observation_version:11,product_status:{can_operate:true,frame_index:11}});');
  await run('actualRefresh()');
  assert.equal(read('operationPreview.can_execute'),true); assert.equal(read('previewNeedsRefresh'),true); assert.equal(read('executeRequestId'),null);
  assert.equal(read('activeTool'),'MOVE'); assert.equal(read('targetHex'),205);
  await run('executeOperation()'); assert.equal(read('calls.filter(c=>c.url.endsWith("execute")).length'),1);
  assert.equal(read('calls.filter(c=>c.url.endsWith("preview")).at(-1).body.observation_version'),11);
  // Turn-based control has no observation version, so frame changes must also refresh tokens.
  reset(); run('current.control_mode="turn_based";delete current.observation_version;');
  await run('activateCapability("MOVE")'); await run('handleHexSelection({position_id:205})');
  run('loadDisplayState=async()=>({...current,product_status:{can_operate:true,frame_index:11}});');
  await run('actualRefresh()');
  assert.equal(read('operationPreview.can_execute'),true);
  assert.equal(read('previewNeedsRefresh'),true,'Frame-only updates must invalidate confirmation freshness');
  assert.equal(read('executeRequestId'),null);
  await run('executeOperation()');
  assert.equal(read('calls.filter(c=>c.url.endsWith("preview")).length'),2);
  assert.equal(read('calls.filter(c=>c.url.endsWith("execute")).length'),1);
  assert.equal(read('"observation_version" in calls[0].body'),false);
  // An in-flight turn-based preview is visual-only if the frame advances before it arrives.
  reset(); run('current.control_mode="turn_based";delete current.observation_version;');
  await run('activateCapability("MOVE")');
  let finishTurnBasedPreview;
  sandbox.turnBasedPreview=new Promise(resolve=>{finishTurnBasedPreview=resolve;});
  run('requestJson=async()=>turnBasedPreview;');
  const turnBasedRequest=run('handleHexSelection({position_id:205})');
  run('loadDisplayState=async()=>({...current,product_status:{can_operate:true,frame_index:11}});');
  await run('actualRefresh()');
  finishTurnBasedPreview({can_execute:true,preview_token:'previous-turn',per_unit_results:[]});
  await turnBasedRequest;
  assert.equal(read('previewNeedsRefresh'),true);
  assert.equal(read('executeRequestId'),null);
  // A refresh already in flight may finish while preview is pending; keep it visual-only.
  reset();
  let resolveRacingPreview; sandbox.racingPreview=new Promise(resolve=>{resolveRacingPreview=resolve;});
  run('requestJson=async()=>racingPreview;loadDisplayState=async()=>({...current,observation_version:11,product_status:{can_operate:true,frame_index:11}});');
  const racing=run('previewOperation("MOVE",{destination_hex:205})');
  await run('actualRefresh()'); resolveRacingPreview({can_execute:true,preview_token:'old-observation'});await racing;
  assert.equal(read('previewNeedsRefresh'),true,'old-observation preview is visual-only and requires fresh validation');
  assert.equal(read('executeRequestId'),null);
  // A contact disappearing or being destroyed cannot leave an active target confirmation.
  for (const remove of [true,false]) {
    reset(); await run('activateCapability("DIRECT_FIRE")'); await run('selectDirectFireWeapon(1,4)'); await run('handleUnitSelection(10)');
    run(`loadDisplayState=async()=>({...current,observation_version:11,product_status:{can_operate:true,frame_index:11},
      scene:{frame:{viewer_units:current.scene.frame.viewer_units.${remove ? 'filter(u=>u.operator_id!==10)' : 'map(u=>u.operator_id===10?{...u,destroyed:true}:u)'}}}});`);
    await run('actualRefresh()'); assert.equal(read('targetUnitId'),null); assert.equal(read('operationPreview'),null);
  }
  // An unavailable capability returned by a new authoritative response clears that draft.
  reset(); await run('activateCapability("MOVE")');
  run('requestJson=async()=>({capabilities:[],direct_fire:{units:[]}});');
  await run('actualLoadCapabilities()'); assert.equal(read('activeTool'),null);
  // Hover/mouseleave only changes coordinate copy; committed route intent remains.
  reset(); await run('activateCapability("MOVE")'); await run('handleHexSelection({position_id:205})');
  run('renderHexInfo(null)'); assert.equal(read('targetHex'),205); assert.equal(read('operationPreview.can_execute'),true);
  // Repeated selected-unit click toggles ring rather than becoming a move target.
  await run('handleUnitSelection(1)'); assert.equal(read('radialOpen'),false); assert.equal(read('targetHex'),205);
  await run('handleUnitSelection(1)'); assert.equal(read('radialOpen'),true);
  // A second target while first preview is pending cannot corrupt its epoch/destination.
  reset(); await run('activateCapability("MOVE")'); let finish; sandbox.slow=new Promise(r=>finish=r);
  run('requestJson=async()=>slow;'); const selecting=run('handleHexSelection({position_id:205})');
  await run('handleHexSelection({position_id:206})'); assert.equal(read('targetHex'),205);
  finish({can_execute:true,preview_token:'slow',per_unit_results:[]}); await selecting;
  // A lost execution response retries the same token/request ID even across polling.
  reset(); await run('activateCapability("MOVE")'); await run('handleHexSelection({position_id:205})');
  run('requestJson=async(url,options)=>{calls.push({url,body:JSON.parse(options.body)});throw new Error("network");};');
  await run('executeOperation()'); const submitted=read('pendingSubmission');
  run('clearOperation();'); assert.deepEqual(read('pendingSubmission'),submitted);
  run('loadDisplayState=async()=>({...current,observation_version:11,product_status:{can_operate:true,frame_index:11}});');
  await run('actualRefresh()');
  run('requestJson=async(url,options)=>{calls.push({url,body:JSON.parse(options.body)});return {accepted:true,target_tick:11};};');
  await run('executeOperation()'); const executions=read('calls.filter(c=>c.url.endsWith("execute"))');
  assert.equal(executions.length,2); assert.deepEqual(executions[0].body,executions[1].body);
  // Fresh partial selection never inherits old consent across an observation update.
  reset(); await run('activateCapability("MOVE")'); await run('handleHexSelection({position_id:205})');
  elements.get('confirm-partial').checked=true;
  run('loadDisplayState=async()=>({...current,observation_version:11,product_status:{can_operate:true,frame_index:11}});');
  await run('actualRefresh()');
  run('requestJson=async(url,options)=>{calls.push({url,body:JSON.parse(options.body)});return {can_execute:true,partial_confirmation_required:true,preview_token:"partial-new",per_unit_results:[]};};');
  await run('executeOperation()'); assert.equal(read('calls.filter(c=>c.url.endsWith("execute")).length'),0);
  assert.equal(elements.get('confirm-partial').checked,false);
  // A contact invalidated while its preview is still in flight cannot be resurrected.
  for (const remove of [true, false]) {
    reset(); await run('activateCapability("DIRECT_FIRE")'); await run('selectDirectFireWeapon(1,4)');
    let finishTargetPreview;
    sandbox.targetPreview=new Promise(resolve=>{finishTargetPreview=resolve;});
    run('requestJson=async()=>targetPreview;');
    const pendingTarget=run('handleUnitSelection(10)');
    run(`loadDisplayState=async()=>({...current,observation_version:11,product_status:{can_operate:true,frame_index:11},
      scene:{frame:{viewer_units:current.scene.frame.viewer_units.${remove ? 'filter(u=>u.operator_id!==10)' : 'map(u=>u.operator_id===10?{...u,destroyed:true}:u)'}}}});`);
    await run('actualRefresh()');
    finishTargetPreview({can_execute:true,preview_token:'invalidated-target',per_unit_results:[]});
    await pendingTarget;
    assert.equal(read('targetUnitId'),null);
    assert.equal(read('operationPreview'),null,'Invalidated contact must not regain an executable preview');
    assert.equal(read('draftOperation'),null);
    assert.equal(read('executeRequestId'),null);
  }
  // An unknown execution outcome must remain retryable even if every actor disappears.
  reset(); await run('activateCapability("MOVE")'); await run('handleHexSelection({position_id:205})');
  run('requestJson=async(url,options)=>{calls.push({url,body:JSON.parse(options.body)});throw new Error("lost execution reply");};');
  await run('executeOperation()'); const uncertainPayload=read('pendingSubmission');
  run(`loadDisplayState=async()=>({...current,observation_version:11,product_status:{can_operate:true,frame_index:11},
    scene:{frame:{viewer_units:current.scene.frame.viewer_units.filter(u=>u.operator_id!==1)}}});`);
  await run('actualRefresh()'); run('renderToolbar()');
  assert.deepEqual(read('selectedIds()'),[]);
  assert.equal(elements.get('operation-dock').hidden,false,'Uncertain submission must keep its retry controls visible');
  assert.equal(elements.get('confirmation-controls').hidden,false);
  assert.equal(elements.get('execute-operation').disabled,false);
  assert.equal(elements.get('cancel-operation').disabled,true);
  assert.deepEqual(read('pendingSubmission'),uncertainPayload);
  assert.equal(read('statusText()[0]'),'结果待确认');
  run('requestJson=async(url,options)=>{calls.push({url,body:JSON.parse(options.body)});return {accepted:true,target_tick:11};};');
  await run('executeOperation()');
  const uncertainExecutions=read('calls.filter(c=>c.url.endsWith("execute"))');
  assert.equal(uncertainExecutions.length,2);
  assert.deepEqual(uncertainExecutions[0].body,uncertainExecutions[1].body);
  // Session identity matters even when the new session happens to reuse frame/version numbers.
  reset(); run('current.session_id="old-session";');
  await run('activateCapability("MOVE")'); await run('handleHexSelection({position_id:205})');
  run('loadDisplayState=async()=>({...current,session_id:"new-session"});');
  await run('actualRefresh()');
  assert.deepEqual(read('selectedIds()'),[]);
  assert.equal(read('operationPreview'),null);
  assert.equal(read('draftOperation'),null);
  assert.equal(read('executeRequestId'),null);
  assert.equal(read('targetHex'),null);
  // A response requested in the previous session must also remain discarded.
  reset(); run('current.session_id="old-session";'); await run('activateCapability("MOVE")');
  let finishOldSession; sandbox.oldSessionPreview=new Promise(resolve=>{finishOldSession=resolve;});
  run('requestJson=async()=>oldSessionPreview;');
  const previousSessionRequest=run('handleHexSelection({position_id:205})');
  run('loadDisplayState=async()=>({...current,session_id:"new-session"});');
  await run('actualRefresh()');
  finishOldSession({can_execute:true,preview_token:'old-session-preview',per_unit_results:[]});
  await previousSessionRequest;
  assert.equal(read('operationPreview'),null);
  assert.equal(read('draftOperation'),null);
  // A changed accepted-unit subset requires fresh consent, not a reused checkbox.
  reset(); run(`selectedUnitIds.add(2); activeTool="MOVE"; selectedCapability="MOVE"; targetHex=205;
    draftOperation={kind:"MOVE",extra:{destination_hex:205}}; previewNeedsRefresh=true;
    operationPreview={can_execute:true,partial_confirmation_required:true,preview_token:"old-subset",per_unit_results:[
      {unit_id:1,accepted_for_preview:true,path:[205]},{unit_id:2,accepted_for_preview:false,path:[]}]};`);
  elements.get('confirm-partial').checked=true;
  run(`requestJson=async(url,options)=>{calls.push({url,body:JSON.parse(options.body)});return {
    can_execute:true,partial_confirmation_required:true,preview_token:"changed-subset",per_unit_results:[
      {unit_id:1,accepted_for_preview:false,path:[]},{unit_id:2,accepted_for_preview:true,path:[205]}]};};`);
  await run('executeOperation()');
  assert.equal(read('calls.filter(c=>c.url.endsWith("execute")).length'),0);
  assert.equal(elements.get('confirm-partial').checked,false);
  assert.equal(read('operationPreview.preview_token'),'changed-subset');
  // Polling must not make an unchanged partial subset impossible to approve in real time.
  reset(); run(`selectedUnitIds.add(2); activeTool="MOVE"; selectedCapability="MOVE"; targetHex=205;
    draftOperation={kind:"MOVE",extra:{destination_hex:205}};
    operationPreview={can_execute:true,partial_confirmation_required:true,preview_token:"old-same-subset",per_unit_results:[
      {unit_id:1,accepted_for_preview:true,path:[205]},{unit_id:2,accepted_for_preview:false,path:[]}]};`);
  elements.get('confirm-partial').checked=true;
  run('loadDisplayState=async()=>({...current,observation_version:11,product_status:{can_operate:true,frame_index:11}});');
  await run('actualRefresh()');
  assert.equal(elements.get('confirm-partial').checked,true,'Unchanged pinned intent retains subset consent during polling');
  run(`requestJson=async(url,options)=>{calls.push({url,body:JSON.parse(options.body)});return url.endsWith("preview")?{
    can_execute:true,partial_confirmation_required:true,preview_token:"fresh-same-subset",per_unit_results:[
      {unit_id:2,accepted_for_preview:false,path:[]},{unit_id:1,accepted_for_preview:true,path:[205]}]}:{accepted:true,target_tick:11};};`);
  await run('executeOperation()');
  assert.equal(read('calls.filter(c=>c.url.endsWith("execute")).length'),1);
  assert.equal(read('calls.at(-1).body.preview_token'),'fresh-same-subset');
  assert.equal(read('calls.at(-1).body.confirm_partial'),true);
  // A transient revalidation failure preserves the pinned path but never its old execute token.
  reset(); await run('activateCapability("MOVE")'); await run('handleHexSelection({position_id:205})');
  run(`operationPreview.per_unit_results=[{unit_id:1,accepted_for_preview:true,path:[102,205]}];
    previewNeedsRefresh=true; executeRequestId=null;
    requestJson=async()=>{throw new Error("temporary preview timeout");};`);
  const pinnedPaths=read('plannedPaths()');
  await run('executeOperation()'); run('renderToolbar()');
  assert.equal(read('targetHex'),205);
  assert.deepEqual(read('plannedPaths()'),pinnedPaths);
  assert.equal(read('previewNeedsRefresh'),true);
  assert.equal(read('executeRequestId'),null);
  assert.equal(elements.get('execute-operation').disabled,false,'Failed revalidation must remain retryable');
  run(`requestJson=async(url,options)=>{calls.push({url,body:JSON.parse(options.body)});return url.endsWith("preview")?
    {can_execute:true,preview_token:"recovered-preview",per_unit_results:[]}:{accepted:true,target_tick:11};};`);
  await run('executeOperation()');
  assert.equal(read('calls.filter(c=>c.url.endsWith("execute")).length'),1);
  assert.equal(read('calls.at(-1).body.preview_token'),'recovered-preview');
  // The ring reflects only AVAILABLE server capabilities and preserves DOM identity on polling.
  reset(); run(`capabilities.capabilities=capabilities.capabilities.slice(0,2);
    capabilities.capabilities.push({capability_type:"CAPTURE",operation_type:"CAPTURE",status:"UNAVAILABLE"});
    current.scene.frame.viewer_units[0].keep=1; current.scene.frame.viewer_units[0].lose_control=1;
    mapRenderer={hitTargets:[{operatorId:1,x:300,y:300}],canvas:{clientWidth:1000,clientHeight:700}};
    renderCapabilityPanel();`);
  const ring=elements.get('capability-panel');
  assert.equal(ring.hidden,false); assert.deepEqual(ring.children.map(b=>b.textContent),['机动','射击']);
  const firstButton=ring.children[0]; run('renderCapabilityPanel()'); assert.equal(ring.children[0],firstButton);
  run('selectedUnitIds.clear();renderCapabilityPanel();');assert.equal(ring.hidden,true);
  console.log('Independent radial operation workflows passed (13 action families and interruption guards).');
}
main().catch(error=>{console.error(error);process.exitCode=1;});
