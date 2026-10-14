const demoId=id=>document.getElementById(id);
let viewedSession=null,chatConnected=false,demoStatus=null,commandBusy=false,stopBusy=false,lastJob=null,defaultChatOpened=false,wardOpened=false;
const stationNames={depot:'物资库',station_a:'A 站',station_b:'B 站',station_c:'C 站'};
const actionNames={pickup:'取货',deliver:'交接',wait:'等待接收',return:'返库'};
const phaseNames={planned:'计划',running:'执行中',completed:'已完成',failed:'未完成',ended:'已结束'};
function notice(text,error=false){demoId('demo-notice').textContent=text;demoId('demo-notice').className=error?'demo-notice error':'demo-notice';}
function showSession(id){
  notice('');
  const url=new URL('http://127.0.0.1:4189/');url.searchParams.set('session',id);
  // Scene reset restarts the daemon at the same port. Force a fresh bootstrap
  // document for deliberate chat switches, without reloading camera/layout changes.
  url.searchParams.set('workbench_open',String(Date.now()));
  demoId('chat').src=url.href;demoId('chat-link').href=url.href;
  chatURL.searchParams.set('session',id);
  const parent=new URL(location.href);parent.searchParams.set('session',id);history.replaceState(null,'',parent);
  viewedSession=null;chatConnected=false;
}
window.addEventListener('message',event=>{
  if(event.origin!=='http://127.0.0.1:4189'||event.source!==demoId('chat').contentWindow||event.data?.type!=='hospital-session-v1')return;
  const id=event.data.session_id;
  if(id!==null&&(typeof id!=='string'||!/^[a-zA-Z0-9:._-]{1,200}$/.test(id)))return;
  viewedSession=id;chatConnected=event.data.connected===true;updateDemo();
});
function updateDemo(){
  const s=latest,current=demoStatus?.viewing_current&&chatConnected,job=demoStatus?.job&&['resetting','stopping'].includes(demoStatus.job.status);
  const ended=!!s&&(s.operator_stopped||s.mhs_outcome==='incomplete'||s.mhs_budget?.stopped_reason);
  const healthy=!!s&&Date.now()-lastSuccess<5000&&s.pose_fresh&&s.navigation_ready&&s.motion_guard?.healthy&&s.crowd?.fresh;
  for(const id of ['demo-normal','demo-urgent','demo-pause','demo-resume'])demoId(id).disabled=commandBusy||stopBusy||job||!current||!healthy||ended;
  demoId('demo-stop').disabled=stopBusy||!s||!!s.operator_stopped;
  demoId('demo-reset').disabled=commandBusy||job||!s;
  demoId('demo-current').disabled=commandBusy||job||!s;
  demoId('demo-session').textContent=job?'新一轮：'+demoStatus.job.phase:!s?'配送服务连接中':!chatConnected?'聊天连接中':current?(ended?'本轮已停止，请新一轮':s.mhs_finished?'上笔已完成，可继续下单':'正在控制本轮机器人'):'历史或其他聊天仅供查看';
  if(s?.operator_stopped)demoId('demo-session').textContent=s.stopped_confirmed?'本轮已停止 · 已确认静止':'本轮停止中 · 等待真实静止';
  demoId('demo-stop').textContent=s?.operator_stopped?(s.stopped_confirmed?'本轮已停止':'停止确认中…'):'停止本轮';
  const ward=s?.smart_ward,smart=!!ward;
  for(const id of ['ward-controls','ward-banner','tab-ward'])demoId(id).hidden=!smart;
  document.body.classList.toggle('smart-ward',smart);
  document.querySelectorAll('[data-legacy-control]').forEach(el=>el.hidden=smart);
  if(smart){
    if(!wardOpened){wardOpened=true;demoId('tab-ward').click();}
    document.title='智慧医院系统 · 运行工作台';
    document.querySelector('h1').textContent='智慧医院系统';
    document.querySelector('.subtitle').textContent='设备发现需求，Agnes 安排并核验处理结果';
    document.querySelector('.chat-bar strong').textContent='Agnes · 病区服务助手';
    document.querySelector('.chat-help').textContent='先开始值守，再通过设备演示制造需求。设备事件自动进入本轮聊天；右侧查看实际执行和恢复反馈。历史聊天仅供查看。';
    const help=document.querySelectorAll('.help p'),helpText=['工作台自动打开本轮聊天。查看历史后，用“回到本轮聊天”返回唯一控制会话。','演示：开始值守 → 放入封装标本；等待柜内采样产生需求，Agnes 自动安排取送。','并行事件：点击“照明变暗”。灯具调节后，必须收到连续的新读数才确认解决。','聊天可问：现在有哪些需求？为什么这样安排？看一下前方有什么？','停止本轮会停止值守和运动；新一轮保存需求、回执与失败记录，默认不自动开启值守。连接失败时保持 SSH 转发窗口打开。'];
    help.forEach((p,i)=>{if(helpText[i])p.textContent=helpText[i];});
    demoId('reset-confirmation').querySelector('p').textContent='保存本轮需求、标本、交接回执和聊天，停止机器人并重建场景。新运行默认暂停值守，需要再次点击“开始值守”。当前聊天中的未发送草稿请先保存。';
    const ready=current&&healthy&&!ended&&!job&&!commandBusy&&!stopBusy;
    for(const id of ['ward-start','ward-insert','ward-dark','ward-lamp-fault','ward-lamp-recover','ward-receiver-pause','ward-receiver-resume'])demoId(id).disabled=!ready;
    demoId('ward-start').disabled=!ready||ward.watch.enabled;
    demoId('ward-start').textContent=ward.watch.enabled?'正在值守':'开始值守';
    demoId('demo-session').textContent=job?'新一轮：'+demoStatus.job.phase:!current?'历史聊天仅供查看，请回到本轮聊天':ended?'本轮已停止，请新一轮':ward.watch.enabled?'正在值守 · 设备事件自动上报':'准备就绪 · 点击开始值守';
    const worker=demoStatus?.notification_worker;
    demoId('ward-worker-status').textContent=worker?.last_error?'通知投递待重试：'+worker.last_error.message:worker?.last_delivered?'服务器已投递设备事件 · '+new Date(worker.last_delivered.wall_time*1000).toLocaleTimeString():'设备事件由服务器投递，关闭网页不影响值守。';
  }
}
window.renderArrangement=s=>{
  if(s.smart_ward){renderSmartWard(s);updateDemo();return;}
  const card=demoId('arrangement'),plan=s.delivery_plan;
  const ended=!!s.operator_stopped||s.mhs_outcome==='incomplete'||!!s.mhs_budget?.stopped_reason;
  card.hidden=!plan&&!s.operator_stopped&&!Object.keys(s.orders).length;
  // Compatibility with a currently running old API: evidence-based refill
  // completion and one highlighted current step, never all visits to depot.
  if(plan){
    let cursor=-1;
    plan.steps.forEach((step,i)=>{
      if(step.action==='return'&&i<plan.steps.length-1){
        const pickup=plan.steps.slice(i+1).find(next=>next.action==='pickup');
        const proof=pickup?.order_ids?.length&&pickup.order_ids.every(id=>s.orders[id]?.loaded||s.orders[id]?.receipt);
        if(proof||(cursor<0&&s.at_stations?.includes('depot')&&s.stopped_confirmed))step.status='completed';
      }
      if(cursor<0&&!['completed','failed'].includes(step.status))cursor=i;
      if(i!==cursor&&step.status==='running')step.status='planned';
    });
  }
  demoId('plan-heading').textContent=s.operator_stopped?'本轮已停止':s.mhs_outcome==='incomplete'?'本次结束 · 有未交付':s.mhs_finished?'本轮已处理完毕':plan?.stale?'安排待更新':'Agnes 的任务安排';
  demoId('plan-reason').textContent=plan?(ended?'原安排（已停止执行）：':'')+plan.reason:'提交任务后，显示导航、取货、交接和末站待命安排。';
  demoId('plan-stamp').textContent=plan?'更新于 '+new Date(plan.wall_time*1000).toLocaleTimeString()+(ended?' · 已归档，不再执行':plan.stale?' · 状态已变化，等待重新安排':' · 公开任务说明'):'';
  card.classList.toggle('stale',!!plan?.stale&&!ended);
  demoId('plan-steps').replaceChildren(...(plan?.steps??[]).map((step,i)=>{
    const status=ended&&!['completed','failed'].includes(step.status)?'ended':step.status;
    const row=document.createElement('div');row.className='plan-step '+status;
    const title=document.createElement('strong');title.textContent=(i+1)+' · '+actionNames[step.action]+' '+stationNames[step.station_id];
    const badge=document.createElement('span');badge.textContent=phaseNames[status];row.append(title,badge);return row;
  }));
  const next=plan?.steps.find(step=>!['completed','failed'].includes(step.status));
  demoId('plan-next').textContent=s.operator_stopped?'本轮不再执行新动作，订单与结果已保留。':s.mhs_outcome==='incomplete'?'未交付及载货已保留，请开始新一轮。':plan?.stale?'下一步：等待最新安排':next?'下一步：'+actionNames[next.action]+' '+stationNames[next.station_id]:plan?'交接结果见订单和回执':'';
  updateDemo();
};
async function submitDemo(action,extra={},operationId=crypto.randomUUID()){
  const isStop=action==='stop';
  if(isStop?stopBusy:commandBusy||stopBusy)return;
  if(isStop)stopBusy=true;else commandBusy=true;
  updateDemo();notice(isStop?'正在停止…':'正在提交…');
  const body={action,operation_id:operationId,expected_run_id:latest.run_id,session_id:viewedSession,...extra};
  // Save the exact ID and input before network IO; a lost response never causes a new task.
  const pendingKey=isStop?'hospital-demo-pending-stop':'hospital-demo-pending';
  sessionStorage.setItem(pendingKey,JSON.stringify(body));
  try{
    const response=await fetch('/api/demo/command',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body),signal:AbortSignal.timeout(15000)});
    const envelope=await response.json();if(!response.ok||!envelope.ok)throw Error(envelope.error||'提交失败');
    const op=envelope.result;sessionStorage.removeItem(pendingKey);
    if(action==='reset')sessionStorage.setItem('hospital-demo-reset-owner',operationId);
    if(op.new_session)showSession(op.new_session);
    notice(op.status==='queued'?'已排队：'+op.text:op.status==='sent'?'已发送：'+op.text:op.status==='resetting'?'正在保存本轮并重建场景…':op.status==='stop_requested'?'已请求停止，等待导航终态和实测静止。':op.text??'操作已完成');
    return op;
  }catch(error){notice(String(error)+'；若网络中断，可点“核验上次提交”，不会重复创建任务。',true);return null;}
  finally{if(isStop)stopBusy=false;else commandBusy=false;updateDemo();}
}
demoId('demo-normal').onclick=()=>submitDemo('normal',{stations:[demoId('normal-first').value,demoId('normal-second').value]});
demoId('demo-urgent').onclick=()=>submitDemo('urgent',{station_id:demoId('urgent-station').value});
demoId('demo-pause').onclick=()=>submitDemo('receiver',{station_id:demoId('receiver-station').value,available:false});
demoId('demo-resume').onclick=()=>submitDemo('receiver',{station_id:demoId('receiver-station').value,available:true});
demoId('demo-stop').onclick=()=>submitDemo('stop');
demoId('ward-start').onclick=()=>submitDemo('smart_start');
demoId('ward-insert').onclick=()=>submitDemo('smart_event',{kind:'insert_specimen',arguments:{cabinet_id:demoId('ward-cabinet').value,priority:demoId('ward-priority').value}});
demoId('ward-dark').onclick=()=>submitDemo('smart_event',{kind:'set_brightness',arguments:{room_id:demoId('ward-room').value,brightness_percent:10}});
demoId('ward-lamp-fault').onclick=()=>submitDemo('smart_event',{kind:'set_lamp_fault',arguments:{room_id:demoId('ward-room').value,stuck:true}});
demoId('ward-lamp-recover').onclick=()=>submitDemo('smart_event',{kind:'set_lamp_fault',arguments:{room_id:demoId('ward-room').value,stuck:false}});
demoId('ward-receiver-pause').onclick=()=>submitDemo('smart_event',{kind:'set_receiver',arguments:{available:false}});
demoId('ward-receiver-resume').onclick=()=>submitDemo('smart_event',{kind:'set_receiver',arguments:{available:true}});
demoId('demo-current').onclick=()=>demoStatus?.current_session?showSession(demoStatus.current_session):submitDemo('open');
demoId('demo-reset').onclick=()=>demoId('reset-confirmation').showModal();
demoId('reset-cancel').onclick=()=>demoId('reset-confirmation').close();
demoId('reset-confirm').onclick=()=>{demoId('reset-confirmation').close();submitDemo('reset');};
demoId('demo-retry').onclick=()=>{
  const stored=sessionStorage.getItem('hospital-demo-pending-stop')||sessionStorage.getItem('hospital-demo-pending');if(!stored)return notice('没有待核验的提交。');
  const a=JSON.parse(stored);if(a.expected_run_id!==latest?.run_id)return notice('上次属于旧运行，请查看执行记录。',true);
  submitDemo(a.action,Object.fromEntries(Object.entries(a).filter(([key])=>!['action','operation_id','expected_run_id'].includes(key))),a.operation_id);
};
async function demoPoll(){
  try{
    const response=await fetch('/api/demo/status?session='+encodeURIComponent(viewedSession??''),{cache:'no-store',signal:AbortSignal.timeout(5000)});
    if(!response.ok)throw Error('演示控制不可用');demoStatus=await response.json();
    // Opening the default workbench starts at its canonical chat, not a stale
    // native last-session preference. An explicit historical URL stays read-only.
    if(!requestedSession&&!defaultChatOpened&&latest&&!commandBusy){
      defaultChatOpened=true;
      if(demoStatus.current_session)showSession(demoStatus.current_session);
      else await submitDemo('open');
    }
    const job=demoStatus.job;
    if(job){
      const key=job.operation_id+':'+job.status+':'+job.phase;
      if(key!==lastJob){lastJob=key;notice(job.error??job.phase,job.status==='failed');}
      const ownsReset=sessionStorage.getItem('hospital-demo-reset-owner')===job.operation_id||!requestedSession;
      if(ownsReset&&job.status==='completed'&&job.new_session&&viewedSession!==job.new_session&&!sessionStorage.getItem('hospital-demo-job-'+job.operation_id)){
        sessionStorage.setItem('hospital-demo-job-'+job.operation_id,'opened');showSession(job.new_session);
      }
    }
    const ops=demoStatus.operations??[];demoId('demo-command-list').replaceChildren(...ops.slice(-4).reverse().map(op=>{
      const p=document.createElement('p');const names={sent:'已发送',queued:'已排队',turn_finished:'本次回复已结束',failed:'失败',completed:'完成',unknown:'待核验',stop_requested:'停止已请求'};
      p.textContent=(names[op.status]??op.status)+' · '+(op.text??op.phase??op.action)+(op.error?' · '+op.error:'');return p;
    }));
  }catch(error){demoStatus=null;}finally{updateDemo();setTimeout(demoPoll,1000);}
}
demoPoll();

const wardStatusNames={new:'已发现',assigned:'已安排',executing:'处理中',awaiting_verification:'等待新反馈核验',resolved:'已解决',unresolved:'未解决'};
const wardActionNames={create_delivery:'登记运输',navigate:'前往',pickup:'取标本',deliver:'交接标本',handoff:'标本交接',handoff_order:'标本交接',set_brightness:'调整灯光',adjust_light:'调整灯光',wait:'等待反馈',verify:'核验反馈',verify_services:'核验服务',standby:'待命',return:'返回'};
const wardDeviceNames={'hospital-robot-01':'配送机器人','hospital-cabinet-b':'B 护士站取件柜','hospital-cabinet-c':'C 护士站取件柜','hospital-cabinet-lab':'检验接收柜','hospital-light-301':'301 病房灯具','hospital-light-303':'303 病房灯具','hospital-sensor-301':'301 环境传感器','hospital-sensor-303':'303 环境传感器'};
function renderSmartWard(s){
  const w=s.smart_ward,watch=w.watch??{},incidents=Array.isArray(w.incidents)?w.incidents:Object.values(w.incidents??{});
  const pending=incidents.filter(i=>!['resolved','unresolved'].includes(i.status)),resolved=incidents.filter(i=>i.status==='resolved'),unresolved=incidents.filter(i=>i.status==='unresolved');
  const phase=s.operator_stopped?'stopped':!watch.enabled&&!watch.started_wall&&!['fault','error','paused'].includes(watch.phase)?'disabled':watch.phase,phaseText={idle:'值守中',watching:'值守中',processing:'正在处理',running:'正在处理',fault:'故障',error:'故障',stopped:'已停止',paused:'未开启',disabled:'未开启'}[phase]??(watch.enabled?(pending.length?'正在处理':'值守中'):'未开启');
  demoId('ward-watch-status').textContent=phaseText;demoId('ward-watch-status').className='pill '+(phase==='fault'||phase==='error'?'error':!watch.enabled?'warn':'');
  demoId('ward-watch-detail').textContent=s.operator_stopped?'值守与运动已锁定，未完成需求已保留。':watch.enabled?'设备发现需求后自动调度，处理结果由新反馈核验。':'开始值守后自动处理新需求，演示按钮只改变仿真设备。';
  demoId('ward-requirement-count').textContent=pending.length?'('+pending.length+')':'';
  demoId('ward-summary').replaceChildren(...[['待处理',pending.length],['已解决',resolved.length],['未解决',unresolved.length]].map(([name,count])=>node('span',name+' '+count)));
  const expanded=new Set([...demoId('ward-incidents').querySelectorAll('details[open]')].map(el=>el.dataset.incident));
  demoId('ward-incidents').replaceChildren(...(incidents.length?[...incidents].reverse().map(i=>{
    const card=node('article','');card.className='ward-incident '+i.status;
    const heading=node('header',''),title=node('strong',i.summary||(i.kind==='low_light'?'病房照明不足':'标本待取')),badge=node('span',wardStatusNames[i.status]??i.status);badge.className='tag';heading.append(title,badge);if(i.priority==='urgent'){const urgent=node('span','急件');urgent.className='tag urgent';heading.append(urgent);}card.append(heading);
    const source=typeof i.source==='string'?i.source:i.source?.label??i.source?.device_id??i.station_id??i.room_id??'';
    card.append(node('p','设备事件 · '+(wardDeviceNames[source]??stationNames[source]??source)+(Number.isFinite(i.created_sim)?' · '+i.created_sim.toFixed(1)+' 仿真秒':'')));
    if(i.reason)card.append(node('p',i.reason));
    const details=node('details','');details.dataset.incident=i.incident_id;details.open=expanded.has(i.incident_id);details.append(node('summary','需求、交接与反馈证据'),node('p','需求：'+i.incident_id));
    if(i.specimen_id){details.append(node('p','标本：'+i.specimen_id));const location=w.specimens?.[i.specimen_id]?.location;if(location)details.append(node('p','实际归属：'+(location==='robot'?'配送机器人':wardDeviceNames[location]??location)));}
    const order=i.order_id?s.orders?.[i.order_id]:null;
    if(i.order_id)details.append(node('p','订单：'+i.order_id));
    if(order?.receipt)details.append(node('p','交接回执：'+order.receipt.receipt_id+' · '+order.receipt.sim_time+' 仿真秒'));
    if(order?.nav_task_id)details.append(node('p','导航：'+order.nav_task_id));
    if(i.resolved_wall)details.append(node('p','恢复反馈确认于 '+new Date(i.resolved_wall*1000).toLocaleTimeString()));
    if(i.attempts!==undefined)details.append(node('p','调节尝试：'+i.attempts+' / 2'));
    if(i.evidence)details.append(node('p','反馈记录：'+JSON.stringify(i.evidence)));
    card.append(details);return card;
  }):[Object.assign(node('div','尚无需求。开始值守后，放入标本或调暗病房灯光即可观察自动发现与处理。'),{className:'empty'})]));
  renderWardDevices(s);
  const tickets=Array.isArray(w.maintenance_tickets)?w.maintenance_tickets:Object.values(w.maintenance_tickets??{});
  demoId('ward-maintenance').replaceChildren(...(tickets.length?tickets.map(t=>node('p',(t.summary??t.reason??t.kind??'设施处理事项')+' · '+(t.status??'待处理'))):[node('small','暂无待人工处理事项。')]));
  const plan=w.service_plan,card=demoId('arrangement');card.hidden=false;card.classList.toggle('stale',!!plan?.stale);
  demoId('plan-heading').textContent=s.operator_stopped?'本轮已停止':'Agnes 的服务安排';
  demoId('plan-reason').textContent=plan?.reason??(watch.enabled?'正在值守，等待设备需求。':'点击“开始值守”，再通过设备演示控件制造需求。');
  demoId('plan-stamp').textContent=plan?'更新于 '+new Date(plan.wall_time*1000).toLocaleTimeString()+' · 公开任务说明':'';
  const steps=plan?.steps??[];
  demoId('plan-steps').replaceChildren(...steps.map((step,index)=>{const row=node('div','');const status=s.operator_stopped&&!['completed','failed'].includes(step.status)?'ended':step.status??'planned';row.className='plan-step '+status;const target=stationNames[step.station_id]??wardDeviceNames[step.device_id]??step.device_id??'';row.append(node('strong',(index+1)+' · '+(wardActionNames[step.action]??step.action)+' '+target),node('span',phaseNames[status]??status));return row;}));
  const next=steps.find(step=>!['completed','failed','resolved'].includes(step.status));
  demoId('plan-next').textContent=s.operator_stopped?'本轮不再接受新设备任务。':next?'下一步：'+(wardActionNames[next.action]??next.action)+' '+(stationNames[next.station_id]??wardDeviceNames[next.device_id]??next.device_id??''):pending.length?'需求已登记，等待 Agnes 安排。':'没有待处理需求。';
}
function renderWardDevices(s){
  const devices=s.smart_ward.devices??{};
  demoId('ward-devices').replaceChildren(...Object.entries(devices).map(([id,d])=>{
    const card=node('article','');card.className='ward-device'+(d.fresh===false?' stale':'');card.append(node('strong',d.label??wardDeviceNames[id]??id));
    const values=[];
    if(id==='hospital-robot-01')values.push('载货 '+Object.values(s.orders??{}).filter(o=>o.loaded).length+' / '+s.capacity,s.stopped_confirmed?'已确认静止':'运动或待停止确认');
    if(d.door_open_fraction!==undefined)values.push('柜门开启 '+Math.round(d.door_open_fraction*100)+'%');
    else if(d.door!==undefined)values.push('柜门 '+({open:'开启',closed:'关闭'}[d.door]??d.door));
    if(d.available!==undefined)values.push(d.fresh===false?'可用状态未知':d.available?'可接收／操作':'暂停接收／操作');
    if(d.slots!==undefined){const slots=Array.isArray(d.slots)?d.slots:Object.values(d.slots);values.push('槽位 '+slots.filter(slot=>slot!==null&&slot!==false&&(typeof slot!=='object'||slot.specimen_id||slot.occupied)).length+' / '+slots.length);}
    else if(d.capacity!==undefined&&Array.isArray(d.specimens))values.push('槽位 '+d.specimens.length+' / '+d.capacity);
    if(d.brightness_percent!==undefined)values.push('实际亮度 '+d.brightness_percent+'%');
    if(d.relative_illumination!==undefined)values.push('相对照明指数 '+Number(d.relative_illumination).toFixed(1));
    if(d.stuck!==undefined)values.push(d.stuck?'故障：无响应':'响应正常');
    card.append(node('p',(d.fresh===false?'上次反馈：':'')+(values.length?values.join(' · '):'等待设备状态')));
    const tag=node('span',d.fresh===false?'反馈过期／未知':d.fresh===true?'反馈新鲜':'新鲜度未知');tag.className='tag';card.append(tag);
    if(d.capture_wall_time??d.wall_time)card.append(node('small','采样 '+new Date((d.capture_wall_time??d.wall_time)*1000).toLocaleTimeString()));
    if(d.provenance||d.source)card.append(node('small','来源：'+(d.provenance??d.source)));
    return card;
  }));
}

