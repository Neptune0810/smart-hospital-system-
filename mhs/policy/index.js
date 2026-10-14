import { readFileSync, appendFileSync } from 'node:fs';
import { randomUUID } from 'node:crypto';
import {DatabaseSync} from 'node:sqlite';
import { connectBusiness } from './business.js';
import { allowTool } from './scope.js';
import { checkSessionRun } from './session-scope.js';
import { completionFeedback,verificationKey,acceptedLightWatches } from './completion.js';
import { hospitalContext } from './context.js';
import {root,data,smart} from './runtime.js';
import {syncModelRequests,meterError} from './model-requests.js';
const instructions=readFileSync(new URL(smart?'../instructions-smart.md':'../instructions.md',import.meta.url),'utf8');
export const presets={'hospital-mhs':JSON.parse(readFileSync(new URL('../hospital-mhs.yaml',import.meta.url),'utf8'))};
export const hospitalMHS={
  inject:['extension'],
  async apply(ctx){
    const extension=ctx.extension?.();if(!extension)return;
    const verified=new Map(),terminalReminders=new Map();
    const closeBusiness=await connectBusiness(extension,(session,report)=>verified.set(session,verificationKey(report)));
    ctx.effect(()=>closeBusiness);
    const context=extension.registerHook('context',payload=>hospitalContext(payload.sections,instructions));
    const scope=extension.registerHook('tool_call',async(payload,hctx)=>{
      let result=allowTool(payload);
      if(result.allow)try{result=await checkSessionRun(hctx.session.key);}catch{result={allow:false,reason:'项目运行身份校验失败，禁止执行。'};}
      if(result.allow)try{await syncModelRequests(hctx.session.key);}catch(error){meterError(hctx.session.key,error);result={allow:false,reason:'模型请求计量不可用，暂不执行设备。'};}
      if(!result.allow)appendFileSync(data+'/policy-denials.jsonl',JSON.stringify({wall_time:Date.now()/1000,session:hctx.session.key,name:payload.name,reason:result.reason})+'\n');
      return result;
    });
    const dispose=extension.registerHook('before_step',async(_,hctx)=>{
      let budget;
      try{
        await syncModelRequests(hctx.session.key);
        const identity=await checkSessionRun(hctx.session.key,randomUUID());
        if(!identity.allow)return {block:true,reason:identity.reason};
        budget=identity.budget;
      }catch(error){appendFileSync(data+'/policy-errors.jsonl',JSON.stringify({wall_time:Date.now()/1000,session:hctx.session.key,error:String(error)})+'\n');return {block:true,reason:'本轮校验失败：'+String(error.message||error)};}
      return budget?.stopped_reason?{block:true,reason:'本轮已停止：'+budget.stopped_reason}:{};
    });
    const completion=extension.registerHook('turn_stopping',async(payload,hctx)=>{
      if(payload.proposedReason!=='completed')return {action:'stop'};
      try{
        await syncModelRequests(hctx.session.key);
        const response=await fetch('http://127.0.0.1:4188/api/state',{signal:AbortSignal.timeout(2500)});
        if(!response.ok)return {action:'stop'};
        const s=await response.json(),isVerified=verified.get(hctx.session.key)===verificationKey(s);
        let waits=new Set();
        if(smart&&s.controller_session===hctx.session.key&&Object.values(s.smart_ward?.incidents??{}).some(i=>i.kind==='low_light'&&i.status==='awaiting_verification')){
          const db=new DatabaseSync(root+'/agent/agh-home-mhs/data/sessions.db',{readOnly:true});
          try{
            const rows=db.prepare("SELECT seq,ts,type,data FROM events WHERE session_key=? AND type IN ('tool/call','tool/result') ORDER BY seq DESC LIMIT 600").all(hctx.session.key);
            waits=acceptedLightWatches(rows.map(row=>({...row,data:JSON.parse(row.data)})),s);
          }finally{db.close();}
        }
        const feedback=completionFeedback(s,hctx.session.key,isVerified,waits);
        if(s.smart_ward&&feedback.action==='stop'&&isVerified&&!s.operator_stopped&&!s.mhs_budget?.stopped_reason){
          const finalized=await fetch('http://127.0.0.1:4188/api/ward/finalize',{method:'POST',headers:{'content-type':'application/json'},body:JSON.stringify({expected_run_id:s.run_id,session_key:hctx.session.key}),signal:AbortSignal.timeout(2500)});
          const result=await finalized.json();
          if(!finalized.ok||!result.ok){
            appendFileSync(data+'/policy-errors.jsonl',JSON.stringify({wall_time:Date.now()/1000,session:hctx.session.key,error:'episode_finalize_rejected',result})+'\n');
            return {action:'stop'};
          }
        }
        if(feedback.terminalVerification){
          const key=hctx.session.key+verificationKey(s),count=terminalReminders.get(key)??0;
          if(count>=2)return {action:'stop'};
          terminalReminders.set(key,count+1);
        }
        if(feedback.action==='continue')appendFileSync(data+'/completion-feedback.jsonl',JSON.stringify({wall_time:Date.now()/1000,run_id:s.run_id,session:hctx.session.key,turn:payload.turn,step:payload.step,event_version:s.latest_event_id,feedback})+'\n');
        return feedback.action==='continue'?{action:'continue',note:feedback.note}:{action:'stop'};
      }catch{return {action:'stop'};}
    });
    ctx.effect(()=>()=>{context();scope();dispose();completion();});
  }
};
