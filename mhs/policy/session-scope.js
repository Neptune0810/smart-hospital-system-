import {readFileSync,writeFileSync,renameSync} from 'node:fs';
import {data} from './runtime.js';
const path=data+'/session-bindings.json';
function load(){try{return JSON.parse(readFileSync(path,'utf8'));}catch(e){if(e.code==='ENOENT')return {};throw e;}}
export async function checkSessionRun(session,decisionId){
  if(!session)return {allow:false,reason:'缺少项目会话身份。'};
  let bindings=load();
  if(bindings[session]?.revoked)return {allow:false,reason:'此会话所属运行已重置。请新建聊天；旧会话仅供查看。'};
  // Only the workbench may bind a new controlling session. Opening a historical
  // or native new chat cannot silently acquire this robot in before_step.
  const identityResponse=await fetch('http://127.0.0.1:4188/api/mhs/identity',{signal:AbortSignal.timeout(2500)});
  if(!identityResponse.ok)throw Error('project_identity_unavailable');
  const identity=await identityResponse.json();
  if(identity.controller_session!==session)return {allow:false,reason:'此聊天仅供查看，请从院递工作台进入本轮聊天后发送任务。'};
  const response=await fetch('http://127.0.0.1:4188'+(decisionId?'/api/mhs/decision':'/api/mhs/identity'),{
    ...(decisionId?{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({decision_id:decisionId,expected_run_id:bindings[session]?.run_id,session_key:session})}:{}),
    signal:AbortSignal.timeout(2500)});
  if(!response.ok){const error=await response.json().catch(()=>({}));throw Error(error.error||'project_http_'+response.status);}
  const envelope=await response.json(), state=envelope.result??envelope;
  if(!state.run_id)throw Error('Project run missing');
  if(state.controller_session&&state.controller_session!==session)return {allow:false,reason:'本轮由另一聊天控制，请回到本轮聊天；新任务请先新一轮。'};
  bindings=load();
  if(bindings[session]?.revoked||bindings[session]?.run_id&&bindings[session].run_id!==state.run_id)
    return {allow:false,reason:'此会话与当前运行不匹配。请新建聊天。'};
  if(!bindings[session]){
    bindings[session]={run_id:state.run_id,bound_wall:Date.now()/1000};
    writeFileSync(path+'.tmp',JSON.stringify(bindings,null,2));renameSync(path+'.tmp',path);
  }
  return {allow:true,...(decisionId?{budget:state.budget}:{})};
}
