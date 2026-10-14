#!/usr/bin/env python3
import argparse,json,time,random,uuid,hashlib,subprocess,urllib.request,urllib.error,sys
from pathlib import Path
from demo import request,tool,nav
from agh_client import invoke,export
import manage
from concurrent.futures import ThreadPoolExecutor
ROOT=Path(__file__).resolve().parents[1]
def put(path,obj):
    tmp=path.with_suffix('.tmp');tmp.write_text(json.dumps(obj,ensure_ascii=False,indent=2));tmp.replace(path)
def scene_reset():
    from agent_live import stop as stop_agent
    stop_agent()
    manage.agh_control('stop')
    c=manage.own_container()
    if c and c['State']['Running']:manage.docker('stop','--time','10',manage.NAME)
    # Fresh sensor/plugin lifetime for every method and seed.
    manage.stop_sim()
    (ROOT/'runs/sim-command.json').unlink(missing_ok=True)
    manage.start(agent_enabled=False)
    deadline=time.time()+60
    while time.time()<deadline:
        try:
            s=tool('get_state')
            if s['navigation_ready'] and s['pose_fresh'] and 'depot' in s['at_stations']:
                request('/api/admin/reset',{});return tool('get_state')
        except Exception:pass
        time.sleep(1)
    raise RuntimeError('navigation_preflight_failed')
def orders(stations,sim,seed,deadline=240,prefix='N'):
    rng=random.Random(seed)
    return [dict(order_id=prefix+str(i),package_id='P'+prefix+str(i),pickup_station='depot',dropoff_station=st,
      priority='urgent' if prefix=='U' else 'normal',deadline_sim=sim+deadline+rng.randint(0,5)) for i,st in enumerate(stations,1)]
def error_probe(name,**args):
    try:tool(name,**args);return {'passed':False,'error':'invalid_operation_accepted'}
    except urllib.error.HTTPError as e:return {'passed':True,'rejection':json.load(e).get('error')}
def inject(event,s,seed,directory):
    if event['kind']=='urgent':
        tool('create_orders',orders=orders([event['station']],s['sim_time'],seed,100,'U'))
    elif event['kind']=='receiver':request('/api/admin/receiver',{'station_id':event['station'],'available':event['available']})
    elif event['kind']=='invalid_handoff':
        o=next(o for o in s['orders'].values() if o['loaded'])
        result=[error_probe('confirm_delivery',order_id=o['order_id'],package_id=o['package_id']),error_probe('confirm_delivery',order_id=o['order_id'],package_id='WRONG')]
        put(directory/'boundary-probes.json',result)
    else:
        pending=next(o for o in s['orders'].values() if not o['loaded'])
        result=[error_probe('pickup_package',order_id=pending['order_id']),
          error_probe('create_orders',orders=[{k:pending[k] for k in ['order_id','package_id','pickup_station','dropoff_station','priority','deadline_sim']}])]
        put(directory/'boundary-probes.json',result)
def fires(trigger,s,origin):
    checks=[]
    if 'sim' in trigger:checks.append(s['sim_time']-origin>=trigger['sim'])
    if 'delivered' in trigger:checks.append(sum(o['status']=='delivered' for o in s['orders'].values())>=trigger['delivered'])
    if 'loaded' in trigger:checks.append(sum(o['loaded'] for o in s['orders'].values())>=trigger['loaded'])
    if 'navigation_elapsed_sim' in trigger:
        n=s['navigation'];checks.append(bool(n and n['status']=='running' and n['goal_station']!='depot' and s['sim_time']-n['started_at_sim']>=trigger['navigation_elapsed_sim']))
    return bool(checks) and all(checks)
def fixed_tick(method,sequence,s,counts):
    navstate=s['navigation'];active=navstate and navstate['status'] in ['pending','running','canceling']
    orders=list(s['orders'].values());open_orders=[o for o in orders if o['status'] not in ['delivered','failed']]
    if method=='rule':ordered=sorted(open_orders,key=lambda o:(o['priority']!='urgent',o['deadline_sim'],o['order_id']))
    else:
        rank={v:i for i,v in enumerate(sequence)}
        ordered=sorted(open_orders,key=lambda o:(rank.get(o['order_id'],len(sequence)),o['created_at_sim'],o['order_id']))
    urgent=next((o for o in ordered if o['priority']=='urgent' and not o['loaded']),None)
    loaded=[o for o in orders if o['loaded'] and o['status']!='failed']
    if active:
        if method=='rule' and urgent and len(loaded)<2 and navstate['goal_station']!='depot' and navstate['status']!='canceling':
            tool('cancel_navigation',nav_task_id=navstate['nav_task_id']);counts[0]+=1
        return
    if not open_orders:
        if 'depot' not in s['at_stations']:tool('navigate_to',station_id='depot');counts[0]+=1
        return
    if navstate and navstate['status']=='failed' and counts[1].get('last_failed')!=navstate['nav_task_id']:
        counts[1]['last_failed']=navstate['nav_task_id']
        goal=navstate['goal_station'];attempt=counts[1].get(goal,0)+1;counts[1][goal]=attempt
        if attempt>2:
            for o in ordered:
                if o['dropoff_station']==goal:tool('close_order',order_id=o['order_id'],reason='navigation_failed_after_2_retries')
            return
    for o in loaded:
        if o['dropoff_station'] in s['at_stations'] and s['receivers'][o['dropoff_station']]:
            tool('confirm_delivery',order_id=o['order_id'],package_id=o['package_id']);counts[0]+=1;return
    if 'depot' in s['at_stations']:
        waiting=[o for o in ordered if not o['loaded']]
        for o in waiting[:max(0,2-len(loaded))]:
            tool('pickup_package',order_id=o['order_id']);counts[0]+=1;return
    if method=='rule' and urgent and len(loaded)<2:goal='depot'
    elif loaded:
        candidates=[o for o in ordered if o['loaded'] and (s['receivers'][o['dropoff_station']] or method=='agnes_once')]
        if not candidates:return
        goal=candidates[0]['dropoff_station']
        if goal in s['at_stations']:return
    else:goal='depot'
    if goal not in s['at_stations']:tool('navigate_to',station_id=goal);counts[0]+=1
def run(case,seed,method,directory,record=False):
    directory.mkdir(parents=True,exist_ok=True)
    session=None;model_calls=0;sequence=[];counts=[0,{}];error=None;interrupted=False
    from health import SimulationHealth
    health=SimulationHealth(ROOT)
    executor=ThreadPoolExecutor(max_workers=1);pending_call=None;pending_signature=None;replan_pending=False;external_signature=None
    s=scene_reset();model_dir=directory/'model'/s['run_id'];origin=s['sim_time'];started=time.time();done=set();last_trigger=None
    if record:put(ROOT/'runs/recording.json',{'directory':str(directory.relative_to(ROOT/'runs')/'frames')})
    initial=orders(case['initial'],origin,seed,case.get('deadline',240))
    tool('create_orders',orders=initial)
    prompt=(ROOT/'agent/instructions.md').read_text()
    try:
        if method=='agnes_once':
            r=invoke('不要调用工具。针对医院容量2的机器人，给下面初始订单安排固定送达顺序。仅输出 JSON {"order_ids":["N1",...]}，必须恰好包含全部订单。站点配置：'+json.dumps(s['stations'])+'；订单：'+json.dumps(initial),model_dir)
            model_calls+=1;session=r['sessionId']
            text=r['text'];left=text.find('{');right=text.rfind('}');sequence=json.loads(text[left:right+1])['order_ids']
            if sorted(sequence)!=sorted(o['order_id'] for o in initial):raise RuntimeError('invalid_model_plan')
        while time.time()-started<1200:
            s=tool('get_state');health.check(s);elapsed=s['sim_time']-origin
            external=json.dumps([sorted(s['orders']),s['receivers']],sort_keys=True)
            if external_signature is not None and external!=external_signature:replan_pending=True
            external_signature=external
            mutations=sum(e['kind'] in ['navigation_started','navigation_cancel_requested','package_picked_up','delivery_confirmed','order_deferred','order_failed'] for e in s['events'])
            if elapsed>=600 or mutations>=60 or model_calls>=60:raise TimeoutError('shared_run_budget')
            injected=False
            for i,event in enumerate(case['events']):
                if i not in done and fires(event['trigger'],s,origin):
                    inject(event,s,seed,directory);done.add(i);injected=True
            if injected:
                s=tool('get_state');replan_pending=True
                external_signature=json.dumps([sorted(s['orders']),s['receivers']],sort_keys=True)
            unfinished=[o for o in s['orders'].values() if o['status'] not in ['delivered','failed']]
            future=any(i not in done and e['kind']=='urgent' for i,e in enumerate(case['events']))
            n=s['navigation'];active=n and n['status'] in ['pending','running','canceling']
            if not unfinished and not future and 'depot' in s['at_stations'] and not active:break
            if elapsed>150:
                for o in unfinished:
                    if not s['receivers'][o['dropoff_station']]:
                        tool('close_order',order_id=o['order_id'],reason='receiver_unavailable_150_sim_seconds');counts[0]+=1
            if method=='agnes_feedback':
                signature=json.dumps([[(o['order_id'],o['status'],o['loaded']) for o in s['orders'].values()],s['receivers'],(n or {}).get('nav_task_id'),(n or {}).get('status') if not active else 'active',s['at_stations']],sort_keys=True)
                if pending_call and pending_call.done():
                    r=pending_call.result();session=r['sessionId'];last_trigger=pending_signature;pending_call=None
                    put(ROOT/'runs/agent-live.json',dict(phase='decision_completed',run_id=s['run_id'],turns=model_calls,message=r['text'],case_id=case['id'],wall_time=time.time()))
                if not pending_call and (replan_pending or not active and signature!=last_trigger):
                    msg='请通过 hospital-delivery MCP 读取 get_state 并执行下一步配送操作。可以完成当前站点的装卸。发起导航或取消请求后立即结束本轮，不要循环等待；控制器会在导航终态和新事件后再次唤醒你。操作编号使用本轮独有前缀 '+str(model_calls)+'. 必须交接全部可交付订单，回到depot，真实不可交付则close_order说明原因。'
                    pending_signature=signature;replan_pending=False
                    pending_call=executor.submit(invoke,(prompt+'\n' if not session else '')+msg,model_dir,session)
                    model_calls+=1
                    put(ROOT/'runs/agent-live.json',dict(phase='thinking',run_id=s['run_id'],turns=model_calls,case_id=case['id'],wall_time=time.time()))
            else:fixed_tick(method,sequence,s,counts)
            time.sleep(.5)
        else:raise TimeoutError('shared_wall_budget')
    except KeyboardInterrupt:error='operator_interrupted';interrupted=True
    except Exception as e:error=str(e)
    finally:
        if pending_call:
            try:session=pending_call.result(timeout=100)['sessionId']
            except Exception as e:error=error or str(e)
        executor.shutdown(wait=True)
        if method=='agnes_feedback' and not error and session:
            try:
                r=invoke('本次任务已结束。只调用 hospital-delivery get_state 和 verify_run，按真实回执报告 delivered、failed、unfinished 及是否返回 depot；不要新增订单或运动。',model_dir,session)
                session=r['sessionId'];model_calls+=1
            except Exception as e:error='final_agent_verification:'+str(e)
        if record:(ROOT/'runs/recording.json').unlink(missing_ok=True)
        s=tool('get_state');n=s['navigation']
        if n and n['status'] in ['pending','running']:
            tool('cancel_navigation',nav_task_id=n['nav_task_id'])
            for _ in range(20):
                if tool('get_state')['navigation']['status'] in ['succeeded','failed','canceled']:break
                time.sleep(.5)
        report=tool('verify_run')
        final_state=tool('get_state');put(directory/'final-state.json',final_state)
        put(ROOT/'runs'/report['run_id']/'final-state.json',final_state)
        report.update(case_id=case['id'],seed=seed,method=method,error=error,model_turns=model_calls,model_seed_supported=False)
        put(directory/'report.json',report)
        if session:
            try:export(session,model_dir)
            except Exception as e:put(directory/'export-error.json',{'error':str(e)})
        from report import enrich
        report=enrich(report,directory,case);put(directory/'report.json',report)
        put(ROOT/'runs'/report['run_id']/'report.json',report)
        put(ROOT/'runs/agent-live.json',dict(phase='experiment_finished',run_id=report['run_id'],turns=model_calls,case_id=case['id'],wall_time=time.time(),error=error))
        if record and (directory/'frames').exists():
            result=manage.docker('exec',manage.NAME,'ffmpeg','-y','-framerate','1','-pattern_type','glob','-i','/project/'+str(directory.relative_to(ROOT)/'frames'/'*.jpg'),'-c:v','libx264','-pix_fmt','yuv420p','-movflags','+faststart','/project/'+str(directory.relative_to(ROOT)/'demo.mp4'),check=False)
            (directory/'video-encode.log').write_text(result.stderr)
    if interrupted:raise KeyboardInterrupt
    return report
def main():
    ap=argparse.ArgumentParser();ap.add_argument('--case');ap.add_argument('--method',choices=['rule','agnes_once','agnes_feedback']);ap.add_argument('--seed',type=int);ap.add_argument('--record',action='store_true');ap.add_argument('--retry-setup',action='store_true');a=ap.parse_args()
    protocol=json.loads((ROOT/'configs/experiment.json').read_text());batch=ROOT/'runs'/'experiment-v6';batch.mkdir(exist_ok=True)
    from batch import freeze
    freeze()
    digest=hashlib.sha256((ROOT/'configs/experiment.json').read_bytes()).hexdigest()
    put(batch/'protocol.json',dict(protocol=protocol,sha256=digest))
    rows=[]
    for case in protocol['cases']:
        if a.case and case['id']!=a.case:continue
        for seed in protocol['seeds']:
            if a.seed and seed!=a.seed:continue
            for method in protocol['methods']:
                if a.method and method!=a.method:continue
                d=batch/(case['id']+'_'+str(seed)+'_'+method)
                if (d/'report.json').exists():
                    previous=json.loads((d/'report.json').read_text())
                    if not a.retry_setup or previous.get('delivered') is not None:rows.append(previous);continue
                    put(d/('setup-attempt-'+str(len(list(d.glob('setup-attempt-*.json')))+1)+'.json'),previous)
                put(batch/'current.json',{'case':case['id'],'seed':seed,'method':method,'started_wall':time.time()})
                try:rows.append(run(case,seed,method,d,a.record))
                except Exception as e:
                    row=dict(case_id=case['id'],seed=seed,method=method,delivered=None,error=str(e),status='NA_preflight_or_model_setup')
                    put(d/'report.json',row);rows.append(row)
                    put(batch/'results.json',rows)
                    raise
                put(batch/'results.json',rows)
    from report import summarize
    summarize(ROOT)
    put(batch/'current.json',{'status':'finished','runs':len(rows),'finished_wall':time.time()})
if __name__=='__main__':main()
