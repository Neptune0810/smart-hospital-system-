// Pure/mock only: actual candidate proxy/Pi control flow, synthetic transport, no model/network.
import test from 'node:test';
import assert from 'node:assert/strict';
import {existsSync,readFileSync} from 'node:fs';
import {createHash} from 'node:crypto';
import {stripTypeScriptTypes} from 'node:module';
import vm from 'node:vm';
import {modelRuntime} from '../agent/agh-source-mhs/packages/host/src/assemble/model-runtime.ts';
import {createHospitalInferAdmission} from './model_admission.mjs';

const request=()=>({sessionKey:'session-current',slot:'primary',route:'test-route',model:'test-model',derivedHash:'a'.repeat(64),messages:[]});
const options=()=>({signal:new AbortController().signal,toolNames:[]});
const collect=async stream=>{const rows=[];for await(const row of stream)rows.push(row);return rows;};
const deferred=()=>{let resolve;const promise=new Promise(r=>resolve=r);return {promise,resolve};};
function provider(name,calls){return {models:()=>[name],count:async()=>({tokens:1}),async *infer(req,opts){calls.push({name,req,opts});yield {type:'done',reason:'stop'};}};}
const snapshot=(name,calls)=>({provider:provider(name,calls),contractForModel:()=>({contract_id:name})});

test('proxy is lazy AsyncIterable, admission precedes one lower dispatch and forces retry=false',async()=>{
  const calls=[],gate=[];
  const runtime=modelRuntime(snapshot('old',calls),async()=>gate.push(calls.length));
  const stream=runtime.provider.infer(request(),options());
  assert.equal(typeof stream[Symbol.asyncIterator],'function');assert.equal(gate.length,0);
  await collect(stream);assert.deepEqual(gate,[0]);assert.equal(calls.length,1);assert.equal(calls[0].opts.retry,false);
  await collect(stream);assert.equal(calls.length,1);
});
test('model snapshot captured before admission await and before first iterator consumption',async()=>{
  const calls=[],gate=deferred(),entered=deferred();
  const runtime=modelRuntime(snapshot('old',calls),async()=>{entered.resolve();await gate.promise;});
  const stream=runtime.provider.infer(request(),options());
  runtime.publish(snapshot('new',calls));
  const done=collect(stream);await entered.promise;gate.resolve();await done;
  assert.equal(calls[0].name,'old');
  await collect(runtime.provider.infer(request(),options()));assert.equal(calls[1].name,'new');
});
test('quota rejection emits nonretry terminal error without a lower dispatch',async()=>{
  const calls=[],runtime=modelRuntime(snapshot('one',calls),async()=>{throw Object.assign(Error('private details'),{code:'QUOTA'});});
  const rows=await collect(runtime.provider.infer(request(),options()));
  assert.equal(calls.length,0);assert.equal(rows[0].code,'QUOTA');assert.equal(rows[0].retryable,false);
  assert.equal(JSON.stringify(rows).includes('private details'),false);
});
test('abort before admission or after committed reservation never delegates',async()=>{
  for(const before of [true,false]){
    const calls=[],ac=new AbortController();let reserved=0;
    const runtime=modelRuntime(snapshot('one',calls),async()=>{reserved++;ac.abort();});
    if(before)ac.abort();
    const rows=await collect(runtime.provider.infer(request(),{signal:ac.signal,toolNames:[]}));
    assert.equal(calls.length,0);assert.equal(reserved,before?0:1);assert.equal(rows[0].code,'ABORTED');
  }
});
test('request identity mutation while awaiting admission fails closed',async()=>{
  const calls=[],req=request();const runtime=modelRuntime(snapshot('one',calls),async()=>{req.sessionKey='other';});
  const rows=await collect(runtime.provider.infer(req,options()));
  assert.equal(calls.length,0);assert.equal(rows[0].code,'FORMAT');
});
test('unset callback keeps old MHS Provider options and model/count behavior',async()=>{
  const calls=[],runtime=modelRuntime(snapshot('legacy',calls));
  await collect(runtime.provider.infer(request(),options()));
  assert.equal(calls[0].opts.retry,undefined);assert.deepEqual(runtime.provider.models(),['legacy']);
  assert.deepEqual(await runtime.provider.count(request(),{signal:options().signal}),{tokens:1});
});

function fakeBusiness({limit=60,invalidContext=false,failPost=false}={}){
  let admitted=0;const seen=new Set(),calls=[];
  const context={schema:'hospital-model-admission89-v1',run_id:'run-current',session_key:'session-current',
    episode:1,scene_epoch:'epoch-current',budget_start_wall:1000,limit:60};
  const fetchImpl=async(url,opts)=>{
    calls.push({url,opts});
    if(url.includes('/context?')){
      if(!url.endsWith('session-current'))return {ok:false,json:async()=>({})};
      return {ok:true,json:async()=>({result:invalidContext?{}:context})};
    }
    if(failPost)throw Error('ambiguous after commit');
    const body=JSON.parse(opts.body),allowed=!seen.has(body.dispatch_id)&&admitted<limit;
    if(allowed){seen.add(body.dispatch_id);admitted++;}
    return {ok:true,json:async()=>({result:{...context,allowed,dispatch_id:body.dispatch_id,
      metadata_hash:body.metadata_hash,ordinal:admitted}})};
  };
  return {fetchImpl,calls,get admitted(){return admitted;}};
}
test('helper approves exact context and records reservation, not an attested dispatch',async()=>{
  const business=fakeBusiness(),records=[];
  const admit=createHospitalInferAdmission({fetchImpl:business.fetchImpl,record:r=>records.push(r)});
  await admit(request(),options());assert.equal(business.admitted,1);
  assert.equal(records[0].status,'reserved');assert.equal(records[0].actual_dispatch_attested,false);
});
test('unknown literal session, missing metadata or malformed context cannot reach reservation',async()=>{
  for(const kind of ['title','missing','context']){
    const business=fakeBusiness({invalidContext:kind==='context'}),req=request();
    if(kind==='title')req.sessionKey='title:random';if(kind==='missing')delete req.derivedHash;
    const admit=createHospitalInferAdmission({fetchImpl:business.fetchImpl});
    await assert.rejects(admit(req,options()));assert.equal(business.calls.filter(c=>c.opts.method==='POST').length,0);
  }
});
test('mock 61st Provider request is denied before fake transport; no payload-hash dedup',async()=>{
  const business=fakeBusiness(),calls=[];
  const runtime=modelRuntime(snapshot('fake',calls),createHospitalInferAdmission({fetchImpl:business.fetchImpl}));
  for(let n=0;n<61;n++)await collect(runtime.provider.infer(request(),options()));
  assert.equal(business.admitted,60);assert.equal(calls.length,60);
});
test('replayed operation cannot authorize a second lower dispatch',async()=>{
  const business=fakeBusiness(),calls=[];
  const admit=createHospitalInferAdmission({fetchImpl:business.fetchImpl,idFactory:()=> 'same-id'});
  const runtime=modelRuntime(snapshot('fake',calls),admit);
  await collect(runtime.provider.infer(request(),options()));await collect(runtime.provider.infer(request(),options()));
  assert.equal(business.admitted,1);assert.equal(calls.length,1);
});
test('unknown POST outcome is not retried; no prompt/image/keys in audit',async()=>{
  const business=fakeBusiness({failPost:true}),calls=[],records=[],req=request();
  req.messages=[{secret:'SYNTHETIC-PROMPT-DO-NOT-LOG'}];req.apiKey='SYNTHETIC-KEY-DO-NOT-LOG';
  const runtime=modelRuntime(snapshot('fake',calls),createHospitalInferAdmission({fetchImpl:business.fetchImpl,record:r=>records.push(r)}));
  await collect(runtime.provider.infer(req,options()));
  assert.equal(calls.length,0);assert.equal(business.calls.filter(c=>c.opts.method==='POST').length,1);
  assert.equal(JSON.stringify(records).includes('DO-NOT-LOG'),false);
});
test('timeout rejects without dispatch',async()=>{
  const calls=[];
  const fetchImpl=async(_url,{signal})=>new Promise((_,reject)=>signal.addEventListener('abort',()=>reject(Error('timeout')),{once:true}));
  const runtime=modelRuntime(snapshot('fake',calls),createHospitalInferAdmission({fetchImpl,timeoutMs:5}));
  const keepAlive=setTimeout(()=>{},100);
  try{await collect(runtime.provider.infer(request(),options()));}finally{clearTimeout(keepAlive);}
  assert.equal(calls.length,0);
});

// Run the real complete PiAdapter method, stubbing only its imported dependencies
// and wire transport. This exercises its AUTH loop rather than copying its predicate.
async function loadPi(source){
  const imports={
    '@agnes/protocol':{modelImageInputError:()=>undefined},
    '../../adapter.js':{WireAdapter:class{credentialFor(){return undefined;}}},
    '../../errors.js':{AiSetupError:class extends Error{}},
    '../../hash.js':{sha256Hex:()=> 'a'.repeat(64)},
    './input-limits.js':{providerPayloadImageError:()=>undefined},
    './probe.js':{probeInference:()=>{}},'./probe-models.js':{probeModelsEndpoint:()=>{}},
    './providers/agnes-ai.js':{AGNES_AI_BASE_URL:'https://synthetic.invalid'},
    './to-context.js':{toContext:()=>({context:{messages:[]}})},
    './translate.js':{responseMeta:()=>({}),translateEvent:event=>[event],withResponse:()=>({})},
    './wire.js':{AMBIENT_CREDENTIAL_APIS:new Set(),streamOverApi:()=>{throw Error('network forbidden');}},
  };
  const context=vm.createContext({URL,Buffer,Request,AbortController,performance,structuredClone,setTimeout,clearTimeout,
    fetch:()=>{throw Error('network forbidden');}});
  const module=new vm.SourceTextModule(stripTypeScriptTypes(source,{mode:'strip'}),{context});
  await module.link(async name=>{
    assert.ok(imports[name],'unexpected runtime import '+name);
    const exports=imports[name];return new vm.SyntheticModule(Object.keys(exports),function(){for(const [key,value]of Object.entries(exports))this.setExport(key,value);},{context});
  });
  await module.evaluate();return module.namespace.PiAdapter;
}
async function piAttemptCount(PiAdapter,{retry,errorCode='AUTH'}={}){
  let attempts=0,recoveries=0;
  const model={id:'test-model',name:'Test',input:['text'],reasoning:false,contextWindow:8192,maxTokens:1024,
    cost:{input:0,output:0,cacheRead:0,cacheWrite:0}};
  const adapter=new PiAdapter({manualRoutes:[{route:'test-route',baseUrl:'https://synthetic.invalid',api:'openai-completions',models:[model]}],
    resolveCredential:async()=> 'SYNTHETIC-TEST-CREDENTIAL',recoverRejectedAuth:async()=>{recoveries++;return true;},
    maxRetries:1,sleep:async()=>{},async *streamImpl(_model,_context,opts){
      assert.equal(opts.maxRetries,0);attempts++;
      if(attempts===1)yield {type:'error',reason:'error',code:errorCode,message:'synthetic',retryable:true};
      else yield {type:'done',reason:'stop'};
    }});
  const events=await collect(adapter.stream('test-route',request(),{signal:options().signal,toolNames:[],
    timeoutMs:{firstToken:1000,total:2000},...(retry===false?{retry:false}:{})}));
  return {attempts,recoveries,events};
}
test('real original Pi AUTH loop demonstrates retry:false gap; candidate prevents its second transport',async()=>{
  const evidence=[new URL('../../official-admission-source88.json',import.meta.url),
    new URL('../.cache/official-admission-source88.json',import.meta.url)].find(path=>existsSync(path));
  assert.ok(evidence,'fixed original source88 fixture required; no network lookup');
  const upstream=JSON.parse(readFileSync(evidence,'utf8'));
  assert.equal(upstream.commit,'57d0c34b33527294042b7ab8a60d7c7c934e932e');
  const originalSource=upstream.files['packages/ai/src/adapters/pi/index.ts'].text;
  assert.equal(createHash('sha256').update(originalSource).digest('hex'),
    '11cccc69e1e3781b8bb5bd4451c07f5d77c3dd889b83a614bb7ccf6a512f9f8b');
  const original=await loadPi(originalSource);
  const candidate=await loadPi(readFileSync(new URL('../agent/agh-source-mhs/packages/ai/src/adapters/pi/index.ts',import.meta.url),'utf8'));
  const before=await piAttemptCount(original,{retry:false}),after=await piAttemptCount(candidate,{retry:false});
  assert.equal(before.attempts,2);assert.equal(before.recoveries,1);
  assert.equal(after.attempts,1);assert.equal(after.recoveries,0);assert.equal(after.events.at(-1).code,'AUTH');
});
test('candidate preserves ordinary AUTH recovery while false also blocks transient resend',async()=>{
  const Pi=await loadPi(readFileSync(new URL('../agent/agh-source-mhs/packages/ai/src/adapters/pi/index.ts',import.meta.url),'utf8'));
  assert.equal((await piAttemptCount(Pi)).attempts,2);
  assert.equal((await piAttemptCount(Pi,{retry:false,errorCode:'RATE_LIMIT'})).attempts,1);
  assert.equal((await piAttemptCount(Pi,{errorCode:'RATE_LIMIT'})).attempts,2);
});
