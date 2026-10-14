"""Complete deterministic V2 source edits before server build."""
import json
from pathlib import Path
from .prepare_v2 import ROOT,edit
def main():
    cfg=ROOT/'configs/nav2.yaml';text=cfg.read_text()
    for anchor in ['      plugins: ["hesai_voxel_layer", "inflation_layer"]','      plugins: ["static_layer", "obstacle_layer", "inflation_layer"]']:
        if anchor not in text:raise RuntimeError('costmap_filter_anchor_missing')
        text=text.replace(anchor,anchor+'\n      filters: ["hospital_route_filter"]\n      hospital_route_filter:\n        plugin: "nav2_costmap_2d::KeepoutFilter"\n        enabled: True\n        filter_info_topic: /carter1/route_filter_info\n        transform_tolerance: 0.3',1)
    cfg.write_text(text)
    edit('mhs/hospital_sim.py','enable_extension("isaacsim.ros2.bridge")','enable_extension("isaacsim.ros2.bridge")\nenable_extension("isaacsim.replicator.agent.core")')
    edit('mhs/hospital_sim.py','app_utils.play()','from crowd_v2 import HospitalCrowd\ncrowd=HospitalCrowd(ROOT,stage,seed=int(os.environ.get("HOSPITAL_PEOPLE_SEED","101")))\ncrowd_start=time.monotonic()\nwhile not crowd.ready():\n app.update()\n if time.monotonic()-crowd_start>180:raise RuntimeError("crowd_setup_timeout")\napp_utils.play()',) if False else None
    # Only initial play gets a setup; reset code must not instantiate duplicate actors.
    p=ROOT/'mhs/hospital_sim.py';t=p.read_text();t=t.replace('app_utils.play()','from crowd_v2 import HospitalCrowd\ncrowd=HospitalCrowd(ROOT,stage,seed=int(os.environ.get("HOSPITAL_PEOPLE_SEED","101")))\ncrowd_start=time.monotonic()\nwhile not crowd.ready():\n app.update()\n if time.monotonic()-crowd_start>180:raise RuntimeError("crowd_setup_timeout")\napp_utils.play()',1)
    t=t.replace(' if time.monotonic()-capture_at>1:', ' crowd.tick(SimulationManager.get_simulation_time())\n if time.monotonic()-capture_at>1:');p.write_text(t)
    edit('mhs/bridge.py',"STATION_LABELS={'depot':'物资库','station_a':'A 护士站','station_b':'B 护士站','station_c':'C 护士站'}","STATION_LABELS={k:v['label'] for k,v in json.loads((ROOT/'configs/stations.json').read_text()).items()}")
    edit('mhs/bridge.py',"name={'depot':'物资库','station_a':'A护士站','station_b':'B护士站','station_c':'C护士站'}[k]","name=v['label']")
    edit('mhs/bridge.py',"self.dev=Device('hospital-robot-01'","fields.update({('receiver_'+k):dict(type='boolean',writable=False) for k in STATION_LABELS})\n        fields['crowd_fresh']=dict(type='boolean',writable=False)\n        self.dev=Device('hospital-robot-01'")
    old="""self.dev.tool('导航到depot、station_a/b/c。活动配送前往护士站必须已装对应包裹，未装货先pickup_package；被拒绝后修正状态再次尝试用新operation_id。长任务running后等待原生完成通知；换目标先stop_device并检查stop_confirmed。',
            name='navigate_to',timeout=600,uses=['chassis'],motion=True,
            params={'station_id':{'type':'string','enum':['depot','station_a','station_b','station_c']},'operation_id':{'type':'string'}},
            required=['station_id','operation_id'])(self.navigate)"""
    new="""self.dev.tool('执行 hospital_get_route_options 返回的一条 Nav2 验证路线。填写route_id和唯一operation_id；同一请求传输重试复用，修正参数的新操作用新编号。长任务running后等待完成通知；换路线先stop_device并确认stop_confirmed。底盘自动让行、限速，无法继续时反馈Agent重规划。',
            name='navigate_route',timeout=3600,uses=['chassis'],motion=True,
            params={'route_id':{'type':'string'},'operation_id':{'type':'string'}},
            required=['route_id','operation_id'])(self.navigate)"""
    edit('mhs/bridge.py',old,new)
    edit('mhs/bridge.py','async def navigate(self,call,station_id,operation_id):','async def navigate(self,call,route_id,operation_id):')
    edit('mhs/bridge.py',"dict(station_id=station_id,operation_id=operation_id)","dict(route_id=route_id,operation_id=operation_id)")
    edit('mhs/bridge.py',"tid=task['nav_task_id'];initial=None","tid=task['nav_task_id'];station_id=task['goal_station'];initial=None")
    edit('mhs/bridge.py',"run_id=s['run_id'])","run_id=s['run_id'],crowd_fresh=s.get('crowd',{}).get('fresh',False),**{'receiver_'+k:v for k,v in s['receivers'].items()})")
    edit('mhs/bridge.py',"if not s['pose_fresh'] or not s['physical_pose']:","if not s['pose_fresh'] or not s['physical_pose'] or not s.get('crowd',{}).get('fresh'):")
    # Restore intended project preset even when installing while the simulation is stopped.
    edit('mhs/refresh_policy.py',"else:print('Policy installed; runtime is stopped. Start with mhs.manage start.')","else:\n        config=json.loads(p.read_text());config['presets']={'default':'hospital-mhs','allowed':['hospital-mhs']};config['packages']=[{'id':'hospital-mhs-policy','source':'file:./mhs/policy','enabled':True},{'id':'@agnes/mhs','source':'file:./agent/agh-source-mhs/packages/mhs/plugin','enabled':True}];p.write_text(json.dumps(config,indent=2))\n        print('Policy installed; runtime stopped.')")
    instructions='''你是院递医院配送助手。用户可指定取货点、交接点、物资、数量和优先级；数量或地点不明确先问。默认取货点物资库，普通900、急件300仿真秒，明确告知用户。十个命名地点由get_state提供，禁止编造坐标。容量两个。
先读取真实状态，创建订单后使用返回的planning_version发布剩余子任务及一句公开理由；子任务包括导航、取货、交接、等待、最终待命。修改安排先读最新状态。业务operation_id由适配层负责，不要填写。工具拒绝后按error读取状态修正，不能原样循环。
每个业务步骤由你决定执行。去任何取货点或交接点前，静止时调用get_route_options，然后比较可行路线，用call_device(device=hospital-robot-01,tool=navigate_route,args={route_id,operation_id})执行选定路线。普通件优先较少人群干扰，急件优先时效；必须尊重禁行、限速和让行。用一句话告知选择理由和导航时间估计（不含模型等待）。MHS运动operation_id必须唯一；传输重试同编号，改参数的新尝试新编号。
导航running后结束本轮，等待MHS原生完成通知；完成通知后读取状态并立即推进取货、交接或下一目标。不用重复提示或轮询代替完成通知。换目标或途中重新选路线，先stop_device，再确认真实静止及旧导航终态。人群和让行由本地程序实时处理；持续受阻会通知你选择绕行或等待，不能自行输出速度指令。
必须真实停稳于订单pickup_station才能pickup_package；交接使用匹配order_id和package_id，成功回执才算交付。急件插入时保留原订单与载货，根据容量安排不同取货点。接收暂停则defer_order，用watch_device监听receiver_站点ID=true以及event_version变化；每单最多两次交接，连续暂停150仿真秒关闭并如实报告。
默认全部交接后在最后交接点静止待命，verify_run并汇报回执、时效和未完成；用户要求返回则执行指定返回点再核验。下笔任务从实际当前位置继续。不要无故返物资库。仿真人物真值不是视觉识别；路线预计时间不是任务完成承诺。
像人交流一样用一两句自然中文，展示公开安排，不输出内部推理、工具名或长编号。读图只在用户询问画面或确实需要观察时使用first_person_camera，不为了配送反复读图。禁止shell、文件、其他设备、改变接收站或场景。每轮上限60次决策、2400仿真秒或3600墙钟秒；触限停止并明确记录未完成。'''
    for rel in ['mhs/instructions.md','mhs/policy/instructions.md']:(ROOT/rel).write_text(instructions,encoding='utf-8')
if __name__=='__main__':main()
