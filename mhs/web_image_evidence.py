"""Prepare/finalize evidence around an actual browser image question; no prompts."""
import argparse,json,time,subprocess
from pathlib import Path
from .manage import ROOT,ENV,api

PROMPT='现在看到什么？请读一下当前第一人称画面，简单描述，不要建单或移动。'
def main():
    parser=argparse.ArgumentParser();parser.add_argument('action',choices=['prepare','finish']);parser.add_argument('directory',nargs='?');args=parser.parse_args()
    if args.action=='prepare':
        state=api()
        if not state['stopped_confirmed'] or state['orders']:raise RuntimeError('fresh_stationary_empty_run_required')
        directory=ROOT/'runs/mhs-v2/acceptance'/('web-image-'+time.strftime('%Y%m%d-%H%M%S'));directory.mkdir(exist_ok=False)
        (directory/'protocol.json').write_text(json.dumps({'input_source':'actual browser composer','prompt':PROMPT,'run_id':state['run_id'],'session':state['controller_session'],'started_wall':time.time(),'requires_actual_provider_image_bytes':True},ensure_ascii=False,indent=2))
        (directory/'before-state.json').write_text(json.dumps(state,ensure_ascii=False,indent=2))
        print('EVIDENCE '+str(directory));return
    directory=Path(args.directory).resolve()
    if ROOT/'runs/mhs-v2/acceptance' not in directory.parents:raise ValueError('outside_acceptance')
    protocol=json.loads((directory/'protocol.json').read_text());before=json.loads((directory/'before-state.json').read_text());after=api()
    if after['run_id']!=protocol['run_id']:raise RuntimeError('run_changed')
    python=str(ROOT/'agent/mhs-venv/bin/python')
    for module,extra in [('mhs.export_session',[protocol['session'],str(directory)]),('mhs.audit_session',[str(directory),'--since-wall',str(protocol['started_wall'])]),('mhs.verify_image_evidence',[str(directory)])]:
        subprocess.run([python,'-m',module,*extra],cwd=ROOT,env=ENV,check=True,capture_output=True)
    image=json.loads((directory/'image-proof.json').read_text());audit=json.loads((directory/'session-audit.json').read_text())
    report={'provider_image_bytes_verified':image['pass'],'orders_unchanged':before['orders']==after['orders'],'navigation_tasks_unchanged':set(before['navigation_tasks'])==set(after['navigation_tasks']),'final_stopped':after['stopped_confirmed'],'read_device_calls':audit['tool_calls'].get('read_device',0),'description_matches_actual_image':None,'capture_freshness_verified':None,'scope':'Transport proof. Human verification of actual image, read result timestamp and response remains required.'}
    (directory/'report.json').write_text(json.dumps(report,indent=2));(directory/'after-state.json').write_text(json.dumps(after,ensure_ascii=False,indent=2));print(json.dumps(dict(directory=str(directory),**report)))

if __name__=='__main__':main()
