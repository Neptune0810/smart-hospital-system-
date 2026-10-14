#!/usr/bin/env python3
import argparse,json,os,signal,subprocess,time,urllib.request,resource,uuid
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]; SIM=Path('/mnt/sda/omni-yezh/isaac-sim-6.0.1')
NAME='hospital-delivery-runtime'; LABEL='hospital-delivery-agent'
def docker(*args,check=True):return subprocess.run(['docker',*args],check=check,capture_output=True,text=True)
def own_container():
    r=docker('inspect',NAME,check=False)
    if r.returncode:return None
    obj=json.loads(r.stdout)[0]
    if obj['Config']['Labels'].get('project')!=LABEL:raise RuntimeError('container_name_owned_by_other_project')
    return obj
def live_sim():
    p=ROOT/'logs/sim.pid'
    if not p.exists():return None
    pid=int(p.read_text())
    try:
        cmd=Path('/proc',str(pid),'cmdline').read_bytes()
        if str(ROOT/'sim/hospital.py').encode() in cmd:return pid
    except FileNotFoundError:pass
    return None
def start_sim():
    if live_sim():return
    env=os.environ.copy()
    env.update(ROS_DOMAIN_ID='73',ROS_DISTRO='humble',RMW_IMPLEMENTATION='rmw_fastrtps_cpp',FASTRTPS_DEFAULT_PROFILES_FILE=str(ROOT/'configs/fastdds.xml'),FASTDDS_DEFAULT_PROFILES_FILE=str(ROOT/'configs/fastdds.xml'),
      LD_LIBRARY_PATH=str(SIM/'exts/isaacsim.ros2.core/humble/lib')+':'+env.get('LD_LIBRARY_PATH',''))
    if os.environ.get('HOSPITAL_DOWNLOAD_PROXY'):
        env['HTTP_PROXY']=env['HTTPS_PROXY']=os.environ['HOSPITAL_DOWNLOAD_PROXY']
    log_offset=(ROOT/'logs/sim.log').stat().st_size if (ROOT/'logs/sim.log').exists() else 0
    with (ROOT/'logs/sim.log').open('a') as f:
        soft,hard=resource.getrlimit(resource.RLIMIT_NOFILE)
        desired=65535 if hard==resource.RLIM_INFINITY else min(65535,hard)
        p=subprocess.Popen([str(SIM/'python.sh'),str(ROOT/'sim/hospital.py')],env=env,stdout=f,stderr=subprocess.STDOUT,start_new_session=True,preexec_fn=lambda:resource.setrlimit(resource.RLIMIT_NOFILE,(max(soft,desired),hard)))
    (ROOT/'logs/sim.pid').write_text(str(p.pid))
    (ROOT/'logs/sim-start.json').write_text(json.dumps({'pid':p.pid,'wall_time':time.time(),'log_offset':log_offset,'nofile_soft':max(soft,desired)}))
def wait_sim_ready(timeout=900):
    deadline=time.time()+timeout
    while time.time()<deadline:
        if not live_sim():raise RuntimeError('project_sim_process_exited')
        try:
            marker=json.loads((ROOT/'logs/sim-start.json').read_text())
            pose=json.loads((ROOT/'runs/physical-pose.json').read_text())
            if pose['wall_time']>=marker['wall_time'] and time.time()-pose['wall_time']<5 and 'physics_time' in pose:return
            with (ROOT/'logs/sim.log').open() as log:
                log.seek(marker['log_offset']);current=log.read()
            if '[Fatal]' in current:raise RuntimeError('Isaac startup reported a fatal error; inspect logs/sim.log')
        except (OSError,ValueError,KeyError):pass
        time.sleep(1)
    raise TimeoutError('project_sim_first_physical_frame_timeout')
def stop_sim():
    pid=live_sim()
    if pid:
        os.killpg(pid,signal.SIGTERM)
        deadline=time.time()+20
        while live_sim() and time.time()<deadline:time.sleep(.2)
        if live_sim():os.killpg(pid,signal.SIGKILL)
def agh_control(action):
    from agh_client import CLI,ENV
    args=['--home',str(ROOT/'agent/agh-home'),'--workspace',str(ROOT)]
    if action=='start':
        status=subprocess.run(CLI+['daemon','status']+args,env=ENV,capture_output=True,text=True,timeout=10)
        if status.returncode==0:
            info=json.loads(status.stdout)
            if info.get('running') and info.get('socketReachable'):return
        with (ROOT/'logs/project-agh-current.log').open('a') as log:
            p=subprocess.Popen(['node',str(ROOT/'agent/agh-runtime/daemon.mjs')]+args,env=ENV,cwd=ROOT,stdout=log,stderr=subprocess.STDOUT,start_new_session=True)
        (ROOT/'logs/project-agh.pid').write_text(str(p.pid))
        deadline=time.time()+20
        while time.time()<deadline:
            if p.poll() is not None:raise RuntimeError('project_AGH_failed_to_start')
            result=subprocess.run(CLI+['daemon','status']+args,env=ENV,capture_output=True,text=True,timeout=5)
            if result.returncode==0 and json.loads(result.stdout).get('socketReachable'):return
            time.sleep(.5)
        raise RuntimeError('project_AGH_start_timeout')
    try:
        r=subprocess.run(CLI+['daemon',action]+args,env=ENV,capture_output=True,text=True,timeout=45)
    except subprocess.TimeoutExpired:
        if action!='stop':raise
        status=subprocess.run(CLI+['daemon','status']+args,env=ENV,capture_output=True,text=True,timeout=10)
        if status.returncode in [0,1] and json.loads(status.stdout).get('running') is False:
            with (ROOT/'logs/agh-lifecycle.jsonl').open('a') as log:log.write(json.dumps({'event':'stop_confirmed_after_cli_timeout','wall_time':time.time()})+'\n')
            return
        raise RuntimeError('project_AGH_stop_unconfirmed')
    if action=='stop':
        status=subprocess.run(CLI+['daemon','status']+args,env=ENV,capture_output=True,text=True,timeout=10)
        if status.returncode in [0,1] and json.loads(status.stdout).get('running') is False:
            with (ROOT/'logs/agh-lifecycle.jsonl').open('a') as log:log.write(json.dumps({'event':'stop_confirmed','cli_exit_code':r.returncode,'cli_stdout':r.stdout[-1200:],'wall_time':time.time()})+'\n')
            return
        raise RuntimeError('project_AGH_stop_unconfirmed:'+r.stdout[-500:])
    if r.returncode:raise RuntimeError('project_AGH_'+action+':'+r.stderr[-500:])
def wait_api():
    deadline=time.time()+180
    while time.time()<deadline:
        try:
            marker=ROOT/'logs/nav-bootstrap-start.json'
            boot=ROOT/'logs/nav-bootstrap.json'
            ready=not marker.exists()
            if marker.exists() and boot.exists():
                expected=json.loads(marker.read_text())
                observed=json.loads(boot.read_text())
                if observed.get('id')==expected['id']:
                    if observed['phase']=='failed':raise RuntimeError('nav2_bootstrap_failed:'+observed.get('error','unknown'))
                    ready=observed['phase']=='passed'
            with urllib.request.urlopen('http://127.0.0.1:4188/api/state',timeout=2) as f:s=json.load(f)
            if ready and s['navigation_ready'] and s['pose_fresh']:return
        except RuntimeError:raise
        except Exception:pass
        time.sleep(.5)
    raise RuntimeError('project_API_failed_to_start')
def start(agent_enabled=True):
    start_sim()
    wait_sim_ready()
    c=own_container()
    if c and c['State']['Running']:
        wait_api()
        agh_control('start')
        if agent_enabled:
            from agent_live import start as start_agent
            start_agent()
        return
    if c:docker('rm',NAME)
    docker('run','-d','--name',NAME,'--label','project='+LABEL,'--network','host','--ipc','host','--user',str(os.getuid())+':'+str(os.getgid()),
      '-e','HOME=/tmp','-e','FASTRTPS_DEFAULT_PROFILES_FILE=/project/configs/fastdds.xml','-e','FASTDDS_DEFAULT_PROFILES_FILE=/project/configs/fastdds.xml','-v',str(ROOT)+':/project','hospital-delivery:humble-v1','sleep','infinity')
    ident=uuid.uuid4().hex
    (ROOT/'logs/nav-bootstrap-start.json').write_text(json.dumps({'id':ident,'wall_time':time.time()}))
    for script,log in [('nav_start.sh','nav2'),('service_start.sh','service'),('nav_bootstrap_start.sh','nav-bootstrap')]:
        docker('exec','-d','-e','HOSPITAL_BOOTSTRAP_ID='+ident,NAME,'bash','-c','exec /project/scripts/'+script+' >> /project/logs/'+log+'.log 2>&1')
    (ROOT/'evidence/runtime-image.json').write_text(docker('image','inspect','hospital-delivery:humble-v1').stdout)
    wait_api()
    agh_control('start')
    if agent_enabled:
        from agent_live import start as start_agent
        start_agent()
def status():
    c=own_container()
    info={'sim_pid':live_sim(),'container_running':bool(c and c['State']['Running']),'api':None}
    try:
        with urllib.request.urlopen('http://127.0.0.1:4188/api/state',timeout=3) as f:
            s=json.load(f)
        info['api']={k:s[k] for k in ['run_id','sim_time','navigation_ready','pose_fresh','pose','navigation','at_stations']}
    except Exception as e:info['api_error']=str(e)
    bootstrap=ROOT/'logs/nav-bootstrap.json'
    if bootstrap.exists():info['nav_bootstrap']=json.loads(bootstrap.read_text())
    print(json.dumps(info,indent=2))
def stop():
    from control import stop as stop_controllers
    stop_controllers()
    from agent_live import stop as stop_agent
    stop_agent()
    agh_control('stop')
    c=own_container()
    if c and c['State']['Running']:docker('stop','--time','15',NAME)
    stop_sim()
def reset():
    stop()
    start(agent_enabled=False)
    for _ in range(30):
        try:
            req=urllib.request.Request('http://127.0.0.1:4188/api/admin/reset',data=b'{}',headers={'Content-Type':'application/json'})
            with urllib.request.urlopen(req,timeout=2) as f:f.read()
            from agent_live import start as start_agent
            start_agent()
            return
        except Exception:time.sleep(1)
    raise RuntimeError('api_reset_not_ready')
if __name__=='__main__':
    ap=argparse.ArgumentParser();ap.add_argument('action',choices=['start','stop','status','reset']);a=ap.parse_args()
    globals()[a.action]()
