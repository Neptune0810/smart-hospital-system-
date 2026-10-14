// Count persisted Harness inference effects, not hook invocations or tool calls.
import {DatabaseSync} from 'node:sqlite';
import {root,smart,data} from './runtime.js';
import {appendFileSync} from 'node:fs';
export async function syncModelRequests(session){
  if(!smart)return;
  const response=await fetch('http://127.0.0.1:4188/api/state',{signal:AbortSignal.timeout(2500)});
  if(!response.ok)throw Error('model_meter_state_unavailable');
  const state=await response.json(),budget=state.mhs_budget;
  if(state.controller_session!==session||!budget||budget.end_wall||state.operator_stopped)return;
  const db=new DatabaseSync(root+'/agent/agh-home-mhs/data/sessions.db',{readOnly:true});
  let rows;
  try{rows=db.prepare("SELECT seq,ts,data FROM events WHERE session_key=? AND type='effect/intent' ORDER BY seq DESC LIMIT 2000").all(session);}finally{db.close();}
  const recorded=new Set(state.smart_ward?.model_request_ids??[]);
  for(const row of rows.reverse()){
    const effect=JSON.parse(row.data),wall=Date.parse(row.ts)/1000;
    if(effect.kind!=='inference'||!Number.isFinite(wall)||wall<budget.start_wall)continue;
    const request_id=effect.effectId??('ledger:'+session+':'+row.seq);
    if(recorded.has(request_id))continue;
    const result=await fetch('http://127.0.0.1:4188/api/ward/model-request',{
      method:'POST',headers:{'Content-Type':'application/json'},signal:AbortSignal.timeout(2500),
      body:JSON.stringify({request_id,expected_run_id:state.run_id,session_key:session})});
    if(!result.ok)throw Error('model_meter_commit_failed:'+result.status);
    recorded.add(request_id);
  }
}
export function meterError(session,error){appendFileSync(data+'/policy-errors.jsonl',JSON.stringify({wall_time:Date.now()/1000,session,kind:'model_meter',error:String(error)})+'\n');}
