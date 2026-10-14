"""Reload only the owned HTTP gateway, after a completed lifecycle job."""
import subprocess,sys
from .manage import ROOT,ENV,stop_process,spawn,api
from .workbench_check import facade

def main():
    state=api()
    if not state['stopped_confirmed'] or any(o['status'] not in {'delivered','failed'} for o in state['orders'].values()):raise RuntimeError('idle_stopped_robot_required')
    code,status=facade('/api/demo/status')
    if code!=200 or (status.get('job') or {}).get('status') in {'resetting','stopping'}:raise RuntimeError('lifecycle_job_not_finished')
    subprocess.run([sys.executable,'-m','mhs.build_workbench','--gateway-only'],cwd=ROOT,env=ENV,check=True)
    stop_process('workbench')
    spawn('workbench',['node',str(ROOT/'mhs/dist/workbench.mjs')],ENV)
    print('Project gateway reloaded; simulation, Nav2, daemon and device adapter retained.')

if __name__=='__main__':main()
