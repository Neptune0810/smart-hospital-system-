"""Compact real state for acceptance; optional snapshot stays in observer directory."""
import json,sys
from .manage import ROOT,api

s=api()
if len(sys.argv)>1:
    name=sys.argv[1]
    if not name.replace('-','').isalnum():raise ValueError('invalid_snapshot_name')
    directory=json.loads((ROOT/'logs/mhs/workbench-observer-current.json').read_text())['directory']
    from pathlib import Path
    path=Path(directory)/(name+'.json')
    if ROOT/'runs/mhs-v2/acceptance' not in path.parents:raise ValueError('outside_acceptance')
    if path.exists():raise FileExistsError(path)
    path.write_text(json.dumps(s,ensure_ascii=False,indent=2))
print(json.dumps({k:s.get(k) for k in ['run_id','sim_time','pose','pose_fresh','navigation','receivers','mhs_budget','mhs_finished','stopped_confirmed','operator_stopped','delivery_plan']}|{'orders':{k:{x:v.get(x) for x in ['dropoff_station','status','loaded','receipt']} for k,v in s['orders'].items()}},ensure_ascii=False))
