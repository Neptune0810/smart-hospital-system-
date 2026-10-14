"""Run the pinned official checker against a separately launched real adapter."""
import subprocess,os,time,json
from .manage import ROOT,ENV,api,stop_process,spawn,process
def main():
    s=api()
    if any(o['status'] not in {'delivered','failed'} for o in s['orders'].values()):raise RuntimeError('active_delivery_run')
    api('/api/mhs/stop',{'reason':'protocol_acceptance'})
    if not api()['stopped_confirmed']:raise RuntimeError('physical_stop_not_confirmed')
    directory=ROOT/'runs/mhs-v2/acceptance'/('conformance-'+time.strftime('%Y%m%d-%H%M%S'));directory.mkdir(parents=True)
    python=str(ROOT/'agent/mhs-venv/bin/python');stop_process('bridge')
    hub=adapter=None
    try:
        with (directory/'checker.log').open('w') as f:
            hub=subprocess.Popen([python,'-m','agnes_mhs.check','--host','127.0.0.1','--port','8800',
                '--profile','core,motion,perception,streaming,maps','--wait','30','--replace-wait','10','--report',str(directory/'official-report.json')],cwd=ROOT,stdout=f,stderr=subprocess.STDOUT)
        time.sleep(1)
        with (directory/'adapter.log').open('w') as f:
            adapter=subprocess.Popen([python,'-m','mhs.bridge'],env=dict(ENV,HOSPITAL_MHS_HUB='ws://127.0.0.1:8800'),cwd=ROOT,stdout=f,stderr=subprocess.STDOUT)
        code=hub.wait(timeout=600)
        (directory/'execution.json').write_text(json.dumps({'checker_exit':code,'command':'official pinned agnes_mhs.check; no scenario overrides','directory':str(directory)},indent=2))
    finally:
        api('/api/mhs/stop',{'reason':'protocol_acceptance_end'})
        for child in [adapter,hub]:
            if child and child.poll() is None:
                child.terminate()
                try:child.wait(timeout=15)
                except subprocess.TimeoutExpired:child.kill();child.wait()
        spawn('bridge',[python,'-m','mhs.bridge'],ENV)
        print('EVIDENCE',directory,flush=True)
if __name__=='__main__':main()
