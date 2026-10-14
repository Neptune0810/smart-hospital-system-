// Point-to-point controls share the same canonical chat and SDK gateway.
const v2Names={depot:'物资库',station_a:'A护士站',station_b:'B护士站',station_c:'C护士站',pharmacy:'药房',lab:'检验交接点',emergency:'急诊接收点',room301:'301病房',room302:'302病房',room303:'303病房',room304:'304病房',waiting:'候诊接待台'};
Object.assign(eventLabels,{route_options_generated:'可行路线已生成',route_mask_confirmed:'通道限制已生效',patient_yield_started:'停车让行',patient_yield_finished:'让行结束',route_replan_required:'道路受阻，等待重新安排',scene_event:'演示环境发生变化',delivery_plan_published:'任务安排已更新'});
Object.assign(label,v2Names);Object.assign(stationNames,v2Names);Object.assign(actionNames,{navigate:'前往',standby:'待命'});
const v2Panel=document.createElement('section');v2Panel.className='card';
v2Panel.innerHTML=`<div style="display:flex;justify-content:space-between;align-items:center"><h2>点到点配送</h2><button class="small-btn" id="v2-toggle" aria-expanded="true">收起下单</button></div><div id="v2-form"><div class="demo-row"><label for="v2-pickup">取货点</label><select id="v2-pickup"></select><label for="v2-destination">交接点</label><select id="v2-destination"></select></div><div class="demo-row"><input id="v2-package" aria-label="物资名称" placeholder="物资名称，如药品" maxlength="40" style="width:130px"><input id="v2-count" type="number" min="1" max="6" value="1" aria-label="数量" style="width:55px"><select id="v2-priority" aria-label="配送优先级"><option value="normal">普通 · 900仿真秒</option><option value="urgent">急件 · 300仿真秒</option></select><button class="small-btn" id="v2-submit">发给 Agnes</button></div><small>地图点选 <label><input type="radio" name="map-pick" value="pickup">取货点</label> <label><input type="radio" name="map-pick" value="destination" checked>交接点</label> · 仅选命名业务点，路线另经 Nav2 校验</small><div class="demo-row"><label for="v2-crowd">仿真人群</label><select id="v2-crowd"><option value="normal">分散巡走</option><option value="crowded">候诊集中</option><option value="crossing">横穿走廊</option></select><button class="small-btn" id="v2-people">切换</button><select id="v2-zone" aria-label="演示通道"><option value="waiting_corridor">候诊通道</option><option value="east_corridor">东侧通道</option></select><button class="small-btn" id="v2-close">封闭</button><button class="small-btn" id="v2-open">恢复</button></div></div><div id="v2-state" class="demo-notice">正在读取状态</div><details><summary>候选路线与选择</summary><div id="v2-routes"></div></details>`;
const v2MapCard=demoId('map-card'),v2MapHeading=v2MapCard.querySelector('.map-heading');
v2Panel.className='card v2-popover';
v2MapHeading.insertBefore(demoId('v2-toggle')??v2Panel.querySelector('#v2-toggle'),demoId('expand-map'));
v2Panel.firstElementChild.remove();v2MapCard.append(v2Panel);
v2MapCard.querySelector('h2').textContent='医院分区 · 12 个交接点';
const layoutNotes=document.createElement('details');layoutNotes.className='layout-notes';
layoutNotes.innerHTML='<summary>房间分布与交接位置</summary><p>色块表示房间用途，圆点表示机器人停车交接位置。病房在门外交接，药房在柜台外取货。</p><div id="layout-care"></div><p id="layout-room">点击地图圆点查看对应房间。</p>';
v2MapCard.append(layoutNotes);
v2MapCard.querySelector('.map').title='点击命名点选择交接点；可在下单面板切换为取货点。';
const v2Info=v2Panel.querySelector('#v2-state');demoId('nav-detail').after(v2Info);demoId('nav-detail').hidden=true;
for(const id of ['v2-pickup','v2-destination'])for(const [key,name] of Object.entries(v2Names)){const option=document.createElement('option');option.value=key;option.textContent=name;document.getElementById(id).append(option);}
document.getElementById('v2-pickup').value='depot';document.getElementById('v2-destination').value='station_c';
for(const id of ['receiver-station','urgent-station']){
 const select=demoId(id),existing=new Set([...select.options].map(x=>x.value));
 for(const [key,name] of Object.entries(v2Names))if(!existing.has(key)&&(id!=='urgent-station'||key!=='pharmacy')){const option=document.createElement('option');option.value=key;option.textContent=name;select.append(option);}
}
function foldV2Form(fold){v2Panel.hidden=fold;demoId('v2-toggle').textContent=fold?'点到点下单':'关闭下单';demoId('v2-toggle').setAttribute('aria-expanded',String(!fold));}
demoId('v2-toggle').onclick=()=>foldV2Form(!v2Panel.hidden);foldV2Form(true);
demoId('v2-submit').onclick=async()=>{const op=await submitDemo('delivery',{pickup:demoId('v2-pickup').value,destination:demoId('v2-destination').value,quantity:Number(demoId('v2-count').value),priority:demoId('v2-priority').value,package_label:demoId('v2-package').value});if(op&&['sent','queued'].includes(op.status))foldV2Form(true);};
document.getElementById('v2-people').onclick=()=>submitDemo('scene',{kind:'crowd_mode',value:{mode:demoId('v2-crowd').value}});
for(const [id,closed] of [['v2-close',true],['v2-open',false]])document.getElementById(id).onclick=()=>submitDemo('scene',{kind:'closed_zone',value:{zone_id:demoId('v2-zone').value,closed}});
document.getElementById('overlay').addEventListener('click',event=>{
 if(!latest)return;const c=event.currentTarget,rect=c.getBoundingClientRect();const px=(event.clientX-rect.left)*c.width/rect.width,py=(event.clientY-rect.top)*c.height/rect.height;
 const x=latest.map.origin[0]+px*latest.map.resolution,y=latest.map.origin[1]+(c.height-py)*latest.map.resolution;
 const nearest=Object.entries(latest.stations).map(([id,p])=>({id,d:Math.hypot(x-p.x,y-p.y)})).sort((a,b)=>a.d-b.d)[0];
 if(nearest?.d<=2){const target=document.querySelector('input[name="map-pick"]:checked').value;demoId('v2-'+target).value=nearest.id;notice('已选'+(target==='pickup'?'取货点':'交接点')+'：'+v2Names[nearest.id]);const p=latest.stations[nearest.id],room=latest.hospital?.rooms?.find(r=>r.id===p.room_id);demoId('layout-room').textContent=(p.district??'')+' · '+p.label+'：'+(room?.basis??'命名交接点');layoutNotes.open=true;}
 else notice('请点击命名业务点附近；任意坐标不能装卸。');
});
const v2OriginalDraw=draw;
draw=s=>{
 for(const [id,p] of Object.entries(s.stations)){v2Names[id]=p.label;label[id]=p.label;stationNames[id]=p.label;}
 v2OriginalDraw(s);
 const current=(s.segment_order_ids??Object.keys(s.orders)).map(id=>s.orders[id]).filter(Boolean);
 const done=current.filter(o=>o.status==='delivered').length;
 $('delivered').textContent=done+' / '+current.length;
 $('delivered').nextElementSibling.textContent='本次交接 / 本次订单';
 $('remaining').textContent=current.filter(o=>!['delivered','failed'].includes(o.status)).length;
 const percent=current.length?Math.round(done/current.length*100):0;
 $('progress').style.width=percent+'%';$('progress').parentElement.setAttribute('aria-valuenow',percent);
 const c=$('overlay');if(!c.width)return;const g=c.getContext('2d');const xy=p=>[(p[0]-s.map.origin[0])/s.map.resolution,c.height-(p[1]-s.map.origin[1])/s.map.resolution];
 const selected=s.selected_route?.route_id;
 for(const route of s.route_options??[]){g.strokeStyle=route.route_id===selected?'#2479bd':'#829bb488';g.lineWidth=route.route_id===selected?6:3;g.setLineDash(route.route_id===selected?[]:[8,6]);g.beginPath();route.points.forEach((p,i)=>i?g.lineTo(...xy(p)):g.moveTo(...xy(p)));g.stroke();}
 g.setLineDash([]);
 for(const person of s.crowd?.people??[]){const p=xy(person.position);g.fillStyle=s.crowd.fresh?'#b875aa':'#999';g.beginPath();g.arc(...p,8,0,Math.PI*2);g.fill();g.strokeStyle='#b875aa55';g.beginPath();g.arc(...p,19,0,Math.PI*2);g.stroke();}
};
const roomColor=room=>room.id.startsWith('ward')?'#5687bd':room.id.startsWith('nurse')?'#138674':room.id==='pharmacy_room'?'#9a6ca4':room.id==='emergency_room'?'#b96452':'#a1824e';
window.drawHospitalRooms=(s,g,xy)=>{
 for(const room of s.hospital?.rooms??[]){const a=xy(room.bounds[0],room.bounds[3]),b=xy(room.bounds[2],room.bounds[1]),color=roomColor(room);g.fillStyle=color+'35';g.strokeStyle=color+'a0';g.lineWidth=2;g.fillRect(a[0],a[1],b[0]-a[0],b[1]-a[1]);g.strokeRect(a[0],a[1],b[0]-a[0],b[1]-a[1]);g.font='bold 19px sans-serif';g.fillStyle=color;g.fillText(room.label,a[0]+7,a[1]+23);}
 for(const zone of s.hospital?.zones??[]){if(!s.closed_zones?.includes(zone.id))continue;const a=xy(zone.bounds[0],zone.bounds[3]),b=xy(zone.bounds[2],zone.bounds[1]);g.fillStyle='#dc615055';g.fillRect(a[0],a[1],b[0]-a[0],b[1]-a[1]);g.fillStyle='#a44433';g.font='bold 18px sans-serif';g.fillText('已封闭',a[0]+5,a[1]+22);}
};
window.drawHospitalStations=(s,g,xy)=>{
 const placed=[];g.font='bold 20px sans-serif';
 for(const [id,p] of Object.entries(s.stations)){const q=xy(p.x,p.y),room=s.hospital?.rooms?.find(r=>r.id===p.room_id);g.strokeStyle='#29526888';g.lineWidth=2;if(room){g.setLineDash([5,4]);g.beginPath();g.moveTo(...q);g.lineTo(...xy(...room.handoff_anchor));g.stroke();g.setLineDash([]);}g.fillStyle='#fff';g.strokeStyle='#173047';g.lineWidth=2;g.beginPath();g.arc(...q,8,0,Math.PI*2);g.fill();g.stroke();const text=p.label.replaceAll(' ',''),w=g.measureText(text).width+12,h=27;const options=[[q[0]+14,q[1]-13],[q[0]+14,q[1]+12],[q[0]-w-14,q[1]-13],[q[0]+14,q[1]-45]];let box=options.map(([x,y])=>[Math.max(2,Math.min(x,$('overlay').width-w-2)),Math.max(2,y),w,h]).find(a=>!placed.some(b=>a[0]<b[0]+b[2]&&a[0]+a[2]>b[0]&&a[1]<b[1]+b[3]&&a[1]+a[3]>b[1]))??[...options[0],w,h];placed.push(box);g.fillStyle='#fffffff0';g.fillRect(...box);g.fillStyle='#173047';g.fillText(text,box[0]+6,box[1]+20);}
};
setInterval(()=>{
 const pointEvidence=latest?.station_navigation_evidence?.points;
 for(const id of ['v2-pickup','v2-destination','receiver-station','urgent-station','normal-first','normal-second'])for(const option of demoId(id).options){const status=pointEvidence?.[option.value]?.navigation_status,district=latest?.stations?.[option.value]?.district;option.textContent=v2Names[option.value]+(district?' · '+district:'')+(status==='last_attempt_failed'?' · 最近导航未通过':status==='not_run'?' · 待导航验证':'');}
 demoId('layout-care').textContent='西病区：B 护士站 — 301 / 302；东病区：C 护士站 — 303 / 304；中央：药房、物资库与 A 护士站；门厅：候诊接待。';
 const current=demoStatus?.viewing_current&&chatConnected,healthy=latest?.pose_fresh&&latest?.motion_guard?.healthy&&latest?.crowd?.fresh;
 for(const id of ['v2-submit','v2-people','v2-close','v2-open'])demoId(id).disabled=!current||!healthy||commandBusy||stopBusy||latest?.operator_stopped||latest?.mhs_outcome==='incomplete'||!!latest?.mhs_budget?.stopped_reason;
 // Stopping this run is always available independently of the selected chat.
 demoId('demo-stop').disabled=stopBusy||!latest||!!latest.operator_stopped;
 const a=demoStatus?.agent_activity,waiting=a?.phase==='waiting_model';
 const stopped=latest?.mhs_budget?.stopped_reason;
 const segmentIds=latest?.segment_order_ids??Object.keys(latest?.orders??{}),failedCount=segmentIds.filter(id=>latest?.orders?.[id]?.status==='failed').length;
 const text=stopped?'本轮已停止 · '+({decision_limit:'达到决策次数上限',sim_time_limit:'达到仿真时间上限',wall_time_limit:'达到运行时间上限'}[stopped]??stopped):latest?.mhs_outcome==='incomplete'?'本次结束 · '+failedCount+' 单未交付，请开始新一轮':waiting?'等待 Agnes 回复 · '+Math.max(0,Math.floor(Date.now()/1000-a.waiting_since))+' 秒':latest?.navigation&&['pending','running'].includes(latest.navigation.status)?'沿选定路线导航中':latest?.mhs_finished?(failedCount?'本次已结束 · '+failedCount+' 单未交付':'任务已交接，原地待命'):!Object.keys(latest?.orders??{}).length?'等待下单':'等待 Agent 下一子任务';
 const ward=latest?.smart_ward,serviceText=ward?latest.operator_stopped?'本轮已停止':waiting?'等待 Agnes 决策 · '+Math.max(0,Math.floor(Date.now()/1000-a.waiting_since))+' 秒':latest?.navigation&&['pending','running'].includes(latest.navigation.status)?'沿选定路线导航中':ward.watch?.enabled?'设备事件值守中':'尚未开启值守':text;
 demoId('v2-state').textContent=serviceText+' · '+(latest?.crowd?.fresh?'4名仿真人员反馈新鲜':'人群反馈不可用')+' · 速度上限 '+(latest?.speed_limit_mps??'NA')+' m/s';
 if(ward){v2Panel.hidden=true;for(const id of ['v2-submit','v2-people','v2-close','v2-open'])demoId(id).disabled=true;}
 const routes=latest?.route_options??[];demoId('v2-routes').replaceChildren(...routes.map(route=>{const el=document.createElement('p');el.textContent=(route.route_id===latest?.selected_route?.route_id?'已选择 · ':'候选 · ')+({shortest:'距离较短',patient_friendly:'患者友好',quiet_avoidance:'减少安静区通行'}[route.kind]??route.kind)+'：'+route.length_m+' m，导航估计 '+route.estimated_navigation_seconds+' 仿真秒（不含让行与模型等待），人群暴露 '+route.crowd_exposure_person_m+' 人·米';return el;}));
 if(a?.last_tool_error){const el=document.createElement('small');el.textContent='最近工具拒绝：'+a.last_tool_error.text;demoId('v2-routes').append(el);}
},1000);


