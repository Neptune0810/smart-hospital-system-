#!/usr/bin/env python3
"""Manage only this project's recorded experiment/qualification controllers."""
import os,sys,json,signal,time,subprocess,argparse
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
def owned_pid(kind):
    script='batch.py' if kind=='batch' else 'qualify.py'
    try:
        pid=int((ROOT/'logs'/(kind+'.pid')).read_text())
        proc=Path('/proc')/str(pid)
        argv=proc.joinpath('cmdline').read_bytes().decode().split('\0')
        if proc.joinpath('cwd').resolve()!=ROOT:return None
        return pid if any(a in [str(ROOT/'scripts'/script),'scripts/'+script] for a in argv) else None
    except (OSError,ValueError):return None
def stop():
    for kind in ['batch','qualification']:
        pid=owned_pid(kind)
        if not pid:continue
        os.kill(pid,signal.SIGINT)
        deadline=time.time()+140
        while owned_pid(kind) and time.time()<deadline:time.sleep(.5)
        if owned_pid(kind):raise RuntimeError(kind+' has not confirmed graceful stop')
        file=ROOT/'runs'/('experiment-v6/current.json' if kind=='batch' else 'qualification-current.json')
        if file.exists():
            state=json.loads(file.read_text());state.update(phase='stopped_by_operator',wall_time=time.time());file.write_text(json.dumps(state,indent=2))
def start():
    if owned_pid('batch') or owned_pid('qualification'):raise RuntimeError('A project controller already owns this robot')
    with (ROOT/'logs/batch.log').open('a') as f:
        p=subprocess.Popen([sys.executable,str(ROOT/'scripts/batch.py'),'--require-gates'],cwd=ROOT,stdout=f,stderr=subprocess.STDOUT,start_new_session=True)
    (ROOT/'logs/batch.pid').write_text(str(p.pid));print('Batch PID',p.pid)
def status():
    info={}
    for kind in ['batch','qualification']:
        path=ROOT/'runs'/('experiment-v6/current.json' if kind=='batch' else 'qualification-current.json')
        info[kind]={'pid':owned_pid(kind),'state':json.loads(path.read_text()) if path.exists() else None}
    print(json.dumps(info,ensure_ascii=False,indent=2))
if __name__=='__main__':
    ap=argparse.ArgumentParser();ap.add_argument('action',choices=['start','stop','status']);a=ap.parse_args();globals()[a.action]()
