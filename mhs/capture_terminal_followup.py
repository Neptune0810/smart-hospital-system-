"""Extend only observation of a native terminal turn; no messages or commands."""
import argparse,json,time,subprocess
from pathlib import Path
from .manage import ROOT,api,ENV


def main():
    p=argparse.ArgumentParser();p.add_argument('protocol');args=p.parse_args()
    protocol_path=(ROOT/args.protocol).resolve()
    if ROOT/'runs/mhs-v2/acceptance' not in protocol_path.parents:raise ValueError('outside_acceptance')
    protocol=json.loads(protocol_path.read_text());run_id=protocol['run_id']
    expected=json.loads((ROOT/'logs/mhs/workbench-observer-current.json').read_text())['directory']
    attempt=Path(expected).resolve()
    if ROOT/'runs/mhs-v2/acceptance' not in attempt.parents:raise ValueError('outside_acceptance')
    folder=attempt/'terminal-followup';folder.mkdir(exist_ok=False)
    (folder/'observation-protocol.json').write_text(json.dumps({'source_attempt':str(attempt),'source_protocol':str(protocol_path),'run_id':run_id,'scope':'Read-only extension of the same actual webpage native terminal turn; no new human task, no retry, no result overwrite','reason':'Original observer report lacks mhs_outcome and may export the conversation before the native failure turn ends','after_terminal_wall_seconds':60,'created_wall':time.time()},indent=2))
    end=time.monotonic()+1200
    while time.monotonic()<end:
        s=api()
        if s['run_id']!=run_id:raise RuntimeError('run_changed')
        if s.get('mhs_outcome')=='incomplete':break
        time.sleep(.5)
    else:raise RuntimeError('no_terminal_outcome_before_observation_limit')
    (folder/'immediate-state.json').write_text(json.dumps(s,ensure_ascii=False,indent=2))
    deadline=time.monotonic()+60
    while time.monotonic()<deadline:
        if api()['run_id']!=run_id:raise RuntimeError('run_changed')
        time.sleep(.5)
    report=api('/api/report');report['acceptance_input']='actual webpage, same original task; read-only terminal observation extended'
    (folder/'report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2))
    (folder/'final-state.json').write_text(json.dumps(api(),ensure_ascii=False,indent=2))
    python=str(ROOT/'agent/mhs-venv/bin/python');session=s['controller_session']
    subprocess.run([python,'-m','mhs.export_session',session,str(folder)],cwd=ROOT,env=ENV,check=True,capture_output=True)
    subprocess.run([python,'-m','mhs.audit_session',str(folder),'--since-wall',str(protocol['started_wall'])],cwd=ROOT,env=ENV,check=True,capture_output=True)
    print(json.dumps({'directory':str(folder),'original_attempt_unchanged':True}),flush=True)


if __name__=='__main__':main()
