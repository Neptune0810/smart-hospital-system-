"""Reload only this project's workbench, daemon and device adapter."""
import json,subprocess
from .manage import ROOT,HOME,RUNTIME,ENV,CLI,MODE,SMART_MODE,stop_process,spawn,api,status,start_daemon,stop_run_and_owner,wait_web_ready
import time

def main():
    if MODE==SMART_MODE:stop_run_and_owner('workbench_reload')
    else:api('/api/mhs/stop',{'reason':'workbench_reload'})
    deadline=time.monotonic()+20
    while time.monotonic()<deadline:
        if api()['stopped_confirmed']:break
        time.sleep(.2)
    else:raise RuntimeError('stop_not_confirmed')
    stop_process('workbench');stop_process('bridge');stop_process('web')
    subprocess.run(CLI+['daemon','stop','--home',str(HOME),'--workspace',str(ROOT)],env=ENV,cwd=ROOT,check=True,timeout=45)
    profile=HOME/'profiles/local-dev/profile.yaml'
    config=json.loads(profile.read_text())
    config['presets']={'default':'hospital-mhs','allowed':['hospital-mhs']}
    config['packages']=[{'id':'hospital-mhs-policy','source':'file:./mhs/policy','enabled':True},
        {'id':'@agnes/mhs','source':'file:./agent/agh-source-mhs/packages/mhs/plugin','enabled':True}]
    profile.write_text(json.dumps(config,indent=2))
    start_daemon()
    spawn('web',CLI+['serve','--home',str(HOME),'--profile','local-dev','--cwd',str(ROOT),'--port','4189'],ENV)
    spawn('bridge',[str(ROOT/'agent/mhs-venv/bin/python'),'-m','mhs.bridge'],ENV)
    wait_web_ready();spawn('workbench',['node',str(ROOT/'mhs/dist/workbench.mjs')],ENV)
    if MODE==SMART_MODE:print('Smart ward reloaded with this run stopped; use a new round before enabling watch.')
    status()

if __name__=='__main__':main()
