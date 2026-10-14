"""Isaac/Nav2 adapter. MHS commands never directly set a robot pose."""
import asyncio,json,math,os,time,uuid,hashlib,struct,logging,urllib.request,urllib.error,contextvars
from pathlib import Path
from agnes_mhs import Device,CallError
import agnes_mhs.device as sdk_device
from agnes_mhs.schema import errors
from websockets.asyncio.client import connect
from websockets.exceptions import ConnectionClosedOK
from .outcome import delivery_wait_flags
from .runtime import mode,SMART_MODE,data_dir,map_yaml,map_image
from .state_sampler import StateSampler
from .scene_io import default_operation_id

ROOT=Path(__file__).resolve().parents[1]
API='http://127.0.0.1:4188'
HUB=os.environ.get('HOSPITAL_MHS_HUB','ws://127.0.0.1:4192')
SMART=mode(ROOT)==SMART_MODE
CAPTURE=data_dir(ROOT)/'protocol'/time.strftime('%Y%m%d-%H%M%S')
if os.environ.get('HOSPITAL_MHS_CAPTURE_DIR'):
    custom=Path(os.environ['HOSPITAL_MHS_CAPTURE_DIR']).resolve()
    if not os.environ.get('HOSPITAL_MHS_DEVICE_SELECT') or not HUB.startswith('ws://127.0.0.1:') or HUB.rstrip('/')=='ws://127.0.0.1:4192' or not custom.is_relative_to((data_dir(ROOT)/'acceptance').resolve()):raise RuntimeError('capture_override_only_for_project_checker_evidence')
    CAPTURE=custom
CAPTURE.mkdir(parents=True,exist_ok=True)
CHANNELS={'command':False,'nerve':False,'registered':False}
WIRE_DEVICE=contextvars.ContextVar('hospital_wire_device',default='hospital-robot-01')
DEVICE_CHANNELS={'hospital-robot-01':CHANNELS}
STATION_LABELS={k:v['label'] for k,v in json.loads((ROOT/'configs/stations.json').read_text()).items()}

def request(path,args=None):
    req=urllib.request.Request(API+path,data=None if args is None else json.dumps(args).encode(),headers={'Content-Type':'application/json'})
    try:
        with urllib.request.urlopen(req,timeout=2) as f:r=json.load(f)
    except urllib.error.HTTPError as e:r=json.load(e)
    if r.get('ok') is False:raise RuntimeError(r.get('error'))
    return r.get('result',r)

state_sampler = StateSampler(lambda: request('/api/state'))

async def fresh_operation_id(call, device_id, tool_name):
    # Do not derive actuation identity from a telemetry cache across new rounds.
    state = await asyncio.to_thread(request, '/api/state')
    ward = state.get('smart_ward') or {}
    if ward.get('scene_fresh') is not True:
        raise CallError('failed', 'Fresh current scene scope is unavailable; no device operation submitted.')
    return default_operation_id(state.get('run_id'), ward.get('epoch'), device_id, tool_name, call.id)

class WireSocket:
    def __init__(self,ws,channel,device):self.ws=ws;self.channel=channel;self.device=device
    def __getattr__(self,name):return getattr(self.ws,name)
    def record(self,value,direction):
        with (CAPTURE/'wire.jsonl').open('a') as f:
            if isinstance(value,bytes):
                digest=hashlib.sha256(value).hexdigest();name=digest+'.bin';(CAPTURE/name).write_bytes(value)
                row=dict(time=time.time(),device=self.device,channel=self.channel,direction=direction,binary=dict(sha256=digest,size=len(value),file=name))
            else:
                m=json.loads(value);row=dict(time=time.time(),device=self.device,channel=self.channel,direction=direction,message=m)
                with (CAPTURE/'messages.jsonl').open('a') as raw:raw.write(json.dumps(m,ensure_ascii=False)+'\n')
            f.write(json.dumps(row,ensure_ascii=False)+'\n')
    async def send(self,value):
        await self.ws.send(value);self.record(value,'out')
    async def recv(self):
        value=await self.ws.recv();self.record(value,'in')
        if self.channel=='command' and isinstance(value,str):
            m=json.loads(value)
            if m.get('id')=='1' and 'result' in m:DEVICE_CHANNELS[self.device]['registered']=True
        return value
    def __aiter__(self):return self
    async def __anext__(self):
        try:return await self.recv()
        except ConnectionClosedOK:raise StopAsyncIteration

class WireConnect:
    def __init__(self,url,**kwargs):
        self.connection=connect(url,**kwargs);self.channel='nerve' if url.endswith('/ws/nerve') else 'command'
        self.device=WIRE_DEVICE.get();self.channels=DEVICE_CHANNELS.setdefault(self.device,{'command':False,'nerve':False,'registered':False})
    async def __aenter__(self):
        ws=await self.connection.__aenter__();self.channels[self.channel]=True
        if self.channel=='command':self.channels['registered']=False
        return WireSocket(ws,self.channel,self.device)
    async def __aexit__(self,*args):
        self.channels[self.channel]=False
        if self.channel=='command':self.channels['registered']=False
        return await self.connection.__aexit__(*args)

# Transport instrumentation only; SDK protocol and approval implementations are unchanged.
sdk_device.connect=WireConnect

def world():
    stations=json.loads((ROOT/'configs/stations.json').read_text())
    text=map_yaml(ROOT).read_text()
    import re
    resolution=float(re.search(r'resolution:\s*([\d.]+)',text).group(1))
    origin=json.loads(re.search(r'origin:\s*(\[[^\]]+\])',text).group(1))
    w,h=struct.unpack('>II',map_image(ROOT).read_bytes()[16:24])
    return {'id':'hospital','name':'院递医院','bounds':[origin[0],origin[1],origin[0]+w*resolution,origin[1]+h*resolution],
        'places':[dict(id=k.replace('_','-'),name=v['label'],at=[v['x'],v['y']],yaw=math.degrees(v['yaw'])) for k,v in stations.items()]}

class HospitalBridge:
    def __init__(self):
        self.latest=None;self.connected=False;self.last_pose=None;self.last_image={};self.last_publish=0;self.last_image_publish={}
        fields={k:dict(type=t,writable=False) for k,t in [('navigation','string'),('loaded','integer'),('event_version','integer'),('receiver_a','boolean'),('receiver_b','boolean'),('receiver_c','boolean'),('stop_confirmed','boolean'),('run_id','string')]}
        fields.update({('receiver_'+k):dict(type='boolean',writable=False) for k in STATION_LABELS})
        fields['crowd_fresh']=dict(type='boolean',writable=False)
        fields['motion_clear']=dict(type='boolean',writable=False)
        fields.update({('delivery_wait_'+k):dict(type='boolean',writable=False) for k in STATION_LABELS})
        fields['task_outcome']=dict(type='string',writable=False)
        self.dev=Device('hospital-robot-01','robot',name='院递配送机器人',model='Isaac Sim Nova Carter',mobile=True,
            profile={'notes':'单层医院仿真，容量两个包裹；地图当前命名点均可作取货点和交接点，具体业务状态由 hospital_get_state 读取；未指定取货点才默认物资库。Nav2运动，仿真交接；第一人称相机随车头转向；第三人称用于展示。'},
            localization='self',maps=[world()],resources={'chassis':'reject'},state=fields,ui={'primary':'first_person_camera'})
        self.dev.update(navigation='unknown',loaded=0,event_version=0,receiver_a=False,receiver_b=False,receiver_c=False,stop_confirmed=False,run_id='')
        self.pose=self.dev.source('pose','pose','ROS map-frame localization in metres and degrees',hz=1,model={'name':'Nav2 AMCL','version':'ROS2 Humble'})
        self.odometry=self.dev.source('odometry','odometry','实测ROS chassis/odom里程计，相对本次进程起点；米、度、m/s、deg/s。不是地图定位。',hz=1)
        self.image=self.dev.source('demo_camera','image','第三人称仿真跟随画面；采集时间为Unix墙钟，age为墙钟秒；不是车载视野',hz=1,mime='image/jpeg',encoding='rgb',size=[960,540],default=False)
        self.first_image=self.dev.source('first_person_camera','image','车身前方第一人称相机，随实际车头转向；采集时间为Unix墙钟，age为墙钟秒',hz=1,mime='image/jpeg',encoding='rgb',size=[960,540],default=True)
        self.stop_waiter=None
        self.dev.on_stop=self.stop_native;self.dev.after=self.after;self.dev.unsafe=self.unsafe
        self.dev.tool('执行 hospital_get_route_options 返回的一条 Nav2 验证路线。必填route_id；operation_id可省略，由MHS真实call.id生成。显式编号的同一请求传输重试复用，修正参数的新操作用新编号。长任务running后等待完成通知；换路线先stop_device并确认stop_confirmed。底盘自动让行、限速，无法继续时反馈Agent重规划。',
            name='navigate_route',timeout=3600,uses=['chassis'],motion=True,
            params={'route_id':{'type':'string'},'operation_id':{'type':'string'}},
            required=['route_id'])(self.navigate)
        issues=errors('RegisterParams',self.dev.description())
        if issues:raise RuntimeError('invalid_device_registration: '+str(issues))
        (CAPTURE/'registration.json').write_text(json.dumps(self.dev.description(),ensure_ascii=False,indent=2))
    def unsafe(self,tool):
        s=self.latest
        if not self.connected:return 'hub_not_available'
        if s and s.get('operator_stopped'):return 'run_stopped_by_operator_new_round_required'
        if not s or not s.get('pose_fresh') or not s.get('physical_pose') or not s.get('navigation_ready'):return 'telemetry_or_navigation_not_ready'
        if not s.get('crowd',{}).get('fresh'):return 'crowd_telemetry_not_ready'
        if s.get('mhs_budget',{} ) and s['mhs_budget'].get('stopped_reason'):return 'run_budget_exhausted'
        if not s.get('stopped_confirmed'):return 'previous_motion_not_stopped'
        return None
    def after(self):
        s=self.latest or {};p=s.get('pose');o=s.get('odometry')
        r={}
        if o:r['odometry']=o
        if p:r['pose']=dict(map='hospital',x=p['x'],y=p['y'],yaw=math.degrees(p['yaw']),ok=bool(s.get('pose_fresh') and s.get('physical_pose')))
        return r
    async def stop_native(self):
        # MHS has a 2 s control-response limit. Send real zero/cancel first;
        # acknowledge the request promptly, while measured stillness gates
        # every subsequent target independently of this acknowledgement.
        await asyncio.to_thread(request,'/api/mhs/stop',{'reason':'mhs_stop'})
        self.dev.update(stop_confirmed=False)
        self.dev.problem='正在确认导航终态与真实静止，暂不允许新目标。'
        if self.stop_waiter is None or self.stop_waiter.done():
            self.stop_waiter=asyncio.create_task(self.confirm_stop())
    async def confirm_stop(self):
        try:
            # The stationary predicate requires a full simulation second plus
            # physical settling. At the measured ~0.22 real-time factor, eight
            # wall seconds can expire before that evidence arrives. Keep the
            # same predicate; only bound the observation window at 20 s.
            end=time.monotonic()+20
            while time.monotonic()<end:
                s=await asyncio.to_thread(request,'/api/state');self.latest=s
                if s.get('stopped_confirmed'):
                    self.dev.update(stop_confirmed=True);self.dev.problem=None;return s
                await asyncio.sleep(.1)
            self.dev.problem='停止未确认：导航终态或真实速度尚未满足。禁止新目标。'
            self.dev.faults=['x_stop_unconfirmed']
        except Exception as e:self.dev.problem='无法确认停止：'+str(e);self.dev.faults=['x_stop_unconfirmed']
    async def stop(self):
        await self.stop_native()
        return await asyncio.shield(self.stop_waiter)
    async def navigate(self,call,route_id,operation_id=None):
        # The official SDK keeps call.id stable across replay of that call.
        # A new model invocation is a new operation and still passes motion gates.
        if operation_id is None:operation_id=(await fresh_operation_id(call,'hospital-robot-01','navigate_route')) if SMART else 'mhs-call:'+call.id
        ended=False
        try:
            task=await asyncio.to_thread(request,'/api/mhs/navigate',dict(route_id=route_id,operation_id=operation_id))
            tid=task['nav_task_id'];station_id=task['goal_station'];initial=None
            while True:
                s=await asyncio.to_thread(request,'/api/state');self.latest=s
                n=s.get('navigation_tasks',{}).get(tid) or s.get('navigation') or {}
                if n.get('nav_task_id')!=tid:raise CallError('x_task_changed','Navigation task identity changed')
                p=s.get('pose');target=s['stations'][station_id]
                distance=math.hypot(p['x']-target['x'],p['y']-target['y']) if p else None
                if initial is None and distance is not None:initial=max(distance,.01)
                progress=s.get('navigation_progress') or {};actual_remaining=progress.get('distance_remaining_m') if progress.get('fresh') and progress.get('nav_task_id')==tid else None
                detail=f'，路线剩余约 {actual_remaining:.1f} 米' if actual_remaining is not None else f'，到目标直线约 {distance:.1f} 米' if distance is not None else ''
                call.progress(text='正在去'+STATION_LABELS[station_id]+detail,data={'nav_task_id':tid,'distance_to_goal_m':distance,'route_remaining_m':actual_remaining,'status':n['status']})
                if n['status'] in {'succeeded','failed','canceled'}:
                    ended=True
                    if n['status']=='succeeded':
                        s=await self.stop() or {}
                        if not s.get('stopped_confirmed'):raise CallError('x_stop_unconfirmed','Nav2 arrived but actual stationary state was not confirmed')
                        with (CAPTURE/'completion-index.jsonl').open('a') as f:
                            f.write(json.dumps({'wall_time':time.time(),'navigation':n,'stopped_confirmed':True},ensure_ascii=False)+'\n')
                        return {'detail':'[设备反馈] 已到达'+STATION_LABELS[station_id]+'，机器人已停稳。'}
                    raise CallError('cancel' if n['status']=='canceled' else 'failed','Nav2 '+n['status'],data=n)
                await asyncio.sleep(.25)
        except RuntimeError as error:
            raise CallError('failed',str(error)) from error
        finally:
            if not ended:
                # Also runs for SDK cancellation, lost command connection and timeout.
                await asyncio.shield(self.stop())
    async def monitor_hub(self):
        while True:
            try:
                # Observed real SDK transport lifecycle and successful registration.
                # Works with both the official Hub and its conformance test hub.
                self.connected=all(CHANNELS.values())
                await asyncio.to_thread(request,'/api/mhs/lease',{'connected':self.connected})
            except Exception as e:
                logging.warning('Hub monitor: %s',e);self.connected=False
                try:await asyncio.to_thread(request,'/api/mhs/lease',{'connected':False})
                except Exception:pass
            await asyncio.sleep(.5)
    async def telemetry(self):
        while True:
            try:
                s=await state_sampler.get();self.latest=s
                n=s.get('navigation') or {};b=s.get('mhs_budget') or {}
                publish=time.monotonic()-self.last_publish>=1
                if publish:
                    self.last_publish=time.monotonic()
                    self.dev.update(navigation=n.get('status','idle'),loaded=sum(o['loaded'] for o in s['orders'].values()),
                        event_version=s['latest_event_id'],receiver_a=s['receivers']['station_a'],receiver_b=s['receivers']['station_b'],
                        receiver_c=s['receivers']['station_c'],stop_confirmed=s['stopped_confirmed'],run_id=s['run_id'],crowd_fresh=s.get('crowd',{}).get('fresh',False),motion_clear=s.get('motion_clear',False),task_outcome=s.get('mhs_outcome') or ('completed' if s.get('mhs_finished') else 'active'),**{'receiver_'+k:v for k,v in s['receivers'].items()},**delivery_wait_flags(s))
                if not s['pose_fresh'] or not s['physical_pose'] or not s.get('crowd',{}).get('fresh'):self.dev.problem='定位或物理反馈过期'
                elif b.get('stopped_reason'):self.dev.problem='本轮停止：'+b['stopped_reason']
                elif s['stopped_confirmed']:self.dev.problem=None;self.dev.faults=[]
                p=s.get('pose');stamp=s.get('pose_wall_time')
                if publish and s.get('odometry') and s['pose_fresh'] and s['physical_pose'] and self.odometry.wants():
                    self.odometry.send(s['odometry'],t=stamp)
                if publish and p and s['pose_fresh'] and s['physical_pose'] and stamp!=self.last_pose and self.pose.wants():
                    self.pose.send(dict(map='hospital',x=p['x'],y=p['y'],yaw=math.degrees(p['yaw']),ok=True),t=stamp);self.last_pose=stamp
                for view,source,filename in [('first_person',self.first_image,'first-person.jpg'),('third_person',self.image,'latest.jpg')]:
                    if not source.wants() or time.monotonic()-self.last_image_publish.get(view,0)<1:continue
                    try:
                        meta=await asyncio.to_thread(request,'/api/frame?view='+view)
                        raw=(ROOT/'runs'/filename).read_bytes()
                        if meta['capture_wall_time']!=self.last_image.get(view) and hashlib.sha256(raw).hexdigest()==meta['sha256'] and 0<=time.time()-meta['capture_wall_time']<=.35:
                            source.send({'w':meta['width'],'h':meta['height']},raw,t=meta['capture_wall_time'])
                            self.last_image[view]=meta['capture_wall_time'];self.last_image_publish[view]=time.monotonic()
                            with (CAPTURE/'image-index.jsonl').open('a') as f:f.write(json.dumps(dict(meta,source='first_person_camera' if view=='first_person' else 'demo_camera'))+'\n')
                    except Exception:pass
            except Exception as e:self.dev.problem='配送服务不可用：'+str(e)
            await asyncio.sleep(.1)
    async def run(self):
        WIRE_DEVICE.set('hospital-robot-01')
        tasks=[asyncio.create_task(self.monitor_hub()),asyncio.create_task(self.telemetry())]
        try:await self.dev.run(HUB)
        finally:
            await self.stop()
            for task in tasks:task.cancel()
            await asyncio.gather(*tasks,return_exceptions=True)


class WardDeviceBridge:
    """Seven stationary devices; only fresh scene samples are streamed."""
    def __init__(self,ident,kind,station_id=None,room_id=None):
        self.ident,self.kind,self.station_id,self.room_id=ident,kind,station_id,room_id
        self.latest=None;self.last_sample=None
        station=json.loads((ROOT/'configs/stations.json').read_text())[station_id or room_id]
        position=[station['x'],station['y']]
        if kind=='cabinet':
            proof=json.loads((data_dir(ROOT)/'scene/layout-proof.json').read_text())
            position=next(p['position'][:2] for p in proof['placements'] if p['id']==ident)
        common={'available':'boolean','fresh':'boolean','sample_seq':'integer','sample_wall_time':'number','run_id':'string','service_done':'boolean'}
        extra={'cabinet':{'slots_used':'integer','capacity':'integer','door_open':'boolean'},
               'light':{'brightness_percent':'number','fault_stuck':'boolean'},
               'sensor':{'relative_illumination':'number','recovered':'boolean'}}[kind]
        fields={k:dict(type=t,writable=False) for k,t in (common|extra).items()}
        names={'hospital-cabinet-b':'B站标本取件柜','hospital-cabinet-c':'C站标本取件柜','hospital-cabinet-lab':'检验标本接收柜','hospital-light-301':'301病房灯具','hospital-light-303':'303病房灯具','hospital-sensor-301':'301相对照明传感器','hospital-sensor-303':'303相对照明传感器'}
        self.dev=Device(ident,kind,name=names[ident],model='院递智慧病区仿真设备',localization='fixed',
            placement={'map':'hospital','x':position[0],'y':position[1],'yaw':0},maps=[world()],
            profile={'notes':'simulation_state；箱体自动仿真交接，无机械臂。照明为simulation_model相对指数，不是lux。'},
            state=fields,resources={'actuator':'reject'} if kind!='sensor' else None)
        defaults={k:(False if t=='boolean' else '' if t=='string' else 0) for k,t in (common|extra).items()}
        self.dev.update(**defaults);self.dev.unsafe=self.unsafe
        source_fields={k:dict(type=t) for k,t in extra.items()}
        self.reading=self.dev.source('feedback','values','新场景采样；过期不重发，fresh=false表示未知。',hz=1,default=True,fields=source_fields)
        if kind=='cabinet':
            self.dev.tool('按已登记标本订单取件或接收；机器人真实到对应点并停稳、编号及容量匹配才可交接。返回需场景ACK和新采样一致；未知状态按同operation_id对账，禁止新编号重复发货。',name='handoff_order',timeout=90,uses=['actuator'],params={'order_id':{'type':'string'},'operation_id':{'type':'string'}},required=['order_id'])(self.handoff)
        elif kind=='light':
            self.dev.tool('设置局部灯具0–100亮度，正常60；命令成功不代表恢复。随后watch对应sensor的service_done=true并重读业务，恢复需连续3个新指数>=50。每事件最多两次，等待30仿真秒/60墙钟秒。',name='set_brightness',timeout=90,uses=['actuator'],params={'brightness_percent':{'type':'number','minimum':0,'maximum':100},'operation_id':{'type':'string'}},required=['brightness_percent'])(self.brightness)
        issues=errors('RegisterParams',self.dev.description())
        if issues:raise RuntimeError('invalid_ward_registration:'+ident+':'+str(issues))
        (CAPTURE/(ident+'-registration.json')).write_text(json.dumps(self.dev.description(),ensure_ascii=False,indent=2))
    def unsafe(self,tool):
        s=self.latest or {};ward=s.get('smart_ward',{});d=ward.get('devices',{}).get(self.ident,{})
        if not all(DEVICE_CHANNELS.get(self.ident,{}).values()) or not DEVICE_CHANNELS.get(self.ident):return 'hub_not_available'
        if s.get('operator_stopped') or not ward.get('watch',{}).get('enabled'):return 'watch_not_active'
        if not d.get('fresh') or not d.get('available'):return 'device_unavailable_or_stale'
        if ward.get('recovery_required'):return 'reconciliation_required'
        return None
    async def execute(self,call,tool,args,operation_id):
        from urllib.parse import quote
        if operation_id is None:operation_id=await fresh_operation_id(call,self.ident,tool)
        result=await asyncio.to_thread(request,'/api/ward/device',{'device_id':self.ident,'tool':tool,'arguments':dict(args,operation_id=operation_id)})
        # A repeated call can join an earlier prepared operation. Its returned
        # identity remains authoritative throughout polling and reconciliation.
        operation_id=result.get('operation_id',operation_id)
        deadline=time.monotonic()+80
        while result.get('status') in {'pending','unknown'}:
            call.progress(text='等待场景执行与新反馈核验',data={'operation_id':operation_id,'status':result['status']})
            if time.monotonic()>deadline:raise CallError('failed','场景交接/执行状态未知；保留原操作编号对账，禁止重复发货',data=result)
            await asyncio.sleep(.25)
            result=await asyncio.to_thread(request,'/api/ward/operation?id='+quote(operation_id,safe=''))
        if result.get('status')!='completed':raise CallError('failed',result.get('error','scene_operation_failed'),data=result)
        return {'detail':'[设备反馈] 场景执行已确认；需求是否恢复以hospital_get_state / verify_services为准。','data':result}
    async def handoff(self,call,order_id,operation_id=None):
        return await self.execute(call,'handoff_order',{'order_id':order_id},operation_id)
    async def brightness(self,call,brightness_percent,operation_id=None):
        return await self.execute(call,'set_brightness',{'brightness_percent':brightness_percent},operation_id)
    async def telemetry(self):
        last_publish=None
        while True:
            try:
                s=await state_sampler.get();self.latest=s
                w=s.get('smart_ward',{});d=w.get('devices',{}).get(self.ident,{})
                sample=d.get('seq',0);wall=d.get('wall_time',0)
                valid_stamp=(isinstance(sample,int) and not isinstance(sample,bool) and sample>0 and
                             isinstance(wall,(int,float)) and not isinstance(wall,bool) and math.isfinite(wall) and wall>0)
                age=time.time()-wall if valid_stamp else float('inf')
                fresh=bool(d.get('fresh')) and valid_stamp and 0<=age<=3
                relevant=[i for i in w.get('incidents',[]) if i.get('room_id')==self.room_id] if self.room_id else [i for i in w.get('incidents',[]) if i.get('cabinet_id')==self.ident]
                done=bool(relevant) and all(i['status'] in {'resolved','unresolved'} for i in relevant)
                values=({'slots_used':len(d.get('specimens',[])),'capacity':d.get('capacity',0),'door_open':bool(d.get('door_open_fraction',d.get('door_angle',0)))} if self.kind=='cabinet' else
                        {'brightness_percent':float(d.get('brightness_percent',0)),'fault_stuck':bool(d.get('stuck',d.get('fault_stuck',False)))} if self.kind=='light' else
                        {'relative_illumination':float(d.get('relative_illumination',0)),'recovered':fresh and done and all(i['status']=='resolved' for i in relevant)})
                self.dev.update(available=fresh and bool(d.get('available')),fresh=fresh,sample_seq=sample if valid_stamp else 0,sample_wall_time=float(wall) if valid_stamp else 0.,run_id=s['run_id'],service_done=done,**values)
                self.dev.problem=None if fresh else '场景采样过期或仿真暂停，状态未知'
                stamp=(s['run_id'],sample,wall)
                now=time.monotonic()
                # Poll faster than the scene's actual 1 Hz sampling so we do not
                # stream a capture almost a second old due to independent phase.
                # Keep the original capture timestamp; old samples are withheld,
                # never restamped. A strict publish gate also caps SDK's 0.9/hz
                # tolerance at one actual send per second for this source.
                if fresh and age<=.35 and stamp!=self.last_sample and (last_publish is None or now-last_publish>=1):
                    if self.reading.wants() and self.reading.send(values,t=float(wall)):
                        self.last_sample=stamp;last_publish=now
            except Exception as error:self.dev.problem='设备反馈不可用：'+str(error);self.dev.update(available=False,fresh=False)
            await asyncio.sleep(.1)
    async def run(self):
        WIRE_DEVICE.set(self.ident)
        task=asyncio.create_task(self.telemetry())
        try:await self.dev.run(HUB)
        finally:task.cancel();await asyncio.gather(task,return_exceptions=True)

async def main():
    adapters=[HospitalBridge()]
    if SMART:
        adapters += [WardDeviceBridge(ident,'cabinet',station_id=station) for ident,station in [('hospital-cabinet-b','station_b'),('hospital-cabinet-c','station_c'),('hospital-cabinet-lab','lab')]]
        adapters += [WardDeviceBridge('hospital-'+kind+'-'+suffix,kind,room_id='room'+suffix) for suffix in ['301','303'] for kind in ['light','sensor']]
    selected=os.environ.get('HOSPITAL_MHS_DEVICE_SELECT')
    if selected:
        if not HUB.startswith('ws://127.0.0.1:') or HUB in {'ws://127.0.0.1:4192','ws://127.0.0.1:4192/'}:raise RuntimeError('device_selection_only_for_isolated_loopback_checker')
        adapters=[a for a in adapters if a.dev.id==selected]
        if len(adapters)!=1:raise RuntimeError('unknown_project_device_selection')
    tasks=[asyncio.create_task(adapter.run()) for adapter in adapters]
    try:await asyncio.gather(*tasks)
    finally:
        for task in tasks:task.cancel()
        await asyncio.gather(*tasks,return_exceptions=True)

if __name__=='__main__':
    logging.basicConfig(level=logging.INFO)
    asyncio.run(main())
