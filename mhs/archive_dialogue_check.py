"""Preserve this named UI acceptance failure before creating a fresh run."""
import json,os,signal,subprocess,time
from pathlib import Path
from .manage import ROOT,api
state=api();api('/api/mhs/stop',{'reason':'dialogue_acceptance_failed'})
deadline=time.monotonic()+12
while not api()['stopped_confirmed']:
    if time.monotonic()>deadline:raise RuntimeError('physical_stop_not_confirmed')
    time.sleep(.2)
target=ROOT/'runs/mhs-v2/acceptance/camera-dialogue-20261011'
(target/'failed-run.json').write_text(json.dumps({'pass':False,'reason':'hook_timeout_before_delivery','state':state,'report':api('/api/report')},ensure_ascii=False,indent=2))
expected=['python3','-m','mhs.observe_web','natural-task-camera-wide']
for entry in Path('/proc').iterdir():
    if not entry.name.isdigit():continue
    try:
        args=[v.decode() for v in (entry/'cmdline').read_bytes().split(b'\0') if v]
        if args==expected and (entry/'cwd').resolve()==ROOT:os.kill(int(entry.name),signal.SIGTERM)
    except (FileNotFoundError,ProcessLookupError):pass
print('Failed acceptance and actual stop preserved; old run remains archived by normal reset.')
