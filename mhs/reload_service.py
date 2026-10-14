"""Reload this project's API inside its verified container, retaining Nav2/scene."""
import subprocess,time,sys
from .manage import ROOT,NAME,ENV,container,api,stop_process,spawn,docker
def main():
    if not container()['State']['Running']:raise RuntimeError('project_container_stopped')
    try:
        api('/api/mhs/stop',{'reason':'service_reload'})
        if not api()['stopped_confirmed']:raise RuntimeError('physical_stop_not_confirmed')
    except Exception:
        if '--recover-unavailable-api' not in sys.argv:raise
        evidence=docker('exec','-e','ROS_DOMAIN_ID=73',NAME,'bash','-c','source /opt/ros/humble/setup.bash && python3 /project/mhs/check_still_inside.py')
        (ROOT/'logs/mhs/API-recovery-stillness.json').write_text(evidence.stdout)
    stop_process('bridge')
    code="""import os,signal,time
from pathlib import Path
found=[]
for q in Path('/proc').iterdir():
 if not q.name.isdigit():continue
 try:
  a=q.joinpath('cmdline').read_bytes().split(b'\\0')
  if a[1:3]==[b'-m',b'mhs.api']:
   if q.joinpath('cwd').resolve()!=Path('/project'):raise RuntimeError('API_cwd_mismatch')
   found.append(int(q.name))
 except FileNotFoundError:pass
if len(found)>1:print('verified_duplicate_project_APIs',found)
for pid in found:
 q=Path('/proc')/str(pid)
 identity=q.joinpath('stat').read_text().split(') ',1)[1].split()[19]
 os.kill(pid,signal.SIGTERM)
 end=time.monotonic()+5
 while q.exists() and time.monotonic()<end:
  if q.joinpath('stat').read_text().split(') ',1)[1].startswith('Z '):break
  time.sleep(.1)
 if q.exists():
  fields=q.joinpath('stat').read_text().split(') ',1)[1].split()
  if fields[0]!='Z':
   if fields[19]!=identity or q.joinpath('cwd').resolve()!=Path('/project') or q.joinpath('cmdline').read_bytes().split(b'\\0')[1:3]!=[b'-m',b'mhs.api']:raise RuntimeError('API_identity_changed')
   os.kill(pid,signal.SIGKILL)
time.sleep(.5)
print('stopped_container_API',found)
"""
    terminated=docker('exec',NAME,'python3','-c',code)
    (ROOT/'logs/mhs/API-reload-processes.log').write_text(terminated.stdout+terminated.stderr)
    docker('exec','-d',NAME,'bash','-c','exec /project/mhs/service_start.sh >> /project/logs/mhs/service.log 2>&1')
    end=time.monotonic()+30
    while time.monotonic()<end:
        try:
            s=api()
            if s['pose_fresh'] and s['navigation_ready'] and s['stopped_confirmed']:break
        except Exception:pass
        time.sleep(.2)
    else:raise RuntimeError('reloaded_API_not_ready')
    spawn('bridge',[str(ROOT/'agent/mhs-venv/bin/python'),'-m','mhs.bridge'],ENV)
    print('reloaded_project_service')
if __name__=='__main__':main()
