"""Record a real demo run. --observe waits for actual webpage inputs; never sends tasks."""
import argparse,json,time,signal,hashlib,subprocess
from .manage import ROOT,api,ENV
from .record import Recorder
from .workbench_check import facade

def main():
    parser=argparse.ArgumentParser();parser.add_argument('scenario',choices=['normal','urgent','receiver','stop']);parser.add_argument('--observe',action='store_true');parser.add_argument('--expected-orders',type=int);args=parser.parse_args()
    directory=ROOT/'runs/mhs-v2/acceptance'/('workbench-'+args.scenario+'-'+time.strftime('%Y%m%d-%H%M%S'))
    directory.mkdir(parents=True,exist_ok=False);recorder=Recorder(directory).start()
    (ROOT/'logs/mhs/workbench-observer-current.json').write_text(json.dumps({'directory':str(directory),'scenario':args.scenario}))
    interrupted=[False]
    signal.signal(signal.SIGTERM,lambda *_:interrupted.__setitem__(0,True))
    signal.signal(signal.SIGINT,lambda *_:interrupted.__setitem__(0,True))
    initial=api();initial_ids=set(initial['orders']);started_wall=time.time()
    (directory/'protocol.json').write_text(json.dumps({'input_mode':'actual webpage' if args.observe else 'backend facade','scenario':args.scenario,'run_id':initial['run_id'],'session':initial.get('controller_session'),'expected_orders':args.expected_orders,'people_seed':initial['crowd']['seed'],'crowd_mode':initial['crowd']['mode'],'wall_limit_seconds':3620,'requires_real_agent_audit':True,'source_sha256':{p:hashlib.sha256((ROOT/p).read_bytes()).hexdigest() for p in ['mhs/workbench_acceptance.py','mhs/engine_v2.py','mhs/navigation_v2.py','mhs/bridge.py','mhs/mcp_server.py','mhs/policy/instructions.md','mhs/policy/package.json']}},indent=2))
    start=time.monotonic();begun=bool(initial_ids and not initial.get('mhs_finished'));last=None
    try:
        while time.monotonic()-start<3620 and not interrupted[0]:
            s=api()
            if s['run_id']!=initial['run_id']:raise RuntimeError('run_changed_during_web_acceptance')
            begun=begun or bool(set(s['orders'])-initial_ids)
            key=(s['latest_event_id'],s.get('operator_stopped') is not None,s.get('stopped_confirmed'))
            if key!=last:
                with (directory/'state.jsonl').open('a') as f:f.write(json.dumps({'wall_time':time.time(),'state':s},ensure_ascii=False)+'\n')
                last=key
            if begun and (s.get('mhs_finished') or s.get('mhs_outcome')=='incomplete' or (s.get('operator_stopped') and s['stopped_confirmed']) or (s.get('mhs_budget') or {}).get('stopped_reason')):break
            time.sleep(.5)
        result=api('/api/report');result['acceptance_input']='actual webpage' if args.observe else 'backend facade; not browser acceptance'
        result['task_started']=begun
        result['expected_orders']=args.expected_orders
        result['quantity_match']=None if args.expected_orders is None else result.get('total')==args.expected_orders
        result['business_pass']=bool(begun and result.get('complete_task') and result['quantity_match'] is not False and result.get('delivered')==result.get('total') and not result.get('failed'))
        if not begun:result['acceptance_status']='not_run_no_task_input'
        result['observer_interrupted']=interrupted[0]
        # Observe the original native completion turn; do not send extra prompts.
        # Keep the immediate business result distinct from the Agent's own verify.
        if result['business_pass'] or s.get('mhs_outcome')=='incomplete':
            end=time.monotonic()+45
            while time.monotonic()<end and not interrupted[0]:time.sleep(.5)
        session=initial.get('controller_session')
        if session:
            python=str(ROOT/'agent/mhs-venv/bin/python')
            subprocess.run([python,'-m','mhs.export_session',session,str(directory)],cwd=ROOT,env=ENV,check=True,capture_output=True)
            subprocess.run([python,'-m','mhs.audit_session',str(directory),'--since-wall',str(started_wall)],cwd=ROOT,env=ENV,check=True,capture_output=True)
            audit=json.loads((directory/'session-audit.json').read_text())
            verified=any(v['complete_task'] is True and v['final_stopped'] is True and v['remaining_cargo']==0 and set(v.get('order_ids') or [])==set(result.get('order_ids') or []) and v.get('segment')==result.get('segment') for v in audit['verifications'])
            result['agent_pass']=bool(args.observe and result['business_pass'] and not interrupted[0] and result.get('error_deliveries')==0 and verified and audit['native_notifications'] and audit['tool_calls'].get('call_device'))
        else:result['agent_pass']=False
        (directory/'report.json').write_text(json.dumps(result,ensure_ascii=False,indent=2))
        (directory/'final-state.json').write_text(json.dumps(api(),ensure_ascii=False,indent=2))
    finally:
        recorder.stop();print('EVIDENCE '+str(directory),flush=True)
if __name__=='__main__':main()
