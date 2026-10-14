"""Real MHS cancellation, adapter-loss and corridor-choice evidence for V2."""
import asyncio,hashlib,json,math,os,signal,subprocess,time,uuid
from .manage import ROOT,ENV,HOME,RUNTIME,CLI,NAME,api,process,spawn,start_daemon,stop_process,container
from .hub_client import HubClient
from .record import Recorder
from .test_safety_live import wait_state,ready

async def begin(hub,station,index=0):
    await ready(hub)
    # Candidate computation is deliberately outside the model qualification.
    options=await asyncio.to_thread(api,'/api/mhs/routes',{'station_id':station})
    if len(options['routes'])<=index:raise RuntimeError('insufficient_real_routes')
    route=options['routes'][index];op=uuid.uuid4().hex
    receipt=await hub.rpc('hub/call',{'device':'hospital-robot-01','tool':'navigate_route','arguments':{'route_id':route['route_id'],'operation_id':op}})
    if not receipt.get('accepted'):raise RuntimeError('MHS_route_not_accepted:'+str(receipt))
    return receipt,route,op

async def main():
    directory=ROOT/'runs/mhs-v2/qualification'/('safety-'+time.strftime('%Y%m%d-%H%M%S'));directory.mkdir(parents=True)
    rows=[];recorder=Recorder(directory).start()
    expected=['cancel_measured_stop','new_goal_after_stop','same_operation_no_new_navigation','adapter_loss_measured_stop','reconnect_no_auto_resume','alternate_corridor_real_arrival','nav2_inactive_refuses_new_motion','hub_disconnect_measured_stop','hub_reconnect_no_auto_resume']
    (directory/'protocol.json').write_text(json.dumps({'scope':'Real MHS/Nav2 safety; not model acceptance','cases':expected,'stop_observation_wall_seconds':20,'source_sha256':{p:hashlib.sha256((ROOT/p).read_bytes()).hexdigest() for p in ['mhs/qualify_v2_safety.py','mhs/bridge.py','mhs/navigation_v2.py','mhs/engine_v2.py','mhs/guard.py','mhs/motion_limits.py']}},indent=2))
    def save(row):
        rows.append(row);(directory/'report.json').write_text(json.dumps(rows,ensure_ascii=False,indent=2));print(json.dumps({'case':row['case'],'pass':row['pass']}),flush=True)
    try:
        async with HubClient(record=directory/'hub.jsonl') as hub:
            call,route,op=await begin(hub,'station_c')
            depot=api()['stations']['depot']
            before=await wait_state(lambda s:s['pose']['speed']>.1 and math.hypot(s['pose']['x']-depot['x'],s['pose']['y']-depot['y'])>1.2,90)
            requested=time.time();reply=await hub.rpc('hub/stop',{'device':'hospital-robot-01'})
            after=await wait_state(lambda s:s['stopped_confirmed']);result=await hub.finish(call['job'],30)
            save({'case':'cancel_measured_stop','pass':after['navigation']['status']=='canceled','before':before,'after':after,'requested_wall':requested,'confirmed_wall':time.time(),'reply':reply,'result':result})
            call,route,op=await begin(hub,'depot');result=await hub.finish(call['job'],900);await ready(hub)
            save({'case':'new_goal_after_stop','pass':result['status']=='done' and 'depot' in api()['at_stations'],'result':result,'route':route,'state':api()})
            # After completion, transport retry returns the original terminal task.
            before=api();duplicate=await hub.rpc('hub/call',{'device':'hospital-robot-01','tool':'navigate_route','arguments':{'route_id':route['route_id'],'operation_id':op}})
            dup_result=await hub.finish(duplicate['job'],30) if duplicate.get('accepted') else duplicate
            after=api()
            save({'case':'same_operation_no_new_navigation','pass':dup_result.get('status')=='done' and len(after['navigation_tasks'])==len(before['navigation_tasks']) and after['stopped_confirmed'],'reply':duplicate,'result':dup_result,'before':before,'after':after})
            call,route,op=await begin(hub,'station_c')
            before=await wait_state(lambda s:s['pose']['speed']>.1)
            pid=process('bridge')
            if not pid:raise RuntimeError('owned_bridge_missing')
            exited=time.time();os.killpg(pid,signal.SIGKILL)
            after=await wait_state(lambda s:s['stopped_confirmed'],20);await asyncio.sleep(3);still=api()
            save({'case':'adapter_loss_measured_stop','pass':after['navigation']['status']=='canceled' and not still['motion_guard']['armed'] and still['stopped_confirmed'],'before':before,'after':after,'later':still,'exit_wall':exited,'confirmed_wall':time.time()})
            spawn('bridge',[str(ROOT/'agent/mhs-venv/bin/python'),'-m','mhs.bridge'],ENV);await ready(hub)
            after=api();save({'case':'reconnect_no_auto_resume','pass':after['stopped_confirmed'] and not after['motion_guard']['armed'],'state':after})
            call,route,op=await begin(hub,'station_c',1);start=api();result=await hub.finish(call['job'],900);await ready(hub);after=api()
            trace=[p[1:] for p in after['trajectory'] if p[0]>=start['sim_time']]
            # Corridor deviation refers to measured ROS map positions, not candidate pixels.
            deviations=[min(math.dist(p,q) for q in route['points']) for p in trace]
            save({'case':'alternate_corridor_real_arrival','pass':result['status']=='done' and 'station_c' in after['at_stations'] and bool(deviations) and max(deviations)<=1.7,'route':route,'result':result,'state':after,'max_measured_corridor_deviation_m':max(deviations,default=None)})
            options=await asyncio.to_thread(api,'/api/mhs/routes',{'station_id':'depot'});route=options['routes'][0]
            if not container()['State']['Running']:raise RuntimeError('owned_project_container_not_running')
            lifecycle=['docker','exec','-e','ROS_DOMAIN_ID=73',NAME,'bash','-c','source /opt/ros/humble/setup.bash && ros2 lifecycle set /carter1/bt_navigator ']
            deactivate=await asyncio.to_thread(subprocess.run,lifecycle[:-1]+[lifecycle[-1]+'deactivate'],capture_output=True,text=True,timeout=30)
            try:
                if deactivate.returncode:raise RuntimeError('Nav2_deactivation_failed:'+deactivate.stdout+deactivate.stderr)
                before=await wait_state(lambda s:not s['navigation_ready'])
                call=await hub.rpc('hub/call',{'device':'hospital-robot-01','tool':'navigate_route','arguments':{'route_id':route['route_id'],'operation_id':uuid.uuid4().hex}})
                result=await hub.finish(call['job'],30) if call.get('accepted') else call
                after=api();rejected=not call.get('accepted') and call.get('reason')=='unsafe' or result.get('status')=='error'
                save({'case':'nav2_inactive_refuses_new_motion','pass':bool(rejected and after['stopped_confirmed'] and len(after['navigation_tasks'])==len(before['navigation_tasks']) and not after['motion_guard']['armed']),'deactivation':deactivate.stdout,'receipt':call,'result':result,'before':before,'after':after})
            finally:
                activate=await asyncio.to_thread(subprocess.run,lifecycle[:-1]+[lifecycle[-1]+'activate'],capture_output=True,text=True,timeout=30)
                (directory/'nav2-reactivate.log').write_text(activate.stdout+activate.stderr)
                if activate.returncode:raise RuntimeError('Nav2_reactivation_failed')
            await ready(hub)
            call,route,op=await begin(hub,'depot');before=await wait_state(lambda s:s['pose']['speed']>.1)
            if not process('daemon'):raise RuntimeError('owned_daemon_missing')
            closed=time.time()
            stopped=await asyncio.to_thread(subprocess.run,CLI+['daemon','stop','--home',str(HOME),'--workspace',str(ROOT)],env=ENV,cwd=ROOT,capture_output=True,text=True,timeout=45)
            after=await wait_state(lambda s:s['stopped_confirmed'],20)
            save({'case':'hub_disconnect_measured_stop','pass':after['navigation']['status']=='canceled' and not after['motion_guard']['armed'],'before':before,'after':after,'closed_wall':closed,'confirmed_wall':time.time(),'daemon_exit':stopped.returncode})
        start_daemon();await wait_state(lambda s:s['motion_guard']['healthy'],30);await asyncio.sleep(3);after=api()
        save({'case':'hub_reconnect_no_auto_resume','pass':after['stopped_confirmed'] and not after['motion_guard']['armed'] and after['navigation']['status']=='canceled','state':after})
    except Exception as error:
        save({'case':'qualification_error','pass':False,'error':str(error)})
        try:await asyncio.to_thread(api,'/api/mhs/stop',{'reason':'v2_safety_qualification_failure'})
        except Exception:pass
    finally:
        if not process('daemon'):start_daemon()
        if not process('bridge'):spawn('bridge',[str(ROOT/'agent/mhs-venv/bin/python'),'-m','mhs.bridge'],ENV)
        for name in expected:
            if not any(row['case']==name for row in rows):save({'case':name,'pass':None,'status':'not_run','reason':'earlier_case_error'})
        stop_process('web');spawn('web',CLI+['serve','--home',str(HOME),'--profile','local-dev','--cwd',str(ROOT),'--port','4189'],ENV)
        recorder.stop();print('EVIDENCE',directory,flush=True)
    return 0 if all(row['pass'] is True for row in rows) else 1

if __name__=='__main__':raise SystemExit(asyncio.run(main()))
