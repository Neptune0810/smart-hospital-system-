// Project-only pre-dispatch gate. Never reads account files or logs prompt/image/key content.
import {createHash, randomUUID} from 'node:crypto';
import {appendFileSync, mkdirSync} from 'node:fs';
import {join} from 'node:path';
export const ADMISSION_VERSION='smart-ward-v1.89';
const SCHEMA='hospital-model-admission89-v1';
const BASE='http://127.0.0.1:4188';
const finite=value=>typeof value==='number'&&Number.isFinite(value);
const fail=(code='TRANSPORT')=>Object.assign(new Error('project_model_admission_rejected'),{code});
const text=value=>typeof value==='string'&&value.length>0&&value.length<=512;

export function createHospitalInferAdmission({projectRoot,fetchImpl=globalThis.fetch,
    idFactory=randomUUID,timeoutMs=2500,record}={}) {
  if(typeof fetchImpl!=='function'||!finite(timeoutMs)||timeoutMs<=0||timeoutMs>2500)
    throw fail();
  const audit=record??(projectRoot?entry=>{
    const folder=join(projectRoot,'runs/smart-ward-v1');
    mkdirSync(folder,{recursive:true});
    appendFileSync(join(folder,'model-admission.jsonl'),JSON.stringify(entry)+'\n');
  }:()=>{});
  return async (request,{signal})=>{
    const dispatch_id=idFactory();
    let context;
    try {
      if(signal.aborted)throw fail('ABORTED');
      const metadata={session_key:request.sessionKey,slot:request.slot,route:request.route,
        model:request.model,derived_hash:request.derivedHash};
      if(!text(metadata.session_key)||!text(metadata.slot)||!text(metadata.route)||!text(metadata.model)
          ||typeof metadata.derived_hash!=='string'||!/^[0-9a-f]{64}$/.test(metadata.derived_hash))throw fail('QUOTA');
      // The context endpoint checks the literal key against the unique controller.
      // Random title/child/history session keys are never guessed into that scope.
      const requestSignal=AbortSignal.any([signal,AbortSignal.timeout(timeoutMs)]);
      const response=await fetchImpl(BASE+'/api/ward/model-admission/context?session_key='+encodeURIComponent(metadata.session_key),
        {signal:requestSignal});
      if(!response.ok)throw fail('QUOTA');
      context=(await response.json()).result;
      if(!context||context.schema!==SCHEMA||context.session_key!==metadata.session_key
          ||!text(context.run_id)||!text(context.scene_epoch)||!Number.isInteger(context.episode)||context.episode<1
          ||!finite(context.budget_start_wall)||context.limit!==60)throw fail();
      const metadata_hash=createHash('sha256').update(JSON.stringify(metadata)).digest('hex');
      const body={dispatch_id,expected_run_id:context.run_id,session_key:context.session_key,
        expected_episode:context.episode,expected_scene_epoch:context.scene_epoch,
        budget_start_wall:context.budget_start_wall,metadata_hash};
      // No automatic retry on an ambiguous reservation. Its committed slot, if any, stays consumed.
      const reserved=await fetchImpl(BASE+'/api/ward/model-admission/reserve',{
        method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body),signal:requestSignal});
      if(!reserved.ok)throw fail('QUOTA');
      const result=(await reserved.json()).result;
      if(!result||result.allowed!==true)throw fail('QUOTA');
      if(result.schema!==SCHEMA||result.dispatch_id!==dispatch_id||result.run_id!==context.run_id
          ||result.session_key!==context.session_key||result.episode!==context.episode
          ||result.scene_epoch!==context.scene_epoch||result.budget_start_wall!==context.budget_start_wall
          ||result.metadata_hash!==metadata_hash||!Number.isInteger(result.ordinal)||result.ordinal<1||result.ordinal>60)
        throw fail();
      audit({version:ADMISSION_VERSION,wall_time:Date.now()/1000,dispatch_id,status:'reserved',
        run_id:context.run_id,episode:context.episode,scene_epoch:context.scene_epoch,
        ordinal:result.ordinal,metadata_hash,actual_dispatch_attested:false});
    } catch(error) {
      try{audit({version:ADMISSION_VERSION,wall_time:Date.now()/1000,dispatch_id,status:'denied_or_unknown',
        run_id:context?.run_id,episode:context?.episode,actual_dispatch_attested:false,
        reason:signal.aborted?'aborted':error?.code==='QUOTA'?'scope_budget_or_replay_rejected':'admission_unavailable_or_invalid'});}catch{}
      throw signal.aborted?fail('ABORTED'):fail(error?.code==='QUOTA'?'QUOTA':'TRANSPORT');
    }
  };
}
