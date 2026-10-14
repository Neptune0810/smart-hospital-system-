"""Lifecycle restricted to the hospital project's MHS processes and container."""
import argparse,json,math,os,signal,subprocess,time,resource,urllib.request,urllib.error,re,sys
from pathlib import Path
from .runtime import SMART_MODE,mode,data_dir,map_yaml
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'scripts'))
import manage as legacy
HOME=ROOT/'agent/agh-home-mhs'
NAME='hospital-delivery-mhs-runtime';LABEL='hospital-delivery-agent-mhs'
LOG=ROOT/'logs/mhs';LOG.mkdir(exist_ok=True)
MODE=mode(ROOT);DATA_DIR=data_dir(ROOT);MAP_YAML=map_yaml(ROOT)
RUNTIME=ROOT/('agent/agh-runtime-smart-ward145' if MODE==SMART_MODE else 'agent/agh-runtime-mhs')
ENV=dict(os.environ,AGH_HOME=str(HOME),AGNES_PROFILE='local-dev',AGNES_WEB_ORIGIN='http://127.0.0.1:4189',
    AGNES_HUB_LISTEN='127.0.0.1:4192',AGNES_HUB_ORIGIN='http://127.0.0.1:4192',AGNES_HUB_DATA=str(HOME/'hub'),
    AGNES_MCP_STDIO_ALLOWLIST=str(ROOT/'mhs/mcp.sh'),HOSPITAL_MODE=MODE,HOSPITAL_ROOT=str(ROOT),HOSPITAL_DATA_DIR=str(DATA_DIR),HOSPITAL_MAP_YAML=str(MAP_YAML))
if MODE==SMART_MODE:ENV['HOSPITAL_INFER_ADMISSION']='smart-ward-v1.89'
else:ENV.pop('HOSPITAL_INFER_ADMISSION',None)
CLI=['node',str(RUNTIME/'agnes.mjs')]
def process(name):
    marker=LOG/(name+'.pid')
    if not marker.exists():return None
    pid=int(marker.read_text())
    try:
        q=Path('/proc')/str(pid)
        if q.joinpath('stat').read_text().split(') ',1)[1].startswith('Z '):return None
        argv=q.joinpath('cmdline').read_bytes().replace(b'\0',b' ').decode();cwd=q.joinpath('cwd').resolve()
        expected={'sim':str(ROOT/'mhs/hospital_sim.py'),'bridge':'mhs.bridge','web':str(RUNTIME/'agnes.mjs'),'daemon':str(RUNTIME/'daemon.mjs'),'workbench':str(ROOT/'mhs/dist/workbench.mjs')}.get(name)
        if cwd!=ROOT or not expected or expected not in argv:
            # A process can exit between reading stat and cmdline. Recheck
            # before treating its empty zombie command line as another owner.
            if q.joinpath('stat').read_text().split(') ',1)[1].startswith('Z '):return None
            raise RuntimeError('process_ownership_mismatch:'+name)
        if name=='web' and (' serve ' not in argv or str(HOME) not in argv):raise RuntimeError('web_home_mismatch')
        if name=='daemon' and str(HOME) not in argv:raise RuntimeError('daemon_home_mismatch')
        return pid
    except FileNotFoundError:return None
def spawn(name,argv,env):
    if process(name):return
    with (LOG/(name+'.log')).open('a') as f:
        p=subprocess.Popen(argv,env=env,cwd=ROOT,stdout=f,stderr=subprocess.STDOUT,start_new_session=True)
    (LOG/(name+'.pid')).write_text(str(p.pid))
def stop_process(name):
    pid=process(name)
    if not pid:return
    stat_path=Path('/proc')/str(pid)/'stat'
    identity=stat_path.read_text().split(') ',1)[1].split()[19]
    os.killpg(pid,signal.SIGTERM)
    end=time.monotonic()+20
    while time.monotonic()<end:
        try:fields=stat_path.read_text().split(') ',1)[1].split()
        except FileNotFoundError:return
        if fields[0]=='Z' or fields[19]!=identity:return
        time.sleep(.2)
    if process(name):os.killpg(pid,signal.SIGKILL)
def docker(*args):return subprocess.run(['docker',*args],capture_output=True,text=True,check=True)
def container():
    r=subprocess.run(['docker','inspect',NAME],capture_output=True,text=True)
    if r.returncode:return None
    c=json.loads(r.stdout)[0]
    if c['Config']['Labels'].get('project')!=LABEL:raise RuntimeError('container_ownership_mismatch')
    return c
def process_mode(pid):
    values=Path('/proc',str(pid),'environ').read_bytes().split(b'\0')
    return next((item.split(b'=',1)[1].decode() for item in values if item.startswith(b'HOSPITAL_MODE=')),'mhs-v2')

def process_admission(pid):
    values=Path('/proc',str(pid),'environ').read_bytes().split(b'\0')
    return next((item.split(b'=',1)[1].decode() for item in values if item.startswith(b'HOSPITAL_INFER_ADMISSION=')),None)

def check_runtime_mode():
    for name in ['sim','bridge','web','daemon','workbench']:
        pid=process(name)
        if pid and process_mode(pid)!=MODE:raise RuntimeError('runtime_mode_change_requires_stop:'+name)
    c=container()
    if c and c['State']['Running']:
        configured=dict(item.split('=',1) for item in c['Config'].get('Env',[]) if '=' in item)
        if configured.get('HOSPITAL_MODE','mhs-v2')!=MODE:raise RuntimeError('runtime_mode_change_requires_stop:container')

def api(path='/api/state',args=None):
    req=urllib.request.Request('http://127.0.0.1:4188'+path,data=None if args is None else json.dumps(args).encode(),headers={'Content-Type':'application/json'})
    try:
        with urllib.request.urlopen(req,timeout=60 if path=='/api/mhs/routes' else 5) as f:d=json.load(f)
    except urllib.error.HTTPError as e:
        detail=json.load(e);raise RuntimeError(detail.get('error','HTTP_'+str(e.code))) from e
    if d.get('ok') is False:raise RuntimeError(d.get('error'))
    return d.get('result',d)
def check_legacy_stopped():
    r=subprocess.run(['python3',str(ROOT/'scripts/control.py'),'status'],capture_output=True,text=True,check=True)
    s=json.loads(r.stdout)
    if s['batch']['pid'] or s['qualification']['pid'] or legacy.live_sim():raise RuntimeError('legacy_experiment_or_sim_active')
    c=legacy.own_container()
    if c and c['State']['Running']:raise RuntimeError('legacy_container_active')
    p=ROOT/'runs/agent-live.pid'
    if p.exists() and Path('/proc',p.read_text().strip()).exists():raise RuntimeError('legacy_agent_active')
def wait_ready():
    end=time.monotonic()+180
    while time.monotonic()<end:
        try:
            s=api()
            marker=ROOT/'logs/nav-bootstrap.json';start_marker=ROOT/'logs/nav-bootstrap-start.json'
            bootstrap=json.loads(marker.read_text()) if marker.exists() else {}
            expected=json.loads(start_marker.read_text()) if start_marker.exists() else {}
            crowd=s.get('crowd',{})
            ward=s.get('smart_ward',{})
            smart_ready=MODE!=SMART_MODE or ward.get('scene_fresh') and len(ward.get('devices',{}))==8
            if s['navigation_ready'] and s['pose_fresh'] and s['physical_pose'] and crowd.get('fresh') and len(crowd.get('people',[]))==4 and bootstrap.get('phase')=='passed' and bootstrap.get('id')==expected.get('id') and smart_ready:return s
        except Exception:pass
        time.sleep(.5)
    raise RuntimeError('mhs_navigation_not_ready')

def phase(message):print('WORKBENCH_PHASE '+message,flush=True)

def start_daemon():
    discovery=HOME/'data/daemon/discovery.json'
    if discovery.exists():
        d=json.loads(discovery.read_text())
        if d.get('ready') and Path('/proc',str(d.get('owner',{}).get('pid'))).exists():
            if d.get('web',{}).get('url')!='ws://127.0.0.1:4195':raise RuntimeError('restart_project_daemon_for_fixed_port')
            owner=int(d['owner']['pid']);q=Path('/proc')/str(owner)
            argv=q.joinpath('cmdline').read_bytes().replace(b'\0',b' ').decode()
            if q.joinpath('cwd').resolve()!=ROOT or str(RUNTIME/'daemon.mjs') not in argv or str(HOME) not in argv:raise RuntimeError('daemon_ownership_mismatch')
            if process_mode(owner)!=MODE:raise RuntimeError('runtime_mode_change_requires_stop:daemon')
            if MODE==SMART_MODE and process_admission(owner)!='smart-ward-v1.89':raise RuntimeError('model_admission_version_requires_project_daemon_restart')
            (LOG/'daemon.pid').write_text(str(owner))
            return
    spawn('daemon',['node',str(RUNTIME/'daemon.mjs'),'--home',str(HOME),'--profile','local-dev','--workspace',str(ROOT),
        '--local-web-addr','127.0.0.1:4195','--local-web-origin','http://127.0.0.1:4189'],ENV)
    end=time.monotonic()+45
    while time.monotonic()<end:
        if not process('daemon'):raise RuntimeError('project_daemon_exited')
        try:
            d=json.loads(discovery.read_text())
            if d.get('ready') and d.get('owner',{}).get('pid')==process('daemon') and d.get('web',{}).get('url')=='ws://127.0.0.1:4195':return
        except (FileNotFoundError,ValueError):pass
        time.sleep(.2)
    raise RuntimeError('project_daemon_start_timeout')

def wait_web_ready():
    end=time.monotonic()+45
    while time.monotonic()<end:
        if not process('web'):raise RuntimeError('project_web_exited')
        try:
            with urllib.request.urlopen('http://127.0.0.1:4189/',timeout=2) as response:
                page=response.read(1048576).decode()
            if re.search(r'data-ws="ws://127\.0\.0\.1:4195(?:/)?"',page):return
        except (OSError,ValueError):pass
        time.sleep(.3)
    raise RuntimeError('project_web_http_not_ready')
def start():
    phase('检查项目归属与旧实验停止状态')
    check_legacy_stopped()
    check_runtime_mode();DATA_DIR.mkdir(parents=True,exist_ok=True)
    from .asset_cache import verify_people_assets
    phase('核验项目人物动画缓存完整性')
    verify_people_assets(ROOT)
    if not (RUNTIME/'agnes.mjs').exists() or not (HOME/'profiles/local-dev/profile.yaml').exists():raise RuntimeError('run_mhs_setup_first')
    env=dict(ENV,ROS_DOMAIN_ID='73',ROS_DISTRO='humble',RMW_IMPLEMENTATION='rmw_fastrtps_cpp',
        FASTRTPS_DEFAULT_PROFILES_FILE=str(ROOT/'configs/fastdds.xml'),FASTDDS_DEFAULT_PROFILES_FILE=str(ROOT/'configs/fastdds.xml'),
        LD_LIBRARY_PATH=str(legacy.SIM/'exts/isaacsim.ros2.core/humble/lib')+':'+os.environ.get('LD_LIBRARY_PATH',''))
    if not process('sim'):
        phase('启动 Isaac Sim，等待真实位姿和首帧')
        soft,hard=resource.getrlimit(resource.RLIMIT_NOFILE)
        resource.setrlimit(resource.RLIMIT_NOFILE,(min(65535,hard) if hard!=resource.RLIM_INFINITY else 65535,hard))
        started=time.time();spawn('sim',[str(legacy.SIM/'python.sh'),str(ROOT/'mhs/hospital_sim.py')],env)
        (LOG/'sim-start.json').write_text(json.dumps({'wall_time':started,'pid':process('sim')}))
        end=time.monotonic()+900
        while time.monotonic()<end:
            if not process('sim'):raise RuntimeError('mhs_sim_exited')
            p=ROOT/'runs/physical-pose.json'
            try:
                truth=json.loads(p.read_text())
                if truth['wall_time']>started and time.time()-truth['wall_time']<5:break
            except Exception:pass
            time.sleep(1)
        else:raise RuntimeError('mhs_sim_first_frame_timeout')
    if MODE==SMART_MODE:
        phase('核验智慧病区柜体地图已生成')
        if not MAP_YAML.is_file() or not MAP_YAML.with_suffix('.png').is_file():raise RuntimeError('smart_ward_scene_map_not_generated')
    phase('启动项目导航与业务服务');c=container()
    if c and not c['State']['Running']:docker('rm',NAME);c=None
    if not c:
        docker('run','-d','--name',NAME,'--label','project='+LABEL,'--network','host','--ipc','host','--user',str(os.getuid())+':'+str(os.getgid()),
            '-e','HOME=/tmp','-e','FASTRTPS_DEFAULT_PROFILES_FILE=/project/configs/fastdds.xml','-e','FASTDDS_DEFAULT_PROFILES_FILE=/project/configs/fastdds.xml',
            '-e','HOSPITAL_MODE='+MODE,'-e','HOSPITAL_ROOT=/project','-e','HOSPITAL_DATA_DIR=/project/runs/'+MODE,
            '-e','HOSPITAL_MAP_YAML=/project/'+MAP_YAML.relative_to(ROOT).as_posix(),
            '-v',str(ROOT)+':/project','hospital-delivery:humble-v1','sleep','infinity')
        import uuid
        ident=uuid.uuid4().hex
        (ROOT/'logs/nav-bootstrap-start.json').write_text(json.dumps({'id':ident,'wall_time':time.time()}))
        for script,name in [('mhs/nav_start.sh','nav2'),('mhs/service_start.sh','service'),('scripts/nav_bootstrap_start.sh','nav-bootstrap')]:
            docker('exec','-d','-e','HOSPITAL_BOOTSTRAP_ID='+ident,NAME,'bash','-c','exec /project/'+script+' >> /project/logs/mhs/'+name+'.log 2>&1')
    wait_ready()
    phase('导航就绪，连接 Agnes 与医院机器人')
    start_daemon()
    spawn('web',CLI+['serve','--home',str(HOME),'--profile','local-dev','--cwd',str(ROOT),'--port','4189'],ENV)
    spawn('bridge',[str(ROOT/'agent/mhs-venv/bin/python'),'-m','mhs.bridge'],ENV)
    end=time.monotonic()+30
    while time.monotonic()<end:
        if api()['motion_guard']['healthy']:break
        if not process('web') or not process('bridge'):raise RuntimeError('mhs_workbench_or_bridge_exited')
        time.sleep(.5)
    else:raise RuntimeError('mhs_hub_registration_not_ready')
    phase('等待聊天网页与固定连接可访问');wait_web_ready()
    spawn('workbench',['node',str(ROOT/'mhs/dist/workbench.mjs')],ENV)
    phase('机器人与工作台已就绪')
    status()
def stop_run_and_owner(reason,expected_run_id=None):
    """Disable persisted watch before cancelling the owning SDK conversation."""
    s=api();import uuid
    if expected_run_id is not None and s.get('run_id')!=expected_run_id:raise RuntimeError('session_run_mismatch')
    operation='lifecycle-stop-'+uuid.uuid4().hex
    api('/api/admin/stop-run',{'operation_id':operation,'expected_run_id':s['run_id'],'reason':reason})
    if s.get('controller_session') and not os.environ.get('HOSPITAL_WORKBENCH_JOB'):
        try:
            body={'action':'stop','operation_id':operation,'expected_run_id':s['run_id'],'session_id':s['controller_session']}
            request=urllib.request.Request('http://127.0.0.1:4194/api/demo/command',data=json.dumps(body).encode(),
                headers={'Content-Type':'application/json','Origin':'http://127.0.0.1:4194'})
            with urllib.request.urlopen(request,timeout=20) as response:result=json.load(response)
            if result.get('ok') is not True:raise RuntimeError('workbench_owner_cancel_failed')
        except Exception as error:
            (LOG/'shutdown-cancel-error.json').write_text(json.dumps({'reason':reason,'error':str(error),'run_id':s['run_id'],'wall_time':time.time()}))
            # The run is already locked. Terminate only the verified project daemon
            # when its SDK facade cannot cancel the owner and its durable queue.
            stop_process('daemon')
    return s

def stop(keep_workbench=False):
    c=container()
    if c and c['State']['Running']:
        try:
            # reset() has already saved the old run and created a paused new one;
            # tearing down its simulator must not operator-stop that new run.
            if keep_workbench:api('/api/mhs/stop',{'reason':'project_shutdown'})
            else:stop_run_and_owner('project_shutdown')
            if process('sim'):
                end=time.monotonic()+20
                while time.monotonic()<end:
                    if api()['stopped_confirmed']:break
                    time.sleep(.2)
                else:raise RuntimeError('physical_stop_not_confirmed')
        except Exception:
            if not keep_workbench:stop_process('workbench')
            stop_process('daemon')
            # Only this verified container and robot's namespace are addressed.
            if process('sim'):
                proof=docker('exec','-e','ROS_DOMAIN_ID=73',NAME,'bash','-c','source /opt/ros/humble/setup.bash && python3 /project/mhs/check_still_inside.py --stop')
                (LOG/'shutdown-stillness.json').write_text(proof.stdout)
    stop_process('bridge');stop_process('web')
    if not keep_workbench:stop_process('workbench')
    if (RUNTIME/'agnes.mjs').exists():
        subprocess.run(CLI+['daemon','stop','--home',str(HOME),'--workspace',str(ROOT)],env=ENV,cwd=ROOT,capture_output=True,text=True,timeout=45)
    stop_process('daemon')
    c=container()
    if c and c['State']['Running']:docker('stop','--time','15',NAME)
    stop_process('sim');status()
def reset_preflight(expected_run_id,require_stopped=True):
    """Read-only lifecycle check; the backend still authoritatively checks reset."""
    s=api()
    if s.get('run_id')!=expected_run_id:raise RuntimeError('reset_preflight_run_changed')
    if require_stopped and not s.get('stopped_confirmed'):raise RuntimeError('stop_not_confirmed')
    navigation=s.get('navigation') or {}
    if navigation.get('status')=='unknown' or require_stopped and navigation.get('status') in {'pending','running','canceling'}:
        raise RuntimeError('prior_navigation_stop_requires_reconciliation')
    if MODE==SMART_MODE:
        ward=s.get('smart_ward') or {}
        if ward.get('navigation_recovery_required'):raise RuntimeError('prior_navigation_stop_requires_reconciliation')
        if any(item.get('status') in {'pending','unknown'} and item.get('command',{}).get('kind')=='handoff'
               for item in ward.get('operations',{}).values()):
            raise RuntimeError('pending_handoff_must_be_reconciled_before_reset')
        qualification=ward.get('fault_qualification')
        if qualification:
            if qualification.get('active'):raise RuntimeError('fault_qualification_must_end_before_reset')
            try:restored=json.loads((DATA_DIR/'scene/fault-state.json').read_text(encoding='utf-8'))
            except (OSError,ValueError):raise RuntimeError('fault_qualification_restoration_not_confirmed_before_reset')
            scene=ward.get('scene_sample') or {};ended=qualification.get('ended_wall')
            sequence,ended_sequence=scene.get('seq'),qualification.get('ended_scene_seq')
            if (not isinstance(restored,dict)
                    or any(restored.get(key)!=qualification.get(key) for key in ('run_id','epoch','qualification_id','session_key'))
                    or restored.get('active') is not False or restored.get('paused') or restored.get('holds')
                    or (scene.get('run_id'),scene.get('epoch'))!=(s['run_id'],ward.get('epoch'))
                    or isinstance(ended,bool) or not isinstance(ended,(int,float)) or not math.isfinite(ended)
                    or not ward.get('scene_fresh') or scene.get('wall_time',0)<=ended
                    or isinstance(sequence,bool) or not isinstance(sequence,int) or sequence<1
                    or isinstance(ended_sequence,int) and sequence<=ended_sequence
                    or not s.get('pose_fresh') or not s.get('physical_pose') or not s.get('crowd',{}).get('fresh')):
                raise RuntimeError('fault_qualification_restoration_not_confirmed_before_reset')
    return s

def restore_reset_services(expected_run_id,previously_running):
    """Restore only previously running project adapters after confirmed refusal."""
    evidence={'expected_run_id':expected_run_id,'restoration_requested':[],'errors':[]}
    try:
        s=api();evidence['observed_run_id']=s.get('run_id')
        if s.get('run_id')!=expected_run_id:
            raise RuntimeError('reset_outcome_unknown_or_run_changed_no_service_restore')
        commands={'web':CLI+['serve','--home',str(HOME),'--profile','local-dev','--cwd',str(ROOT),'--port','4189'],
                  'bridge':[str(ROOT/'agent/mhs-venv/bin/python'),'-m','mhs.bridge']}
        for name in ['web','bridge']:
            if previously_running.get(name):
                try:spawn(name,commands[name],ENV);evidence['restoration_requested'].append(name)
                except Exception as error:evidence['errors'].append(name+':'+str(error))
    except Exception as error:evidence['errors'].append(str(error))
    return evidence

def reset():
    phase('确认真实停止并保存旧运行')
    initial=api()
    # Reject known reconciliation blockers before cancelling an owner or its
    # daemon. Normal active navigation can still proceed to the stop phase.
    reset_preflight(initial['run_id'],require_stopped=False)
    original=stop_run_and_owner('scene_reset',expected_run_id=initial['run_id'])
    end=time.monotonic()+20
    while time.monotonic()<end:
        if api()['stopped_confirmed']:break
        time.sleep(.2)
    else:raise RuntimeError('stop_not_confirmed')
    # Reject while the old services and session binding still exist. This is a
    # read-only preflight, not an atomic guarantee against a later state change.
    reset_preflight(original['run_id'])
    previously_running={name:bool(process(name)) for name in ['bridge','web']}
    try:
        stop_process('bridge');stop_process('web')
        result=api('/api/admin/reset',{})
    except Exception as error:
        recovery=restore_reset_services(original['run_id'],previously_running)
        recovery.update(error=str(error),wall_time=time.time())
        try:(LOG/('reset-recovery-'+str(time.time_ns())+'.json')).write_text(json.dumps(recovery,ensure_ascii=False))
        except OSError as failure:print('Reset recovery evidence unavailable: '+str(failure),flush=True)
        raise
    from .session_scope import revoke_previous
    revoke_previous()
    # Recreate the scene at its actual initial depot pose after preserving the
    # old business run. No teleport or restoration of old navigation goals.
    phase('重建仿真场景');stop(keep_workbench=True);start()
    print(json.dumps({'new_run':result['run_id'],'note':'Open a new Agnes conversation for the new run'},ensure_ascii=False))
def status():
    c=container();d={'processes':{n:process(n) for n in ['sim','web','bridge','daemon','workbench']},'container_running':bool(c and c['State']['Running']),
        'workbench':'http://127.0.0.1:4194/','chat':'http://127.0.0.1:4189/','devices':'http://127.0.0.1:4192/',
        'selected_mode':MODE,'data_dir':str(DATA_DIR),'map_yaml':str(MAP_YAML)}
    try:
        s=api();d['runtime']={k:s.get(k) for k in ['run_id','sim_time','pose_fresh','physical_pose','navigation_ready','navigation','motion_guard','stopped_confirmed','mhs_budget']}
        crowd=s.get('crowd',{});d['runtime']['crowd']={k:crowd.get(k) for k in ['fresh','seed','mode','source','wall_time']};d['runtime']['crowd']['people_count']=len(crowd.get('people',[]))
        d['runtime']['orders']={k:{x:v.get(x) for x in ['status','loaded','dropoff_station','priority']} for k,v in s['orders'].items()}
        if s.get('smart_ward'):
            ward=s['smart_ward'];d['runtime']['smart_ward']={k:ward.get(k) for k in ['watch','scene_fresh','summary','recovery_required']}
            d['runtime']['smart_ward']['device_count']=len(ward.get('devices',{}))
            d['runtime']['smart_ward']['fresh_device_count']=sum(v.get('fresh') is True for v in ward.get('devices',{}).values())
    except Exception as e:d['api_error']=str(e)
    print(json.dumps(d,ensure_ascii=False,indent=2))
if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('action',choices=['start','stop','status','reset']);args=p.parse_args()
    globals()[args.action]()
