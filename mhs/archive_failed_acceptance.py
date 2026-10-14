"""Preserve and end a failed implementation acceptance, not an experiment rerun."""
import json,os,signal,time,sys,re
from pathlib import Path
from .manage import ROOT,api,stop_process,CLI,ENV,HOME
import subprocess

def main():
    session,reason=sys.argv[1:]
    marker=json.loads((ROOT/'logs/mhs/observer-current.json').read_text())
    directory=Path(marker['directory']).resolve()
    label=marker['label']
    if ROOT/'runs/mhs-v2/acceptance' not in directory.parents or not label.startswith('recovery-web') or not directory.name.startswith(label+'-'):raise RuntimeError('unexpected_observer')
    stop_process('bridge');stop_process('web')
    subprocess.run(CLI+['daemon','stop','--home',str(HOME),'--workspace',str(ROOT)],env=ENV,cwd=ROOT,check=True,capture_output=True,text=True,timeout=45)
    api('/api/mhs/stop',{'reason':'acceptance_gate_failed'})
    end=time.monotonic()+12
    while time.monotonic()<end:
        if api()['stopped_confirmed']:break
        time.sleep(.2)
    else:raise RuntimeError('failed_acceptance_physical_stop_not_confirmed')
    for expected in [['python3','-m','mhs.observe_web',label],['python3','-m','mhs.inject_recovery',session]]:
        found=subprocess.run(['pgrep','-f','^'+re.escape(' '.join(expected))+'$'],capture_output=True,text=True)
        for ident in found.stdout.split():
            proc=Path('/proc')/ident
            try:
                args=[x.decode() for x in proc.joinpath('cmdline').read_bytes().split(b'\0') if x]
                if args==expected and proc.joinpath('cwd').resolve()==ROOT:os.kill(int(ident),signal.SIGTERM)
            except (FileNotFoundError,ProcessLookupError,PermissionError):pass
    state=api();report=api('/api/report')
    (directory/'failed-acceptance.json').write_text(json.dumps({'pass':False,'reason':reason,'session':session,'state':state,'report':report,'operator_stopped_after_acceptance_failure':True},ensure_ascii=False,indent=2))
    from .export_session import main as export
    sys.argv=['export_session',session,str(directory)];export()
    print(json.dumps({'directory':str(directory),'reason':reason,'stopped_confirmed':state['stopped_confirmed']}))
if __name__=='__main__':main()
