// Facade tests use the real workbench source with in-memory IO and the SDK boundary mocked.
// They verify notification/stop behavior; they do not claim a real Agnes or Isaac pass.
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import vm from 'node:vm';
import {queuedTurnTerminal} from './notification-terminal.js';

const source=readFileSync(new URL('./workbench.js',import.meta.url),'utf8').replace(/^import .*;\r?$/gm,'');
function fixture({busy=false,mode='smart-ward-v1',promptError=false,stopError=false}={}){
  const requests=[],sdkCalls=[],logs=[],intervals=[];
  let state={run_id:'run-01',controller_session:'agnes:local:local-dev:cli:workspace:ward-run-01',operator_stopped:false,pose_fresh:true,navigation_ready:true,motion_guard:{healthy:true},mhs_budget:{start_wall:1000},smart_ward:{watch:{enabled:true,phase:'watching'}}};
  let notifications=[],sceneBlock=null,turnResolve,ledger=[];
  const handle={
    projectUIOpening:async()=>({timeline:{opState:busy?{}:null,pendingInputs:[]}}),
    followUp:async(text,options)=>{sdkCalls.push({method:'followUp',text,...options});return 1;},
    prompt:(text)=>{sdkCalls.push({method:'prompt',text});return promptError?Promise.reject(Error('model_unavailable')):new Promise(resolve=>{turnResolve=resolve;});},
    cancel:async()=>{sdkCalls.push({method:'cancel'});},removeQueued:async()=>{}
  };
  const client={connectionState:'connected',initialize:async()=>{},close:async()=>{},session:{load:async()=>handle,new:async()=>handle}};
  const context=vm.createContext({
    http:{createServer:()=>({listen(){},close(){}})},
    readFileSync(path){
      if(path.endsWith('/configs/stations.json'))return '{"station_b":{"label":"B护士站"},"station_c":{"label":"C护士站"}}';
      if(path.endsWith('/discovery.json'))return '{"socketPath":"/test/agent/agh-home-mhs/data/daemon/agnesd.sock","ready":true}';
      const e=Error('missing');e.code='ENOENT';throw e;
    },
    writeFileSync(){},renameSync(){},mkdirSync(){},appendFileSync:(path,text)=>logs.push({path,text}),spawn(){throw Error('not_in_test');},
    DatabaseSync:class{prepare(){return {all:()=>ledger.map(row=>({...row,data:JSON.stringify(row.data)}))};}close(){}},createClient:()=>client,fileJournal:()=>({}),queuedTurnTerminal,
    process:{env:{HOSPITAL_ROOT:'/test',HOSPITAL_MODE:mode},pid:123,on(){},exit(){}},
    fetch:async(url,options={})=>{
      const path=new URL(url).pathname,a=options.body?JSON.parse(options.body):undefined;requests.push({path,a});
      let result;
      if(path==='/api/state')result=structuredClone(state);
      else if(path.endsWith('/notifications/claim'))result=notifications.shift()??null;
      else if(path.endsWith('/notifications/ack'))result={accepted:true};
      else if(path==='/api/admin/stop-run'){if(stopError)throw Error('stop_service_unreachable');state.operator_stopped=true;state.smart_ward.watch.enabled=false;result={stopped:true};}
      else if(path==='/api/admin/smart-ward/command'){if(sceneBlock)await sceneBlock;result={pending:true};}
      else if(path==='/api/admin/claim'||path==='/api/admin/smart-ward/start')result={ok:true};
      else throw Error('unexpected_api '+path);
      return {ok:true,json:async()=>path==='/api/state'?result:{ok:true,result}};
    },
    AbortSignal,URL,Buffer,Date,Math,Promise,console,
    setInterval(fn){intervals.push(fn);return {unref(){}};},
    setTimeout(fn,ms){const timer=setTimeout(fn,ms);timer.unref();return timer;}
  });
  vm.runInContext(source+'\nglobalThis.testAPI={notificationTick,notificationTerminalTick,dispatchCommand,notificationState};',context,{filename:'workbench.js'});
  return {api:context.testAPI,requests,sdkCalls,logs,intervals,
    enqueue(id='notification-01',attempts=1){notifications.push({notification_id:id,run_id:state.run_id,session_id:state.controller_session,text:'[设备事件] B护士站有封装标本待取。',event_ids:['incident-01'],attempts});},
    state:()=>state,setLedger(value){ledger=value;},setBusy(value){busy=value;},setSceneBlock(value){sceneBlock=value;},finishTurn(result={reason:'completed'}){turnResolve?.(result);}
  };
}
const flush=()=>new Promise(resolve=>setImmediate(resolve));
function queuedLedger(reason='error'){
  const content=[{type:'text',text:'[设备事件] B护士站有封装标本待取。'}];
  return [{seq:1,ts:'1970-01-01T00:16:41Z',type:'inbox',data:{items:[{itemId:'i1',commandId:'notification-01',kind:'follow_up',target:'next-turn',content}]}},
    {seq:2,ts:'1970-01-01T00:16:42Z',type:'turn/end',data:{reason:'completed'}},
    {seq:3,ts:'1970-01-01T00:16:43Z',type:'inbox',data:{items:[]}},
    {seq:4,ts:'1970-01-01T00:16:43Z',type:'user/message',data:{kind:'follow_up',content}},
    {seq:5,ts:'1970-01-01T00:16:43Z',type:'turn/start',data:{turn:2}},
    {seq:6,ts:'1970-01-01T00:16:44Z',type:'turn/end',data:{reason}}].map(row=>({...row,lane_hex:'6D61696E',origin:row.type==='user/message'?'principal':'system',trust:'trusted'}));
}
{
  const f=fixture({busy:true});f.enqueue();await Promise.all([f.api.notificationTick(),f.api.notificationTick()]);
  assert.equal(f.requests.filter(r=>r.path.endsWith('/notifications/claim')).length,1,'overlapping ticks must have one claim');
  assert.equal(f.sdkCalls[0].method,'followUp');assert.equal(f.sdkCalls[0].commandId,'notification-01');
  const ack=f.requests.find(r=>r.path.endsWith('/notifications/ack')).a;
  assert.equal(ack.status,'delivered');assert.equal(ack.details.expected_run_id,'run-01');assert.match(ack.details.worker_id,/workbench-123-/);
}
{
  const f=fixture();f.enqueue();await f.api.notificationTick();
  assert.equal(f.sdkCalls[0].method,'prompt');assert.equal(f.api.notificationState.phase,'idle','model completion must not block notification loop');
  assert.equal(f.requests.filter(r=>r.path.endsWith('/notifications/ack')).length,1);f.finishTurn();await flush();
}
{
  const f=fixture({promptError:true});f.enqueue();await f.api.notificationTick();await flush();
  assert.deepEqual(f.requests.filter(r=>r.path.endsWith('/notifications/ack')).map(r=>r.a.status),['delivered','retry'],'late model failure must remain retryable after SDK acceptance');
}
{
  const f=fixture();f.enqueue();await f.api.notificationTick();f.finishTurn({reason:'error',stopReason:'error'});await flush();
  assert.equal(f.requests.filter(r=>r.path.endsWith('/notifications/ack')).at(-1).a.status,'retry','resolved error TurnResult must not be mistaken for completed');
}
{
  const f=fixture({promptError:true});f.enqueue('notification-03',3);await f.api.notificationTick();await flush();
  assert.ok(f.state().operator_stopped,'repeated model failure must stop safely instead of infinite retry');
  assert.equal(f.requests.filter(r=>r.path.endsWith('/notifications/ack')).length,1,'terminal failure must not be requeued');
}
{
  const f=fixture();f.enqueue();await f.api.notificationTick();f.finishTurn({reason:'max_steps'});await flush();
  assert.ok(f.state().operator_stopped,'budget/max-step result must stop instead of restarting model');
}
{
  const f=fixture();f.state().smart_ward.watch.enabled=false;f.enqueue();await f.api.notificationTick();
  assert.equal(f.sdkCalls.length,0);assert.equal(f.requests.some(r=>r.path.endsWith('/notifications/claim')),false,'paused watch must never consume event');
}
{
  const f=fixture();let release;f.setSceneBlock(new Promise(resolve=>{release=resolve;}));
  const normal=f.api.dispatchCommand({action:'smart_event',operation_id:'scene-test-01',expected_run_id:'run-01',session_id:f.state().controller_session,kind:'set_brightness',arguments:{room_id:'room301',brightness_percent:10}});
  await flush();assert.ok(f.requests.some(r=>r.path==='/api/admin/smart-ward/command'));
  const stopped=await f.api.dispatchCommand({action:'stop',operation_id:'stop-test-01',expected_run_id:'run-01'});
  assert.equal(stopped.status,'stop_requested');assert.ok(f.state().operator_stopped,'stop must bypass blocked normal command');
  f.enqueue();await f.api.notificationTick();assert.equal(f.sdkCalls.some(c=>c.method==='prompt'||c.method==='followUp'),false,'stop must prevent event wake');release();await normal;
}
{
  const f=fixture({mode:'mhs-v2'});f.enqueue();await f.api.notificationTick();assert.equal(f.requests.length,0,'legacy profile must have no background event worker');
}
{
  const f=fixture({busy:true});f.enqueue();await f.api.notificationTick();f.setLedger(queuedLedger());
  await f.api.notificationTerminalTick();
  assert.ok(f.state().operator_stopped,'attributable queued turn error must stop without waiting for batch timeout');
  assert.equal(f.sdkCalls.filter(call=>call.method==='followUp').length,1,'delivered input must never be resent by terminal monitor');
  assert.equal(f.requests.filter(r=>r.path.endsWith('/notifications/ack')).length,1,'terminal monitor must not retry delivered input');
  assert.equal(f.api.notificationState.phase,'fault');assert.equal(f.api.notificationState.last_error.terminal_seq,6);
  await f.api.notificationTerminalTick();
  assert.equal(f.requests.filter(r=>r.path==='/api/admin/stop-run').length,1,'same failure cannot repeatedly stop');
}
{
  const f=fixture({busy:true});f.enqueue();await f.api.notificationTick();f.setLedger(queuedLedger('completed'));
  await f.api.notificationTerminalTick();assert.equal(f.state().operator_stopped,false,'normal queued/native completed turn must not fault');
}
{
  const f=fixture({busy:true,stopError:true});f.enqueue();await f.api.notificationTick();f.setLedger(queuedLedger());
  await f.api.notificationTerminalTick();
  assert.equal(f.api.notificationState.phase,'fault','failed stop API must preserve the terminal fault');
  assert.equal(f.api.notificationState.paused,true,'failed stop API cannot resume event deliveries');
  assert.match(f.api.notificationState.last_error.stop_error,/stop_service_unreachable/);
  assert.equal(f.api.notificationState.last_error.terminal_seq,6,'raw terminal identity remains available');
  assert.ok(f.sdkCalls.some(call=>call.method==='cancel'),'owning session cancellation is attempted even when stop API fails');
  assert.ok(f.logs.some(row=>JSON.parse(row.text).kind==='queued_turn_stop_failure'),'stop failure is recorded separately');
  f.enqueue('another-notification');await f.api.notificationTick();
  assert.equal(f.sdkCalls.filter(call=>call.method==='followUp').length,1,'fault cannot resubmit or admit another device event');
}
{
  const f=fixture({busy:true});f.enqueue();await f.api.notificationTick();const rows=queuedLedger();rows[1].data.reason='error';f.setLedger(rows.slice(0,2));
  await f.api.notificationTerminalTick();assert.equal(f.state().operator_stopped,false,'current unrelated failing turn is not the queued input turn');
  f.state().run_id='run-02';f.setLedger(rows);await f.api.notificationTerminalTick();
  assert.equal(f.state().operator_stopped,false,'new run clears earlier queued monitors');
}
{
  const f=fixture();f.enqueue('direct-notification');await f.api.notificationTick();
  f.setBusy(true);f.enqueue();await f.api.notificationTick();f.setLedger(queuedLedger());
  await f.api.notificationTerminalTick();
  assert.equal(f.state().operator_stopped,false,'raw ledger monitor cannot race an owned direct prompt outcome');
  f.finishTurn({reason:'completed',lastSeq:2});await flush();await f.api.notificationTerminalTick();
  assert.ok(f.state().operator_stopped,'queued turn failure is still checked after direct prompt settles');
}
// Browser JS and embedded dashboard script must parse without starting a browser.
for(const name of ['dashboard.js','dashboard-v2.js'])new vm.Script(readFileSync(new URL('./'+name,import.meta.url),'utf8'),{filename:name});
const html=readFileSync(new URL('./dashboard.html',import.meta.url),'utf8');
for(const [index,match] of [...html.matchAll(/<script(?:\s[^>]*)?>([\s\S]*?)<\/script>/g)].entries())if(match[1].trim())new vm.Script(match[1],{filename:'dashboard.html#script-'+index});
for(const id of ['ward-start','ward-insert','ward-dark','ward-receiver-pause','ward-lamp-fault','ward-incidents','ward-devices'])assert.ok(html.includes('id="'+id+'"'));
{
  // Execute the actual renderer with a minimal DOM, including receipt linkage and stale readings.
  class Element{
    constructor(tag,text=''){this.tagName=tag;this._text=text;this.children=[];this.dataset={};this.classList={toggle(){}};}
    set textContent(value){this._text=String(value);this.children=[];}
    get textContent(){return this._text+this.children.map(c=>c.textContent).join('');}
    append(...children){this.children.push(...children);}replaceChildren(...children){this.children=children;this._text='';}
    querySelectorAll(){return [];}
  }
  const elements=new Map(),demoId=id=>{if(!elements.has(id))elements.set(id,new Element('div'));return elements.get(id);};
  const context=vm.createContext({demoId,node:(tag,text)=>new Element(tag,text),stationNames:{station_b:'B护士站',lab:'检验交接点'},phaseNames:{completed:'已完成',running:'执行中',planned:'计划'},Date});
  const renderer=readFileSync(new URL('./dashboard.js',import.meta.url),'utf8').split('const wardStatusNames=')[1];
  vm.runInContext('const wardStatusNames='+renderer+'\nglobalThis.render=renderSmartWard;',context);
  const devices=Object.fromEntries(['hospital-robot-01','hospital-cabinet-b','hospital-cabinet-c','hospital-cabinet-lab','hospital-light-301','hospital-light-303','hospital-sensor-301','hospital-sensor-303'].map(id=>[id,{fresh:true,available:true}]));
  devices['hospital-sensor-301']={fresh:false,available:false,relative_illumination:28,source:'simulation_model'};
  devices['hospital-cabinet-lab']={fresh:true,available:true,door_open_fraction:0,specimens:['S-01'],capacity:6};
  context.render({orders:{'O-01':{loaded:false,receipt:{receipt_id:'R-01',sim_time:50}}},capacity:2,stopped_confirmed:true,smart_ward:{watch:{enabled:true,phase:'watching'},incidents:[{incident_id:'I-01',kind:'specimen_ready',status:'resolved',summary:'B站标本待取',station_id:'station_b',specimen_id:'S-01',order_id:'O-01',created_sim:1,resolved_wall:100},{incident_id:'I-02',kind:'low_light',status:'awaiting_verification',room_id:'room301',attempts:1}],devices,specimens:{'S-01':{location:'hospital-cabinet-lab'}},service_plan:{reason:'先送标本，同时恢复灯光',wall_time:100,steps:[{incident_id:'I-01',action:'deliver',station_id:'lab',status:'completed'},{incident_id:'I-02',action:'set_brightness',device_id:'hospital-light-301',status:'running'}]},maintenance_tickets:[]}});
  assert.equal(demoId('ward-devices').children.length,8);
  assert.match(demoId('ward-devices').children[6].textContent,/上次反馈.*可用状态未知.*相对照明指数 28.0/);
  assert.match(demoId('ward-incidents').textContent,/实际归属：检验接收柜/);assert.match(demoId('ward-incidents').textContent,/交接回执：R-01/);
  assert.equal(demoId('plan-steps').children[0].className,'plan-step completed');
  assert.equal(demoId('plan-steps').children[1].className,'plan-step running');
}
console.log('PASS smart workbench: busy/idle delivery, TurnResult/retry limits, paused watch, stop bypass, legacy isolation, UI syntax and evidence rendering');
