#!/usr/bin/env python3
"""Continuous real Agnes MCP dispatcher; starts idle until orders arrive."""
import os,sys,json,time,signal,subprocess,threading,argparse,uuid
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor
from demo import tool
from agh_client import invoke,export
ROOT=Path(__file__).resolve().parents[1]
PID=ROOT/'logs/agent-live.pid';STATUS=ROOT/'runs/agent-live.json'
def write(obj):
    obj['wall_time']=time.time();p=STATUS.with_suffix('.tmp');p.write_text(json.dumps(obj,ensure_ascii=False));p.replace(STATUS)
def live_pid():
    try:
        pid=int(PID.read_text());cmd=Path('/proc',str(pid),'cmdline').read_bytes()
        return pid if str(Path(__file__).resolve()).encode() in cmd else None
    except (OSError,ValueError):return None
def start():
    if live_pid():return
    p=subprocess.Popen([sys.executable,str(Path(__file__).resolve()),'run'],cwd=ROOT,stdout=(ROOT/'logs/agent-live.log').open('a'),stderr=subprocess.STDOUT,start_new_session=True)
    PID.write_text(str(p.pid));print('Agent PID',p.pid)
def stop():
    pid=live_pid()
    if not pid:return
    os.kill(pid,signal.SIGTERM)
    deadline=time.time()+110
    while live_pid() and time.time()<deadline:time.sleep(.5)
    if live_pid():raise RuntimeError('Agent still finishing an authorized model turn; stop not confirmed')
def run():
    from health import SimulationHealth
    health=SimulationHealth(ROOT);health_error=None
    stopping=threading.Event()
    signal.signal(signal.SIGTERM,lambda *_:stopping.set())
    signal.signal(signal.SIGINT,lambda *_:stopping.set())
    executor=ThreadPoolExecutor(max_workers=1);future=None;session=None;directory=None
    run_id=None;last_sig=None;last_external=None;replan=False;submitted_sig=None;turn=0;origin=None;started=None;failures=0;next_idle_wake=None
    prompt=(ROOT/'agent/instructions.md').read_text()
    try:
        while not stopping.is_set():
            health.check()
            try:s=tool('get_state')
            except Exception as e:write({'phase':'waiting_for_service','error':str(e)});time.sleep(1);continue
            health.check(s)
            if s['run_id']!=run_id and not future:
                run_id=s['run_id'];session=None;directory=ROOT/'runs'/run_id/'agent';turn=0;origin=None;last_sig=None;last_external=None;replan=False;failures=0;next_idle_wake=None
            n=s['navigation'];active=n and n['status'] in ['pending','running','canceling']
            unfinished=[o for o in s['orders'].values() if o['status'] not in ['delivered','failed']]
            external=json.dumps([sorted(s['orders']),s['receivers']],sort_keys=True)
            if last_external is not None and external!=last_external:replan=True
            last_external=external
            signature=json.dumps([[(o['order_id'],o['status'],o['loaded']) for o in s['orders'].values()],s['receivers'],(n or {}).get('nav_task_id'),(n or {}).get('status') if not active else 'active',s['at_stations']],sort_keys=True)
            if future and future.done():
                try:
                    r=future.result();session=r['sessionId'];export(session,directory);failures=0
                    write({'phase':'decision_completed','run_id':run_id,'turns':turn,'message':r['text']})
                except Exception as e:failures+=1;replan=True;write({'phase':'model_error','run_id':run_id,'error':str(e),'failures':failures})
                last_sig=submitted_sig;future=None
            idle=not unfinished and not active and 'depot' in s['at_stations']
            if idle and not future:
                if origin is not None and session:
                    origin=None;started=None;replan=False;submitted_sig=signature
                    future=executor.submit(invoke,'只调用 hospital-delivery get_state 和 verify_run，根据实际回执报告任务终态及返库；不要新增业务或运动。',directory,session)
                    turn+=1;write({'phase':'verifying','run_id':run_id,'turns':turn})
                else:
                    origin=None;started=None;replan=False;write({'phase':'idle','run_id':run_id,'turns':turn})
            elif s['navigation_ready'] and s['pose_fresh'] and not future:
                if origin is None:origin=s['sim_time'];started=time.time()
                if turn>=60 or s['sim_time']-origin>=600 or time.time()-started>=1200 or failures>=3:
                    reason='model_failure_limit' if failures>=3 else 'model_turn_budget' if turn>=60 else 'sim_time_budget' if s['sim_time']-origin>=600 else 'wall_time_budget'
                    write({'phase':'stopped_at_budget','reason':reason,'run_id':run_id,'unfinished':[o['order_id'] for o in unfinished],'turns':turn})
                    (ROOT/'runs'/run_id/'agent-stop.json').write_text(json.dumps({'reason':reason,'report':tool('verify_run')},ensure_ascii=False,indent=2))
                    if active and n['status']!='canceling':tool('cancel_navigation',nav_task_id=n['nav_task_id'])
                    time.sleep(1);continue
                if replan or not active and (signature!=last_sig or unfinished and (next_idle_wake is None or s['sim_time']>=next_idle_wake)):
                    msg='请只通过 hospital-delivery MCP get_state 读取实际状态并决策下一步。当前站点可装卸；发起导航或取消后立即结束本轮，不要循环等待。取消确认终态后才发新目标。新的紧急订单应重新考虑顺序，保留原订单和载货。每轮operation_id独有前缀 '+str(turn)+'. 全部订单处理后返回depot并verify_run。'
                    submitted_sig=signature;replan=False;next_idle_wake=s['sim_time']+30
                    future=executor.submit(invoke,(prompt+'\n' if not session else '')+msg,directory,session)
                    turn+=1;write({'phase':'thinking','run_id':run_id,'turns':turn})
            time.sleep(.5)
    except RuntimeError as e:
        health_error=str(e)
        write({'phase':'simulation_failed','error':health_error,'run_id':run_id,'turns':turn})
    finally:
        if future:
            try:r=future.result(timeout=100);session=r['sessionId'];export(session,directory)
            except Exception:pass
        executor.shutdown(wait=True)
        try:
            s=tool('get_state');n=s['navigation']
            if n and n['status'] in ['pending','running']:tool('cancel_navigation',nav_task_id=n['nav_task_id'])
        except Exception:pass
        write({'phase':'simulation_failed' if health_error else 'stopped','error':health_error,'run_id':run_id,'turns':turn})
if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('action',choices=['start','stop','status','run']);a=p.parse_args()
    if a.action=='status':print(json.dumps({'pid':live_pid(),'state':json.loads(STATUS.read_text()) if STATUS.exists() else None},ensure_ascii=False))
    else:globals()[a.action]()
