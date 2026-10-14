// Pure functional tests of the complete actual policy apply/registered hook.
// VM imports stub only service, SDK and filesystem boundaries; completion.js
// remains the original real implementation. No HTTP/model/SSH/ROS is performed.
import test from 'node:test';
import assert from 'node:assert/strict';
import {readFileSync,existsSync} from 'node:fs';
import {createHash,randomUUID} from 'node:crypto';
import vm from 'node:vm';

const beforeURL=new URL('../../before/mhs/policy/index.js',import.meta.url);
const completionURL=existsSync(new URL('./completion.js',import.meta.url))
  ?new URL('./completion.js',import.meta.url):new URL('../../../../mhs/policy/completion.js',import.meta.url);
const completionSource=readFileSync(completionURL,'utf8');
assert.equal(createHash('sha256').update(completionSource).digest('hex'),
  'f7c80a377e55a1c34fb6917c40d7777b0e3d905300416911331e56dcf66ccce7');
const policySource=readFileSync(new URL('./index.js',import.meta.url),'utf8');
const ROOT='/mnt/sda/omni-yezh/hospital-delivery-agent';
const SESSION='agnes:local:local-dev:cli:workspace:ward-'+'a'.repeat(32);
const current=()=>({run_id:'a'.repeat(32),controller_session:SESSION,mhs_finished:true,pose_fresh:true,
  stopped_confirmed:true,motion_guard:{healthy:true},mhs_budget:{end_wall:null,stopped_reason:null},
  smart_ward:{episode:1,verification_event_version:7,watch:{enabled:true},scene_fresh:true,
    incidents:[{incident_id:'i1',kind:'low_light',status:'resolved',episode:1}]}});

// Exact public return envelope: official TurnStoppingReturn schema is a strict
// union with additionalProperties:false; terminalVerification is internal only.
// Root separately tested these shapes through pinned official validateHook.
function validReturn(value){
  if(!value||typeof value!=='object'||Array.isArray(value))return false;
  const keys=Object.keys(value).sort();
  return value.action==='stop'?JSON.stringify(keys)==='["action"]'
    :value.action==='continue'&&JSON.stringify(keys)==='["action","note"]'&&typeof value.note==='string';
}
function synthetic(context,exports){
  return new vm.SyntheticModule(Object.keys(exports),function(){
    for(const [key,value]of Object.entries(exports))this.setExport(key,value);
  },{context});
}
async function harness({state=current(),source=policySource,meterError=null,fetchError=null,
  stateOK=true,finalizeOK=true}={}){
  const seen={hooks:new Map(),logs:[],requests:[],meter:0,effects:[],verifier:null};
  const context=vm.createContext({URL,AbortSignal,Date,console,fetch:async(url,options)=>{
    seen.requests.push({url,options});
    if(fetchError)throw fetchError;
    if(url.endsWith('/api/state'))return {ok:stateOK,json:async()=>state};
    assert.equal(url,'http://127.0.0.1:4188/api/ward/finalize');
    return {ok:finalizeOK,json:async()=>({ok:finalizeOK})};
  }});
  const completion=new vm.SourceTextModule(completionSource,{context,identifier:completionURL.href});
  await completion.link(()=>{throw Error('unexpected_completion_dependency');});
  const imports={
    'node:fs':{readFileSync:url=>String(url).endsWith('/hospital-mhs.yaml')?'{}':'synthetic instructions',
      appendFileSync:(path,text)=>seen.logs.push({path,value:JSON.parse(text)})},
    'node:crypto':{randomUUID},'node:sqlite':{DatabaseSync:class{
      prepare(){return {all:()=>[]};}close(){}}},
    './business.js':{connectBusiness:async(extension,onVerification)=>{
      seen.verifier=onVerification;return async()=>{};}},
    './scope.js':{allowTool:()=>({allow:true})},
    './session-scope.js':{checkSessionRun:async()=>({allow:true})},
    './context.js':{hospitalContext:sections=>({sections})},
    './runtime.js':{root:ROOT,data:ROOT+'/runs/smart-ward-v1',smart:true},
    './model-requests.js':{syncModelRequests:async()=>{seen.meter++;if(meterError)throw meterError;},
      meterError:()=>{throw Error('unexpected_meter_error_sink');}},
  };
  const policy=new vm.SourceTextModule(source,{context,identifier:new URL('./index.js',import.meta.url).href,
    initializeImportMeta(meta){meta.url=new URL('./index.js',import.meta.url).href;}});
  await policy.link(name=>{
    if(name==='./completion.js')return completion;
    if(!Object.hasOwn(imports,name))throw Error('unexpected_policy_dependency:'+name);
    return synthetic(context,imports[name]);
  });
  await policy.evaluate();
  const extension={registerHook:(name,hook)=>{
    assert.equal(seen.hooks.has(name),false);seen.hooks.set(name,hook);return ()=>{};}};
  await policy.namespace.hospitalMHS.apply({extension:()=>extension,effect:value=>seen.effects.push(value)});
  assert.equal(typeof seen.hooks.get('turn_stopping'),'function');
  const invoke=async(reason='completed')=>JSON.parse(JSON.stringify(await seen.hooks.get('turn_stopping')(
    {proposedReason:reason,turn:1,step:8},{session:{key:SESSION}})));
  return {state,seen,invoke,verify:()=>seen.verifier(SESSION,
    {run_id:state.run_id,episode:state.smart_ward.episode,
      verification_event_version:state.smart_ward.verification_event_version})};
}

test('missing verification emits ABI-valid continuation and keeps internal reminder only in its log',async()=>{
  const h=await harness();const result=await h.invoke();
  assert.equal(result.action,'continue');assert.equal(validReturn(result),true);
  assert.match(result.note,/hospital_verify_services/);assert.equal(Object.hasOwn(result,'terminalVerification'),false);
  assert.equal(h.seen.logs.length,1);assert.equal(h.seen.logs[0].value.feedback.terminalVerification,true);
  assert.equal(h.seen.requests.some(r=>r.url.endsWith('/finalize')),false);
});
test('actual before source reproduces invalid additional property',
  {skip:!existsSync(beforeURL)&&'before source not supplied; local reproduction only'},async()=>{
    const source=readFileSync(beforeURL,'utf8');
    assert.equal(createHash('sha256').update(source).digest('hex'),
      '7453248ffbe381a81ff94b46359ca35f12db0343ae3db87cdc97db0ba3235082');
    const h=await harness({source});const result=await h.invoke();
    assert.equal(result.action,'continue');assert.equal(result.terminalVerification,true);
    assert.equal(validReturn(result),false);
  });
test('accepted current verification still delegates finalize once and returns plain stop',async()=>{
  const h=await harness();h.verify();const result=await h.invoke();
  assert.deepEqual(result,{action:'stop'});assert.equal(validReturn(result),true);
  const calls=h.seen.requests.filter(r=>r.url.endsWith('/api/ward/finalize'));
  assert.equal(calls.length,1);assert.equal(calls[0].options.method,'POST');
  assert.deepEqual(JSON.parse(calls[0].options.body),{expected_run_id:h.state.run_id,session_key:SESSION});
});
test('stale verification key cannot finalize the current event version',async()=>{
  const h=await harness();h.verify();h.state.smart_ward.verification_event_version++;
  const result=await h.invoke();assert.equal(result.action,'continue');assert.equal(validReturn(result),true);
  assert.equal(h.seen.requests.some(r=>r.url.endsWith('/finalize')),false);
});
test('same-key terminal reminders remain bounded at exactly two',async()=>{
  const h=await harness();
  assert.equal((await h.invoke()).action,'continue');assert.equal((await h.invoke()).action,'continue');
  assert.deepEqual(await h.invoke(),{action:'stop'});
  assert.equal(h.seen.logs.length,2);assert.equal(h.seen.requests.some(r=>r.url.endsWith('/finalize')),false);
});
test('ordinary unfinished work emits a legal plain continuation',async()=>{
  const state=current();state.mhs_finished=false;state.smart_ward.incidents[0].status='detected';
  const h=await harness({state});const result=await h.invoke();
  assert.equal(result.action,'continue');assert.equal(validReturn(result),true);
  assert.equal(Object.hasOwn(h.seen.logs[0].value.feedback,'terminalVerification'),false);
});
test('operator stop, watch disabled, blocked budget and different controller keep plain stop',async()=>{
  for(const mutate of [s=>s.operator_stopped={operation_id:'stop'},
    s=>s.smart_ward.watch.enabled=false,s=>s.mhs_budget.stopped_reason='decision_limit',
    s=>s.controller_session='other']){
    const state=current();mutate(state);const h=await harness({state});
    assert.deepEqual(await h.invoke(),{action:'stop'});
    assert.equal(h.seen.requests.some(r=>r.url.endsWith('/finalize')),false);
  }
});
test('existing recovery, stale scene and unhealthy-guard fault branches remain stop',async()=>{
  for(const mutate of [s=>s.smart_ward.recovery_required=true,
    s=>s.smart_ward.scene_fresh=false,s=>s.motion_guard.healthy=false]){
    const state=current();state.mhs_finished=false;state.smart_ward.incidents[0].status='detected';mutate(state);
    const h=await harness({state});assert.deepEqual(await h.invoke(),{action:'stop'});
    assert.equal(h.seen.requests.some(r=>r.url.endsWith('/finalize')),false);
  }
});
test('existing not-stationary stop and failed finalize behavior remain unchanged',async()=>{
  const state=current();state.stopped_confirmed=false;
  const moving=await harness({state});assert.deepEqual(await moving.invoke(),{action:'stop'});
  const rejected=await harness({finalizeOK:false});rejected.verify();
  assert.deepEqual(await rejected.invoke(),{action:'stop'});
  assert.equal(rejected.seen.logs[0].value.error,'episode_finalize_rejected');
});
test('existing exception and non-completed paths remain stop without added retry',async()=>{
  for(const options of [{meterError:Error('synthetic meter timeout')},
    {fetchError:Error('synthetic state timeout')},{stateOK:false}]){
    const h=await harness(options);assert.deepEqual(await h.invoke(),{action:'stop'});
    assert.equal(h.seen.requests.filter(r=>r.url.endsWith('/finalize')).length,0);
  }
  const h=await harness();assert.deepEqual(await h.invoke('interrupted'),{action:'stop'});
  assert.equal(h.seen.meter,0);assert.equal(h.seen.requests.length,0);
});
