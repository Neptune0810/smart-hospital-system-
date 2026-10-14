"""One real Agnes delivery via official CLI; no scripted business/motion actions."""
import argparse,hashlib,json,os,signal,subprocess,time,uuid
from pathlib import Path
from .manage import ROOT,ENV,CLI,api
from .workbench_check import facade
from .record import Recorder

def main():
    parser=argparse.ArgumentParser();parser.add_argument('--pickup',default='depot');parser.add_argument('--destination',default='station_c');parser.add_argument('--quantity',type=int,default=1);args=parser.parse_args()
    s=api();stations=s['stations']
    if args.pickup not in stations or args.destination not in stations or args.pickup==args.destination or not 1<=args.quantity<=2:raise ValueError('invalid_acceptance_order')
    if s['orders'] or not s['stopped_confirmed'] or not s['navigation_ready'] or not s['motion_guard']['healthy']:raise RuntimeError('fresh_idle_run_required')
    directory=ROOT/'runs/mhs-v2/acceptance'/('delivery-cli-'+time.strftime('%Y%m%d-%H%M%S'));directory.mkdir(parents=True)
    code,opened=facade('/api/demo/command',{'action':'open','operation_id':uuid.uuid4().hex,'expected_run_id':s['run_id'],'session_id':None})
    if code!=200:raise RuntimeError('canonical_chat_open_failed')
    session=opened['result']['new_session'];prompt=f"从{stations[args.pickup]['label']}取{args.quantity}件药品，送到{stations[args.destination]['label']}，普通配送，尽量少干扰患者，交接后原地待命。"
    protocol={'scope':'Real Agnes CLI delivery; NOT browser acceptance','session':session,'run_id':s['run_id'],'prompt':prompt,'started_wall':time.time(),'people_seed':s['crowd']['seed'],'crowd_mode':s['crowd']['mode'],
              'max_model_decisions':60,'max_sim_seconds':2400,'max_wall_seconds':3600,'no_model_resubmissions':True,
              'source_sha256':{p:hashlib.sha256((ROOT/p).read_bytes()).hexdigest() for p in ['mhs/acceptance_delivery_v2.py','mhs/engine_v2.py','mhs/navigation_v2.py','mhs/bridge.py','mhs/mcp_server.py','mhs/policy/instructions.md','mhs/policy/package.json']}}
    (directory/'protocol.json').write_text(json.dumps(protocol,ensure_ascii=False,indent=2))
    recorder=Recorder(directory).start();child=None;error=None;last=None;finished_at=None;interrupted=[False]
    signal.signal(signal.SIGTERM,lambda *_:interrupted.__setitem__(0,True));signal.signal(signal.SIGINT,lambda *_:interrupted.__setitem__(0,True))
    try:
        with (directory/'cli.jsonl').open('w') as f:
            child=subprocess.Popen(CLI+['--profile','local-dev','--cwd',str(ROOT),'--resume',session,'-p',prompt,'--mode','json'],cwd=ROOT,env=ENV,stdin=subprocess.DEVNULL,stdout=f,stderr=subprocess.STDOUT,start_new_session=True)
            while time.time()-protocol['started_wall']<3600 and not interrupted[0]:
                state=api()
                if state['run_id']!=s['run_id']:raise RuntimeError('run_changed_during_acceptance')
                key=(state['latest_event_id'],state.get('stopped_confirmed'))
                if key!=last:
                    with (directory/'state.jsonl').open('a') as trace:trace.write(json.dumps({'wall_time':time.time(),'state':state},ensure_ascii=False)+'\n')
                    last=key
                if state.get('mhs_finished'):
                    if finished_at is None:finished_at=time.monotonic()
                    # Allow the native completion turn to write its verification and reply.
                    if time.monotonic()-finished_at>=45:break
                if (state.get('mhs_budget') or {}).get('stopped_reason') or state.get('operator_stopped'):break
                if child.poll() not in (None,0):raise RuntimeError('CLI_failed:'+str(child.returncode))
                if not state['orders'] and child.poll() is not None and time.time()-protocol['started_wall']>=60:raise RuntimeError('no_order_created')
                time.sleep(.5)
    except Exception as failure:error=str(failure)
    finally:
        if child and child.poll() is None:
            # Recheck this exact child, project cwd and selected session.
            p=Path('/proc')/str(child.pid);argv=p.joinpath('cmdline').read_bytes().split(b'\0')
            if p.joinpath('cwd').resolve()!=ROOT or str(ROOT/'agent/agh-runtime-mhs/agnes.mjs').encode() not in argv or session.encode() not in argv:raise RuntimeError('acceptance_CLI_ownership_changed')
            os.killpg(child.pid,signal.SIGTERM)
            try:child.wait(timeout=20)
            except subprocess.TimeoutExpired:os.killpg(child.pid,signal.SIGKILL);child.wait()
        state=api()
        if not state.get('mhs_finished'):
            api('/api/mhs/stop',{'reason':'delivery_acceptance_end_without_completion'})
            end=time.monotonic()+20
            while not api()['stopped_confirmed'] and time.monotonic()<end:time.sleep(.2)
        recorder.stop()
        report=api('/api/report');report.update(input_source=protocol['scope'],observer_interrupted=interrupted[0],acceptance_error=error)
        (directory/'final-state.json').write_text(json.dumps(api(),ensure_ascii=False,indent=2))
        subprocess.run([str(ROOT/'agent/mhs-venv/bin/python'),'-m','mhs.export_session',session,str(directory)],cwd=ROOT,env=ENV,check=True)
        subprocess.run([str(ROOT/'agent/mhs-venv/bin/python'),'-m','mhs.audit_session',str(directory)],cwd=ROOT,env=ENV,check=True,capture_output=True)
        audit=json.loads((directory/'session-audit.json').read_text())
        report['pass']=bool(not error and not interrupted[0] and report.get('complete_task') and report['total']==args.quantity and report['delivered']==args.quantity and report['error_deliveries']==0 and audit['final_standby_verified_by_agent'] and audit['tool_calls'].get('call_device') and audit['native_notifications'])
        (directory/'report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2));print('EVIDENCE',directory,flush=True)
    return 0 if report['pass'] else 1

if __name__=='__main__':raise SystemExit(main())
