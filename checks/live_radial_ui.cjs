"use strict";
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const root = path.join(__dirname, "../src/wargame_native_engine/viewer/static");
const elements = new Map();
class Element {
  constructor() { this.children = []; this.events = {}; this.hidden = false; this.disabled = false; this.className = ""; this.value = ""; this.textContent = ""; this.classList = {add() {}}; }
  replaceChildren(...children) { this.children = children; }
  appendChild(child) { this.children.push(child); }
  addEventListener(name, fn) { this.events[name] = fn; }
}
const events = {};
const context = {
  document: { getElementById(id) { if (!elements.has(id)) elements.set(id, new Element()); return elements.get(id); }, createElement() { return new Element(); } },
  addEventListener(name, fn) { events[name] = fn; }, setTimeout() {},
  ReplayMapRenderer: { formatEdgeFacts() {return "";} }, UnitRadialActions: { layout() {} }, ViewerUrls: {resolve: value => value},
};
context.window = context;
vm.createContext(context);
vm.runInContext(fs.readFileSync(path.join(root,"live_actions.js"),"utf8"),context);
vm.runInContext(fs.readFileSync(path.join(root,"live_app.js"),"utf8") + "\nfunction initialize() {}",context);
const run = text => vm.runInContext(text, context);
const value = text => JSON.parse(run(`JSON.stringify(${text})`));
run(`
 mapRenderer = {setActionTargets(value){this.targets=value;},render(){}};
 sessionData = {perspective:"RED"};
 currentView = {step_index:0, terminal:false, observation:{}, frame:{viewer_units:[
 {operator_id:1,side:"red",alive:true},{operator_id:2,side:"red",alive:true},
 {operator_id:3,side:"red",alive:false},{operator_id:10,side:"blue",alive:true},
 {operator_id:11,side:"blue",alive:true}]}, valid_actions:[]};
 function action(id, actor, type, target=null, parameters={}) {
 return {action_id:id,action:{actor_id:actor,action_type:type,target_id:target,parameters}}; }
 currentView.valid_actions = [action("move-a",1,"MOVE",null,{path:[1,2]}),
 action("shot-a",1,"SHOOT",10,{weapon_id:4}),action("shot-b",1,"SHOOT",10,{weapon_id:5}),
 action("shot-c",1,"SHOOT",11,{weapon_id:4}),action("stop-a",1,"STOP_MOVE"),
 action("board-a",1,"BOARD",2),action("dead-a",3,"WAIT"),action("ready",null,"END_DEPLOYMENT")];
 renderCurrent(); handleUnitSelected(1);
`);
assert.deepEqual(elements.get("unit-actions").children.map(b=>b.textContent),["机动","射击","停止","搭载"]);
assert.equal(elements.get("submit-action").disabled,true);
run(`handleHexSelected({position_id:2});`);
assert.equal(value("selectedActionId"),null,"Hex alone cannot start move");
run(`chooseKind("MOVE"); handleHexSelected({position_id:2});`);
assert.equal(value("selectedActionId"),"move-a");
assert.equal(elements.get("submit-action").disabled,false);
run(`renderCurrent();`);
assert.equal(value("selectedActionId"),"move-a","Render preserves draft");
events.keydown({key:"Escape"});
assert.equal(value("selectedActionId"),null);
assert.equal(value("selectedKind"),null);
run(`chooseKind("SHOOT"); handleUnitSelected(10);`);
assert.equal(value("selectedActionId"),null,"Multiple weapons require explicit choice");
assert.equal(elements.get("action-select").hidden,false);
assert.equal(elements.get("submit-action").disabled,true);
elements.get("action-select").events.change({target:{value:"shot-b"}});
assert.equal(value("selectedActionId"),"shot-b");
run(`handleUnitSelected(11);`);
assert.equal(value("selectedActionId"),"shot-c","Can retarget after an initial target");
run(`cancelDraft(); handleUnitSelected(3);`);
assert.equal(value("selectedUnitId"),1,"Dead actor cannot take selection even with bad server candidate");
run(`handleUnitSelected(10);`);
assert.equal(value("selectedUnitId"),1,"Enemy cannot take actor selection");
run(`chooseKind("BOARD"); handleUnitSelected(2);`);
assert.equal(value("selectedActionId"),"board-a","Friendly target must not steal actor selection");
assert.equal(value("selectedUnitId"),1);
run(`currentView.valid_actions = currentView.valid_actions.filter(x=>x.action_id!=="board-a"); renderCurrent();`);
assert.equal(value("selectedActionId"),null,"Stale action clears without retargeting");
assert.equal(value("selectedKind"),null);
assert.equal(elements.get("submit-action").disabled,true);
assert.equal(elements.get("global-actions").children[0].textContent,"就绪");
elements.get("global-actions").children[0].events.click();
assert.equal(value("selectedActionId"),"ready");
assert.equal(value("selectedUnitId"),null);
run(`cancelDraft(); handleUnitSelected(1); chooseKind("STOP_MOVE");`);
// LOCK requires an explicit concrete weapon even when only one is carried.
run(`cancelDraft(); currentView.valid_actions.push(action("lock-4",1,"WEAPON_LOCK",null,{weapon_id:4}));
 handleUnitSelected(1); chooseKind("WEAPON_LOCK");`);
assert.equal(value("selectedActionId"),null);
assert.equal(elements.get("action-select").hidden,false);
elements.get("action-select").events.change({target:{value:"lock-4"}});
assert.equal(value("selectedActionId"),"lock-4");
run(`cancelDraft(); handleUnitSelected(1); chooseKind("STOP_MOVE");`);
async function main() {
 let finish;
 const requests=[];
 context.fetch = (url, options) => {
   requests.push({url, options});
   return new Promise(resolve => {
     finish = () => resolve({ok: true, json: async () => ({
       current: value("currentView"), command: {accepted: true},
     })});
   });
 };
 const pending=run(`submitAction()`);
 await run(`submitAction()`);
 assert.equal(requests.length,1,"Double confirmation submits once");
 run(`cancelDraft();`);
 assert.equal(value("commandPending"),true);
 assert.ok(elements.get("unit-actions").children.every(button=>button.disabled));
 finish(); await pending;
 assert.equal(value("selectedKind"),null);
 assert.equal(value("commandPending"),false);
 elements.get("wait-step").events.click();
 assert.equal(value("selectedKind"),"NO_OP");
 assert.equal(elements.get("submit-action").disabled,false);
 run(`currentView.terminal=true;renderCurrent();`);
 assert.equal(elements.get("submit-action").disabled,true);
 assert.ok(elements.get("unit-actions").children.every(button=>button.disabled));
 run(`currentView.terminal=false; publicationComplete=false; cancelDraft(); chooseKind("STOP_MOVE");`);
 const errorRequests=[];
 context.fetch=async (url, options={}) => {
   errorRequests.push({url,options});
   if (options.method === "POST") return {ok:false,status:409,statusText:"Conflict",json:async()=>({error:"stale"})};
   const current=value("currentView"); current.step_index=2; current.valid_actions=[];
   return {ok:true,json:async()=>current};
 };
 await run(`submitAction()`);
 assert.deepEqual(errorRequests.map(item=>item.url),["/api/command","/api/view"]);
 assert.equal(value("currentStepIndex"),2);
 assert.equal(value("selectedActionId"),null);
 assert.equal(value("commandPending"),false);
 let finishPublish;
 const publications=[];
 context.fetch=(url,options)=>{ publications.push({url,options});return new Promise(resolve=>{
   finishPublish=()=>resolve({ok:true,json:async()=>({replay:"saved"})});
 }); };
 const publishing=run(`publishMatch()`);
 await run(`publishMatch()`);
 await run(`submitAction()`);
 assert.equal(publications.length,1,"Publication locks repeated publish and command requests");
 finishPublish(); await publishing;
 assert.equal(value("publicationComplete"),true);
 assert.equal(elements.get("submit-action").disabled,true);
 const page=fs.readFileSync(path.join(root,"live_index.html"),"utf8");
 assert.doesNotMatch(page,/<aside|双击合法目标提交/);
 assert.match(page,/unit_radial_actions\.js/);
 const code=fs.readFileSync(path.join(root,"live_app.js"),"utf8");
 assert.doesNotMatch(code,/onUnitActivated:|onHexActivated:/,"Double-click never bypasses confirmation");
 console.log("Legacy live radial actions: legal menu, target/weapon, retarget, cancel, stale action, dead/enemy, friendly target, global actions and single-submit pass");
}
main().catch(error=>{console.error(error);process.exitCode=1;});
