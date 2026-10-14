"""Stop only an explicitly identified cache process in this project."""
import json,os,signal,sys,time
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
pid=int(sys.argv[1]);proc=Path('/proc')/str(pid)
argv=proc.joinpath('cmdline').read_bytes().replace(b'\0',b' ').decode()
if proc.joinpath('cwd').resolve()!=ROOT or 'python3 -m mhs.cache_people_assets ' not in argv+' ':
    raise RuntimeError('cache_process_ownership_mismatch')
folder=ROOT/'runs/mhs-v2/asset-downloads';folder.mkdir(parents=True,exist_ok=True)
files=list((ROOT/'assets/people-v2').rglob('*'))
record={'pid':pid,'argv':argv,'cwd':str(ROOT),'wall_time':time.time(),'action':'stop_download_for_worker_increase','existing_files':sum(p.is_file() for p in files),'existing_bytes':sum(p.stat().st_size for p in files if p.is_file())}
(folder/(str(pid)+'-stop.json')).write_text(json.dumps(record,indent=2));os.kill(pid,signal.SIGTERM)
print(json.dumps(record))
