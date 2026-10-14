// A completion check supplies feedback, never chooses or executes a route.
export function verificationKey(state){return state.smart_ward||state.protocol==='smart-ward-v1'||state.episode!==undefined?JSON.stringify([state.run_id,state.smart_ward?.episode??state.episode,state.smart_ward?.verification_event_version??state.verification_event_version]):JSON.stringify([state.run_id,state.segment,state.mhs_budget?.end_wall]);}
// Proof is scoped by the caller to this controller's ledger. An old successful
// wait cannot cover another adjustment attempt, including a retry in this run.
export function acceptedLightWatches(rows,state,nowWall=Date.now()/1000){
  const incidents=Object.values(state.smart_ward?.incidents??{});
  const results=new Map(rows.filter(row=>row.type==='tool/result'&&row.data?.isError!==true).map(row=>[row.data?.toolUseId,row]));
  const accepted=new Set();
  for(const row of rows){
    if(row.type!=='tool/call'||row.data?.name!=='watch_device')continue;
    const args=row.data.input??row.data.arguments??row.data.args??{},until=args.until??{},result=results.get(row.data.toolUseId);
    if(!row.data.toolUseId||!result||result.seq<=row.seq||until.state!=='service_done'||until.eq!==true||Object.keys(until).length!==2)continue;
    const wall=Date.parse(row.ts)/1000;
    const timeout=args.timeout??600;
    if(!Number.isFinite(timeout)||timeout<=0||!Number.isFinite(wall)||wall+Math.min(timeout,3600)<=nowWall)continue;
    // The watch ID need not be parsed from display text. A later successful
    // unwatch conservatively invalidates proofs until a new wait is registered.
    if(rows.some(other=>other.type==='tool/call'&&other.data?.name==='unwatch_device'&&other.seq>result.seq&&results.get(other.data.toolUseId)?.seq>other.seq))continue;
    for(const incident of incidents){
      if(incident.kind!=='low_light'||incident.status!=='awaiting_verification'||!Number.isFinite(incident.attempt_start_wall))continue;
      if(args.device==='hospital-sensor-'+String(incident.room_id).slice(-3)&&Number.isFinite(wall)&&wall>=incident.attempt_start_wall)accepted.add(incident.incident_id);
    }
  }
  return accepted;
}
export function completionFeedback(state,session,verified=false,acceptedWatches=new Set()){
  if(state.controller_session!==session||state.operator_stopped||state.mhs_budget?.stopped_reason)return {action:'stop'};
  if(state.smart_ward){
    const ward=state.smart_ward;
    if(!ward.watch?.enabled)return {action:'stop'};
    const incidents=Array.isArray(ward.incidents)?ward.incidents:Object.values(ward.incidents??{});
    if(!incidents.length)return {action:'stop'};
    if(incidents.every(x=>['resolved','unresolved'].includes(x.status))&&(state.mhs_finished||state.mhs_outcome==='incomplete')){
      if(verified||!state.stopped_confirmed)return {action:'stop'};
      return {action:'continue',terminalVerification:true,note:'本批需求已处理且机器人停稳。调用hospital_verify_services核验，简短分别报告已解决和未解决，随后保持值守等待新事件。不要再移动或凭命令成功宣称恢复。'};
    }
    if(ward.recovery_required||!ward.scene_fresh)return {action:'stop'};
    if(incidents.every(x=>['resolved','unresolved'].includes(x.status))&&state.stopped_confirmed)return {action:'continue',note:'设备需求已处理，但计划中的最终待命点尚未到达。继续已发布末段路线，真实到达停稳后再verify_services；不要重复取送或调灯。'};
    if(['pending','running','canceling'].includes(state.navigation?.status))return {action:'stop'};
    const pending=incidents.filter(x=>!['resolved','unresolved'].includes(x.status));
    if(pending.every(x=>x.status==='awaiting_verification'||x.status==='waiting')){
      const missing=pending.filter(x=>x.kind==='low_light'&&x.status==='awaiting_verification'&&!acceptedWatches.has(x.incident_id));
      if(missing.length)return {action:'continue',note:'调灯后还缺少本次反馈监听。对'+missing.map(x=>'hospital-sensor-'+String(x.room_id).slice(-3)).join('、')+'调用watch_device，until={"state":"service_done","eq":true}，然后结束回合等通知。不要重复调灯或轮询。'};
      return {action:'stop'};
    }
    if(!state.pose_fresh||!state.motion_guard?.healthy)return {action:'stop'};
    return {action:'continue',note:'仍有未完成的设备需求，当前无导航长任务。读取最新状态，继续已发布安排；需要等反馈时使用watch_device并结束回合，完成后verify_services。'};
  }
  if(state.mhs_finished||state.mhs_outcome==='incomplete'){
    if(verified||!state.pose_fresh||!state.stopped_confirmed)return {action:'stop'};
    return {action:'continue',terminalVerification:true,note:'本次已经结束且机器人停稳。只调用hospital_verify_run核验最终结果，然后用简短中文报告交付或未交付、保留载货和末站。不要继续运动或修改订单。未交付关闭后必须开始新一轮，不能承诺在本轮重试。不要输出分析、think标签、工具参数或长编号。'};
  }
  const orders=Object.values(state.orders??{});
  if(!orders.length||!state.pose_fresh||!state.navigation_ready||!state.motion_guard?.healthy)return {action:'stop'};
  if(['pending','running','canceling'].includes(state.navigation?.status))return {action:'stop'};
  const unfinished=orders.filter(order=>!['delivered','failed'].includes(order.status));
  // Unavailable receivers use native watch notifications. Do not busy-poll them.
  if(unfinished.length&&unfinished.every(order=>order.status==='deferred'&&state.receivers[order.dropoff_station]===false))return {action:'stop'};
  // Delivered orders can still have a user-requested return leg. The engine's
  // mhs_finished flag proves arrival at the published final point.
  return {action:'continue',note:'本轮配送尚未完成，当前没有活动导航。请读取最新状态，继续取货、交接或发下一目标；全部订单处理后在交接点待命并核验；用户要求返回时执行指定返回点。简短汇报不能替代实际动作。'};
}
