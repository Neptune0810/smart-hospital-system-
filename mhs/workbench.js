// Project-only facade. The official SDK owns session transport and durable follow-ups.
import http from 'node:http';
import {readFileSync,writeFileSync,renameSync,mkdirSync,appendFileSync} from 'node:fs';
import {spawn} from 'node:child_process';
import {DatabaseSync} from 'node:sqlite';
import {createClient,fileJournal} from '../agent/agh-source-mhs/packages/sdk/src/index.node.ts';
import {queuedTurnTerminal} from './notification-terminal.js';
const root=process.env.HOSPITAL_ROOT??'/mnt/sda/omni-yezh/hospital-delivery-agent',home=root+'/agent/agh-home-mhs';
const smartMode=process.env.HOSPITAL_MODE==='smart-ward-v1';
const dataRoot=process.env.HOSPITAL_DATA_DIR??root+'/runs/'+(smartMode?'smart-ward-v1':'mhs-v2');
const dir=dataRoot+'/workbench';mkdirSync(dir,{recursive:true});
const stations=Object.fromEntries(Object.entries(JSON.parse(readFileSync(root+'/configs/stations.json','utf8'))).map(([k,v])=>[k,v.label]));
function activity(id){
  if(!id)return null;
  const db=new DatabaseSync(home+'/data/sessions.db',{readOnly:true});
  try{
    const rows=db.prepare("SELECT seq,ts,type,data FROM events WHERE session_key=? ORDER BY seq DESC LIMIT 35").all(id);
    const intent=rows.find(r=>r.type==='effect/intent'&&JSON.parse(r.data).kind==='inference');
    const waiting=intent&&!rows.some(r=>r.seq>intent.seq&&r.type==='effect/settled'&&JSON.parse(r.data).effectId===JSON.parse(intent.data).effectId);
    const error=rows.find(r=>r.type==='tool/result'&&JSON.parse(r.data).isError);
    return {phase:waiting?'waiting_model':rows[0]?.type==='turn/end'?'idle':'processing',waiting_since:waiting?Date.parse(intent.ts)/1000:null,last_event:rows[0]?.ts,last_tool_error:error?{at:error.ts,text:JSON.parse(error.data).content.filter(b=>b.type==='text').map(b=>b.text).join('').slice(0,400)}:null};
  }finally{db.close();}
}
let client,connecting,job=null;
const sessions=new Map();
function load(path,fallback){try{return JSON.parse(readFileSync(path,'utf8'));}catch(e){if(e.code==='ENOENT')return fallback;throw e;}}
const operations=load(dir+'/operations.json',{});
for(const op of Object.values(operations))if(['submitting','resetting','stopping'].includes(op.status)){
  op.status='unknown';op.error='工作台重启，操作结果待核验；不会重复发送任务。';
}
function persist(){writeFileSync(dir+'/operations.tmp',JSON.stringify(operations,null,2));renameSync(dir+'/operations.tmp',dir+'/operations.json');}
function record(op){persist();appendFileSync(dir+'/commands.jsonl',JSON.stringify({...op,recorded_wall_time:Date.now()/1000})+'\n');}
async function api(path='/api/state',body){
  const response=await fetch('http://127.0.0.1:4188'+path,{...(body?{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)}:{}),signal:AbortSignal.timeout(5000)});
  const d=await response.json();if(!response.ok||d.ok===false)throw Error(d.error||'配送服务不可用');return d.result??d;
}
async function sdk(){
  if(client&&client.connectionState==='connected')return client;
  if(connecting)return connecting;
  connecting=(async()=>{
    if(client)await client.close().catch(()=>{});
    const discovery=load(home+'/data/daemon/discovery.json',{});
    if(discovery.socketPath!==home+'/data/daemon/agnesd.sock'||!discovery.ready)throw Error('项目 Agnes 未就绪');
    client=createClient({transport:{kind:'unix',path:discovery.socketPath},auth:{kind:'local'},
      journal:fileJournal(dir+'/sdk-journal','hospital-workbench-v1'),locale:'zh-CN'});
    await client.initialize();sessions.clear();return client;
  })().finally(()=>{connecting=null;});return connecting;
}
async function session(id){
  const c=await sdk();if(!sessions.has(id))sessions.set(id,await c.session.load(id,{cwd:root}));return sessions.get(id);
}
function binding(id){return load(dataRoot+'/session-bindings.json',{})[id];}
function assertSession(s,id){
  if(!id||!id.startsWith('agnes:local:local-dev:'))throw Error('请使用本轮聊天，历史聊天仅供查看');
  const b=binding(id);
  if(b&&(b.revoked||b.run_id!==s.run_id))throw Error('历史聊天仅供查看，请回到本轮聊天');
  if(s.controller_session&&s.controller_session!==id)throw Error('当前显示另一聊天，请回到本轮聊天');
}
async function newSession(s){
  if(s.controller_session)return s.controller_session;
  const c=await sdk();const key='agnes:local:local-dev:cli:workspace:'+(smartMode?'ward-':'demo-')+s.run_id;
  const created=await c.session.new({cwd:root,preset:'hospital-mhs',sessionKey:key});
  await api('/api/admin/claim',{session_key:key,expected_run_id:s.run_id});
  sessions.set(key,created);return key;
}
async function send(op,text){
  const handle=await session(op.session_id);const opening=await handle.projectUIOpening({maxNodes:30,maxBytes:131072});
  const busy=opening.timeline.opState!==null;
  op.text=text;op.status='submitting';record(op);
  if(busy){op.queue_seq=await handle.followUp(text,{commandId:op.operation_id});op.status='queued';record(op);}
  else{
    const turn=handle.prompt(text,{titleLocale:'zh-CN'});
    op.status='sent';record(op);
    turn.then(result=>{op.turn_result=result;op.status='turn_finished';record(op);})
      .catch(error=>{op.status='failed';op.error=String(error);record(op);});
  }
}
async function cancelOwner(s){
  if(!s.controller_session)return;
  try{
    const h=await session(s.controller_session);await h.cancel();
    const opening=await h.projectUIOpening({maxNodes:10,maxBytes:131072});
    for(const input of opening.timeline.pendingInputs??[])await h.removeQueued(input.itemId,{commandId:'stop-'+input.itemId});
  }catch(error){appendFileSync(dir+'/errors.jsonl',JSON.stringify({wall_time:Date.now()/1000,kind:'cancel_chat',error:String(error)})+'\n');}
}
function resetRuntime(op){
  job=op;op.status='resetting';op.phase='保存并停止本轮';record(op);
  const child=spawn('python3',['-m','mhs.manage','reset'],{cwd:root,env:{...process.env,HOSPITAL_WORKBENCH_JOB:op.operation_id},stdio:['ignore','pipe','pipe']});
  let lines='';const log=dir+'/'+op.operation_id+'.log';
  for(const stream of [child.stdout,child.stderr])stream.on('data',chunk=>{
    appendFileSync(log,chunk);lines+=chunk.toString();const parts=lines.split('\n');lines=parts.pop();
    for(const line of parts)if(line.startsWith('WORKBENCH_PHASE ')){op.phase=line.slice(16);record(op);}
  });
  child.on('error',error=>{op.status='failed';op.error=String(error);record(op);job=null;});
  child.on('exit',async code=>{
    try{
    if(code!==0)throw Error('新一轮启动失败，详见项目生命周期日志；退出码 '+code);
      op.phase='创建本轮聊天';record(op);const s=await api();op.new_session=await newSession(s);op.new_run_id=s.run_id;
      op.status='completed';op.phase='准备就绪，等待下单';record(op);
    }catch(error){op.status='failed';op.error=String(error);record(op);}
  });
}
async function command(a){
  if(!/^[a-zA-Z0-9_-]{8,80}$/.test(a.operation_id??''))throw Error('invalid_operation_id');
  const signature=JSON.stringify(a);const existing=operations[a.operation_id];
  if(existing){if(existing.signature!==signature)throw Error('operation_id_conflict');return existing;}
  const s=await api();if(a.expected_run_id!==s.run_id)throw Error('运行已改变，请刷新后再操作');
  if(!['open','normal','urgent','delivery','scene','receiver','stop','reset','smart_start','smart_event'].includes(a.action))throw Error('unknown_demo_action');
  if(['smart_start','smart_event'].includes(a.action)&&!smartMode)throw Error('smart_ward_profile_required');
  if(smartMode&&['normal','urgent','delivery','receiver','scene'].includes(a.action))throw Error('智慧病区请使用设备演示控件，直接配送入口已隐藏');
  if(job&&['resetting','stopping'].includes(job.status))throw Error('正在新建运行，请等待完成');
  if(!['reset','open','stop','smart_start'].includes(a.action))assertSession(s,a.session_id);
  if(['normal','urgent','delivery','receiver','scene','smart_start','smart_event'].includes(a.action)&&(s.operator_stopped||(s.mhs_budget??{}).stopped_reason))throw Error('本轮已停止，请开始新一轮');
  if(a.action==='normal'&&(!Array.isArray(a.stations)||a.stations.length!==2||a.stations.some(id=>!stations[id])))throw Error('请选择两个有效护士站');
  if(['urgent','receiver'].includes(a.action)&&!stations[a.station_id])throw Error('请选择有效护士站');
  if(a.action==='receiver'&&typeof a.available!=='boolean')throw Error('invalid_receiver_availability');
  const op={...a,signature,status:'accepted',wall_time:Date.now()/1000};operations[a.operation_id]=op;record(op);
  try{
    if(a.action==='open'){op.new_session=await newSession(s);op.status='completed';record(op);}
    else if(a.action==='smart_start'){
      if(s.controller_session)assertSession(s,a.session_id);
      const id=await newSession(s);op.new_session=id;
      await api('/api/admin/smart-ward/start',{session_key:id,expected_run_id:s.run_id,operation_id:a.operation_id});
      notificationState.paused=false;op.text='已开始值守；设备新事件由服务器自动交给 Agnes，关闭网页仍会继续。';op.status='completed';record(op);
    }
    else if(a.action==='smart_event'){
      if(!['insert_specimen','set_brightness','set_receiver','set_lamp_fault'].includes(a.kind))throw Error('unknown_smart_ward_command');
      op.scene_result=await api('/api/admin/smart-ward/command',{kind:a.kind,arguments:a.arguments??{},operation_id:a.operation_id,expected_run_id:s.run_id});
      op.text={insert_specimen:'设备演示：已请求放入封装标本，等待柜内新采样。',set_brightness:'设备演示：已请求调暗灯具，等待传感器检测。',set_receiver:'设备演示：接收柜状态已请求改变。',set_lamp_fault:'设备演示：灯具响应状态已请求改变。'}[a.kind];
      op.status='completed';record(op);
    }
    else if(a.action==='stop'||a.action==='reset'){
      op.status='stopping';record(op);
      notificationState.paused=true;
      await api('/api/admin/stop-run',{operation_id:a.operation_id,expected_run_id:s.run_id});
      // Physical stop precedes SDK cancellation; unrelated normal commands cannot hold it up.
      await Promise.race([notificationSubmission.catch(()=>{}),new Promise(resolve=>setTimeout(resolve,5000))]);
      await cancelOwner(s);
      if(a.action==='reset')resetRuntime(op);
      else {op.status='stop_requested';record(op);}
    }else{
      if(!s.pose_fresh||!s.navigation_ready||!s.motion_guard.healthy)throw Error('设备未就绪，请等恢复后再操作');
      await api('/api/admin/claim',{session_key:a.session_id,expected_run_id:s.run_id});
      let text;
      if(a.action==='normal')text='从物资库取两件包裹，'+a.stations.map(id=>stations[id]+'一件').join('，')+'，尽量少干扰患者，交接后原地待命。';
      if(a.action==='urgent')text='从药房再取一件急件，送 '+stations[a.station_id]+'，保留已有任务。';
      if(a.action==='delivery'){
        if(!stations[a.pickup]||!stations[a.destination]||a.pickup===a.destination||!Number.isInteger(a.quantity)||a.quantity<1||a.quantity>6||!['normal','urgent'].includes(a.priority)||typeof a.package_label!=='string'||a.package_label.length>40)throw Error('无效点到点任务');
        text='从'+stations[a.pickup]+'取'+a.quantity+'件'+(a.package_label||'物资')+'，送到'+stations[a.destination]+'，'+(a.priority==='urgent'?'这是急件，优先时效，保留已有任务':'普通配送，尽量少干扰患者')+'，交接后原地待命。';
      }
      if(a.action==='scene'){
        await api('/api/admin/scene-event',{kind:a.kind,value:a.value});
        text='[演示操作] '+(a.kind==='crowd_mode'?'仿真人群模式已改为 '+a.value.mode+'，请读取真实反馈决定路线。':'通道可用性改变，请读取最新路线条件。');
      }
      if(a.action==='receiver'){
        await api('/api/admin/receiver',{station_id:a.station_id,available:a.available});
        // Device state and native watch notifications carry the change.
        // A UI operator action must not masquerade as another Agent task.
        op.text=stations[a.station_id]+(a.available?'已恢复接收':'已暂停接收');
        op.status='completed';record(op);return op;
      }
      await send(op,text);
    }
  }catch(error){op.status='failed';op.error=String(error);record(op);throw error;}
  return op;
}
// Backend owns durable outbox rows and an atomic single-worker lease. A browser is
// never the scheduler. Redelivery is intentional; incident/order IDs deduplicate it.
const notificationWorkerId='workbench-'+process.pid+'-'+Math.random().toString(36).slice(2);
const notificationState={worker_id:notificationWorkerId,paused:false,phase:smartMode?'idle':'disabled',last_delivered:null,last_error:null};
let notificationPolling=false,notificationSubmission=Promise.resolve(),notificationEpoch=0,shuttingDown=false;
const queuedNotifications=new Map(),directPromptNotifications=new Map(),handledNotificationTerminals=new Set();
let notificationMonitorBinding=null,notificationTerminalPolling=false;
function monitorBinding(state){
  const key=JSON.stringify([state.run_id,state.controller_session,state.mhs_budget?.start_wall??null]);
  if(key!==notificationMonitorBinding){
    queuedNotifications.clear();directPromptNotifications.clear();handledNotificationTerminals.clear();
    notificationMonitorBinding=key;
  }
  return key;
}
function notificationLedger(sessionId,afterSeq){
  const db=new DatabaseSync(home+'/data/sessions.db',{readOnly:true});
  try{return db.prepare("SELECT seq,ts,type,origin,trust,hex(lane) AS lane_hex,data FROM events WHERE session_key=? AND lane=x'6d61696e' AND seq>=? AND type IN ('inbox','user/message','turn/start','turn/end') ORDER BY seq DESC LIMIT 600").all(sessionId,afterSeq).map(row=>({...row,data:JSON.parse(row.data)}));}
  finally{db.close();}
}
function notificationError(error){
  const message=String(error),previous=notificationState.last_error,now=Date.now()/1000;
  notificationState.last_error={wall_time:now,message,last_logged_wall:previous?.message===message?previous.last_logged_wall:undefined};notificationState.phase='retry_wait';
  if(previous?.message!==message||now-(previous.last_logged_wall??0)>=60){
    notificationState.last_error.last_logged_wall=now;
    appendFileSync(dir+'/notifications.jsonl',JSON.stringify({kind:'error',wall_time:now,message})+'\n');
  }
}
async function acknowledgeNotification(n,status,details={}){
  return api('/api/admin/smart-ward/notifications/ack',{notification_id:n.notification_id,status,details:{...details,worker_id:notificationWorkerId,expected_run_id:n.run_id}});
}
async function notificationTurnResult(n,outcome){
  if(!outcome.error&&outcome.result?.reason==='completed')return;
  const reason=outcome.error?'error':outcome.result?.reason??'unknown';
  const detail={reason,error:outcome.error?String(outcome.error):undefined,turn_result:outcome.result,stage:'prompt'};
  appendFileSync(dir+'/notifications.jsonl',JSON.stringify({kind:'turn_failure',notification_id:n.notification_id,wall_time:Date.now()/1000,...detail})+'\n');
  const fresh=await api();
  // A late turn from an archived/stopped run must never restart it or pause a new run.
  if(fresh.run_id!==n.run_id||fresh.controller_session!==n.session_id||fresh.operator_stopped||!fresh.smart_ward?.watch?.enabled||n.budget_start_wall!==undefined&&n.budget_start_wall!==fresh.mhs_budget?.start_wall)return;
  notificationError(outcome.error??Error('Agnes turn ended: '+reason));
  if(reason==='error'&&(n.attempts??1)<3){await acknowledgeNotification(n,'retry',detail);return;}
  notificationState.paused=true;notificationState.phase='fault';notificationEpoch++;
  await api('/api/admin/stop-run',{operation_id:('notification-failed-'+n.notification_id).slice(0,80),expected_run_id:n.run_id,reason:'notification_model_'+reason});
  await cancelOwner(fresh);
}
async function notificationTerminalTick(){
  if(!smartMode||notificationTerminalPolling||shuttingDown||notificationState.paused)return;
  notificationTerminalPolling=true;const epoch=notificationEpoch;
  try{
    const state=await api();monitorBinding(state);
    if(!state.smart_ward?.watch?.enabled||state.operator_stopped||state.mhs_budget?.stopped_reason||!state.controller_session||!queuedNotifications.size)return;
    // Direct prompt() already owns its TurnResult, retry and failure handling.
    // It can settle just after a raw end row; defer one tick instead of racing it.
    if(directPromptNotifications.size)return;
    const entries=[...queuedNotifications.values()].filter(n=>n.run_id===state.run_id&&n.session_id===state.controller_session&&n.budget_start_wall===state.mhs_budget?.start_wall);
    if(!entries.length)return;
    const rows=notificationLedger(state.controller_session,Math.min(...entries.map(n=>n.queue_seq)));
    for(const n of entries){
      const proof=queuedTurnTerminal(rows,n,state.mhs_budget?.start_wall);
      if(!proof)continue;
      const terminalKey=state.run_id+':'+state.controller_session+':'+proof.terminal_seq;
      queuedNotifications.delete(n.notification_id);
      if(handledNotificationTerminals.has(terminalKey))continue;
      handledNotificationTerminals.add(terminalKey);
      if(proof.reason==='completed')continue;
      const fresh=await api();
      if(epoch!==notificationEpoch||notificationState.paused||shuttingDown||fresh.run_id!==n.run_id||fresh.controller_session!==n.session_id||fresh.operator_stopped||!fresh.smart_ward?.watch?.enabled||fresh.mhs_budget?.start_wall!==n.budget_start_wall)return;
      // This input was already delivered. Preserve the actual terminal failure;
      // never resubmit it or reset the batch budget from this monitor.
      const detail={kind:'queued_turn_failure',wall_time:Date.now()/1000,run_id:n.run_id,session_id:n.session_id,...proof};
      appendFileSync(dir+'/notifications.jsonl',JSON.stringify(detail)+'\n');
      notificationState.paused=true;notificationState.phase='fault';notificationEpoch++;
      notificationState.last_error={wall_time:detail.wall_time,message:'排队设备事件的 Agnes 回合结束：'+proof.reason,notification_id:n.notification_id,terminal_seq:proof.terminal_seq};
      try{await api('/api/admin/stop-run',{operation_id:('followup-failed-'+n.notification_id+'-'+proof.terminal_seq).slice(0,80),expected_run_id:n.run_id,reason:'notification_followup_'+proof.reason});}
      catch(error){
        notificationState.last_error.stop_error=String(error);
        appendFileSync(dir+'/notifications.jsonl',JSON.stringify({...detail,kind:'queued_turn_stop_failure',error:String(error)})+'\n');
      }
      await cancelOwner(fresh);return;
    }
  }catch(error){notificationError(error);}finally{notificationTerminalPolling=false;}
}
async function notificationTick(){
  if(!smartMode||notificationPolling||shuttingDown||notificationState.paused)return;
  notificationPolling=true;const epoch=notificationEpoch;
  try{
    const s=await api();
    monitorBinding(s);
    if(!s.smart_ward?.watch?.enabled||s.operator_stopped||s.mhs_budget?.stopped_reason||!s.controller_session){notificationState.phase='paused';return;}
    const n=await api('/api/admin/smart-ward/notifications/claim',{worker_id:notificationWorkerId,expected_run_id:s.run_id});
    if(!n?.notification_id){notificationState.phase='idle';return;}
    if(n.run_id!==s.run_id||n.session_id!==s.controller_session)throw Error('notification_binding_mismatch');
    let submitted=false;
    notificationSubmission=(async()=>{
      const h=await session(n.session_id),opening=await h.projectUIOpening({maxNodes:10,maxBytes:131072});
      const fresh=await api();
      if(epoch!==notificationEpoch||notificationState.paused||shuttingDown||fresh.run_id!==n.run_id||fresh.controller_session!==n.session_id||!fresh.smart_ward?.watch?.enabled||fresh.operator_stopped)throw Error('notification_delivery_paused');
      notificationState.phase='delivering';
      let turnOutcome=null;
      n.budget_start_wall=fresh.mhs_budget?.start_wall;
      if(opening.timeline.opState!==null){
        n.queue_seq=await h.followUp(n.text,{commandId:n.notification_id});submitted=true;
        if(!Number.isSafeInteger(n.queue_seq)||n.queue_seq<1)throw Error('invalid_followup_ack_sequence');
        queuedNotifications.set(n.notification_id,{...n});
      }else{
        const directOwner={run_id:n.run_id,session_id:n.session_id};
        directPromptNotifications.set(n.notification_id,directOwner);
        const turn=h.prompt(n.text,{titleLocale:'zh-CN'});submitted=true;
        // prompt completes only when the model turn ends. Do not block other events.
        turnOutcome=Promise.resolve(turn).then(result=>({result}),error=>({error}));
        turnOutcome.then(async outcome=>{
          if(outcome.result?.lastSeq)handledNotificationTerminals.add(n.run_id+':'+n.session_id+':'+outcome.result.lastSeq);
          await notificationTurnResult(n,outcome);
        }).catch(notificationError).finally(()=>{if(directPromptNotifications.get(n.notification_id)===directOwner)directPromptNotifications.delete(n.notification_id);});
      }
      await acknowledgeNotification(n,'delivered',{session_id:n.session_id,stage:'sdk_accepted',...(n.queue_seq?{queue_seq:n.queue_seq}:{})});
      notificationState.last_delivered={notification_id:n.notification_id,wall_time:Date.now()/1000,event_ids:n.event_ids??[],...(n.queue_seq?{queue_seq:n.queue_seq}:{})};
      if(epoch===notificationEpoch&&!notificationState.paused){notificationState.last_error=null;notificationState.phase='idle';}
      appendFileSync(dir+'/notifications.jsonl',JSON.stringify({kind:'delivered',...notificationState.last_delivered})+'\n');
    })();
    try{await notificationSubmission;}
    catch(error){
      // If ACK was lost, the lease expires and the same durable notification can return.
      if(!submitted)try{await acknowledgeNotification(n,'retry',{error:String(error),stage:'submission'});}catch(ackError){notificationError(ackError);}
      throw error;
    }
  }catch(error){notificationError(error);}finally{notificationPolling=false;}
}
function reply(res,status,body,type='application/json'){
  const bytes=Buffer.isBuffer(body)?body:Buffer.from(typeof body==='string'?body:JSON.stringify(body));
  res.writeHead(status,{'Content-Type':type,'Content-Length':bytes.length,'Cache-Control':'no-store','X-Content-Type-Options':'nosniff'});res.end(bytes);
}
let mutation=Promise.resolve();const pendingCommands=new Map();
function dispatchCommand(a){
  const existing=pendingCommands.get(a.operation_id);
  if(existing){if(existing.signature!==JSON.stringify(a))throw Error('operation_id_conflict');return existing.promise;}
  const urgent=a.action==='stop'||a.action==='reset';
  if(urgent){notificationEpoch++;notificationState.paused=true;}
  const pending=urgent?command(a):mutation.then(()=>command(a));
  if(!urgent)mutation=pending.catch(()=>{});
  pendingCommands.set(a.operation_id,{signature:JSON.stringify(a),promise:pending});
  pending.finally(()=>pendingCommands.delete(a.operation_id)).catch(()=>{});return pending;
}
const server=http.createServer(async(req,res)=>{
  try{
    const u=new URL(req.url,'http://127.0.0.1:4194');
    if(req.method==='GET'){
      if(u.pathname==='/')return reply(res,200,readFileSync(root+'/mhs/dashboard.html'),'text/html; charset=utf-8');
      if(u.pathname==='/workbench.js')return reply(res,200,readFileSync(root+'/mhs/dashboard.js'),'text/javascript; charset=utf-8');
      if(u.pathname==='/dashboard-v2.js')return reply(res,200,readFileSync(root+'/mhs/dashboard-v2.js'),'text/javascript; charset=utf-8');
      if(u.pathname==='/api/demo/status'){
        let s=null;try{s=await api();}catch{}
        const id=u.searchParams.get('session');const b=id?binding(id):null;
        return reply(res,200,{job,current_session:s?.controller_session??null,agent_activity:activity(s?.controller_session),profile:smartMode?'smart-ward-v1':'mhs-v2',notification_worker:smartMode?notificationState:null,viewing_current:!!s&&!!id&&(id===s.controller_session||!s.controller_session&&!!b&&b.run_id===s.run_id&&!b.revoked),operations:Object.values(operations).filter(op=>!s||op.expected_run_id===s.run_id).slice(-15)});
      }
      if(!['/api/state','/api/frame','/frame.jpg','/map.png','/api/report'].includes(u.pathname))return reply(res,404,{error:'not_found'});
      const response=await fetch('http://127.0.0.1:4188'+u.pathname+u.search,{signal:AbortSignal.timeout(5000)});
      return reply(res,response.status,Buffer.from(await response.arrayBuffer()),response.headers.get('content-type')??'application/json');
    }
    if(req.method!=='POST'||u.pathname!=='/api/demo/command')return reply(res,404,{error:'not_found'});
    if(!['http://127.0.0.1:4193','http://127.0.0.1:4194'].includes(req.headers.origin)||!['127.0.0.1:4193','127.0.0.1:4194'].includes(req.headers.host)||req.headers['content-type']?.split(';')[0]!=='application/json')return reply(res,403,{error:'origin_or_host_rejected'});
    let bytes=0,parts=[];for await(const chunk of req){bytes+=chunk.length;if(bytes>8192)throw Error('body_too_large');parts.push(chunk);}
    const a=JSON.parse(Buffer.concat(parts).toString());
    reply(res,200,{ok:true,result:await dispatchCommand(a)});
  }catch(error){reply(res,400,{ok:false,error:String(error)});}
});
server.listen(4194,'127.0.0.1',()=>console.log('Hospital workbench 127.0.0.1:4194'));
if(smartMode){setInterval(notificationTick,1000).unref();setInterval(notificationTerminalTick,1000).unref();}
for(const sig of ['SIGTERM','SIGINT'])process.on(sig,()=>{shuttingDown=true;notificationEpoch++;notificationState.paused=true;server.close();client?.close().finally(()=>process.exit());setTimeout(()=>process.exit(),2000).unref();});
