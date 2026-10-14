#!/usr/bin/env python3
"""Exclusive 72-run batch with source freeze; summarize incrementally."""
import os,sys,json,time,hashlib,subprocess,argparse
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
PID=ROOT/'logs/batch.pid'
def freeze():
    groups=['scripts','service','sim','launch','maps','configs','agent/hospital-policy']
    files=[ROOT/'Dockerfile',ROOT/'agent/instructions.md',ROOT/'agent/profile.template.json']
    for group in groups:
        files += [f for f in (ROOT/group).rglob('*') if f.is_file() and '__pycache__' not in f.parts and f.suffix!='.pyc']
    hashes={str(f.relative_to(ROOT)):hashlib.sha256(f.read_bytes()).hexdigest() for f in sorted(set(files))}
    path=ROOT/'runs/experiment-v6/code-freeze.json';path.parent.mkdir(exist_ok=True)
    if path.exists():
        previous=json.loads(path.read_text())
        if previous['files']!=hashes:raise RuntimeError('Frozen implementation changed; preserve old results and create a new batch version')
    else:path.write_text(json.dumps({'wall_time':time.time(),'files':hashes,'model':'agnes-3.0-flash','agh_runtime_hashes':json.loads((ROOT/'evidence/agh-runtime-hashes.json').read_text())},indent=2))
def main():
    ap=argparse.ArgumentParser();ap.add_argument('--require-gates',action='store_true');a=ap.parse_args()
    if a.require_gates:
        proof=json.loads((ROOT/'evidence/acceptance.json').read_text())
        if any(proof[k]['status']!='passed' for k in ['navigation','delivery','agent','recovery']):raise RuntimeError('Acceptance gates are not all passed')
    # flock prevents two batch controllers from sharing the robot.
    import fcntl
    lock=(ROOT/'runs/experiment.lock').open('a')
    fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
    PID.write_text(str(os.getpid()));freeze()
    from experiment import run,put
    from report import summarize
    protocol=json.loads((ROOT/'configs/experiment.json').read_text())
    batch=ROOT/'runs/experiment-v6'
    put(batch/'protocol.json',dict(protocol=protocol,sha256=hashlib.sha256((ROOT/'configs/experiment.json').read_bytes()).hexdigest()))
    for case in protocol['cases']:
        for seed in protocol['seeds']:
            for method in protocol['methods']:
                d=batch/(case['id']+'_'+str(seed)+'_'+method)
                if (d/'report.json').exists():continue
                put(batch/'current.json',dict(phase='running',case=case['id'],seed=seed,method=method,wall_time=time.time()))
                print('START',case['id'],seed,method,flush=True)
                try:r=run(case,seed,method,d)
                except Exception as e:
                    put(d/'report.json',dict(case_id=case['id'],seed=seed,method=method,delivered=None,error=str(e),status='NA_preflight_or_model_setup'))
                    summarize(ROOT);raise
                print('DONE',case['id'],seed,method,r['delivered'],r['total'],r['error'],flush=True)
                summary=summarize(ROOT)
                put(batch/'current.json',dict(phase='between_runs',completed=summary['actual_runs'],wall_time=time.time()))
    summary=summarize(ROOT)
    put(batch/'current.json',dict(phase='finished',completed=summary['actual_runs'],missing=summary['missing'],wall_time=time.time()))
    # Return to a fresh depot state, then leave the application available.
    from experiment import scene_reset
    import manage
    scene_reset();manage.start()
    subprocess.run([sys.executable,str(ROOT/'scripts/package_delivery.py')],cwd=ROOT,check=True)
    print('BATCH FINISHED',json.dumps(summary),flush=True)
if __name__=='__main__':
    try:main()
    except Exception as e:
        from experiment import put
        current=ROOT/'runs/experiment-v6/current.json'
        state=json.loads(current.read_text()) if current.exists() else {}
        state.update(phase='failed',error=str(e),wall_time=time.time());put(current,state)
        raise
