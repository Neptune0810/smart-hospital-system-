"""Real motion, independent guard and Nav2 failure checks; no Agent substitution."""
import asyncio,json,time,os,signal,subprocess
from .manage import ROOT,ENV,HOME,CLI,NAME,api,process,spawn,container
from .hub_client import HubClient
from .record import Recorder

async def wait_state(test,timeout=25):
    end=time.monotonic()+timeout
    while time.monotonic()<end:
        s=await asyncio.to_thread(api)
        if test(s):return s
        await asyncio.sleep(.1)
    raise RuntimeError('state_condition_timeout')

async def ready(hub):
    end=time.monotonic()+25
    while time.monotonic()<end:
        s=await asyncio.to_thread(api);devices=(await hub.rpc('hub/devices',{}))['devices']
        d=next((x for x in devices if x['id']=='hospital-robot-01'),{})
        if s['stopped_confirmed'] and s['motion_guard']['healthy'] and d.get('available') and d.get('state',{}).get('values',{}).get('stop_confirmed') and not d.get('busy',{}).get('chassis'):return s
        await asyncio.sleep(.25)
    raise RuntimeError('device_stop_and_resource_not_ready')

async def begin(hub,station):
    await ready(hub);receipt=await hub.navigate(station)
    if not receipt.get('accepted'):raise RuntimeError('navigation_rejected:'+str(receipt))
    return receipt

async def main():
    if any(o['status'] not in {'delivered','failed'} for o in api()['orders'].values()):raise RuntimeError('active_delivery_run')
    directory=ROOT/'runs/mhs-v1/acceptance'/('safety-'+time.strftime('%Y%m%d-%H%M%S'));directory.mkdir(parents=True)
    rows=[];rec=Recorder(directory).start()
    def save(row):
        rows.append(row);(directory/'report.json').write_text(json.dumps(rows,ensure_ascii=False,indent=2))
        print(json.dumps({'case':row['case'],'pass':row['pass']},ensure_ascii=False),flush=True)
    async def depot(hub):
        call=await begin(hub,'depot');result=await hub.finish(call['job'])
        await ready(hub)
        if result['status']!='done':raise RuntimeError('depot_navigation_failed')
        return result
    try:
        async with HubClient(record=directory/'hub.jsonl') as hub:
            start=await begin(hub,'station_a');before=await wait_state(lambda s:s['pose']['speed']>.1)
            requested=time.time();reply=await hub.rpc('hub/stop',{'device':'hospital-robot-01'})
            after=await wait_state(lambda s:s['stopped_confirmed']);result=await hub.finish(start['job'])
            save(dict(case='stop_real_motion',**{'pass':after['navigation']['status']=='canceled'},before=before,after=after,requested_wall=requested,confirmed_wall=time.time(),reply=reply,result=result))
            changed=await depot(hub);save({'case':'change_goal_after_confirmed_stop','result':changed,'state':api(),'pass':True})
            start=await begin(hub,'station_a');before=await wait_state(lambda s:s['pose']['speed']>.1)
            pid=process('bridge')
            if not pid:raise RuntimeError('owned_bridge_missing')
            killed=time.time();os.killpg(pid,signal.SIGKILL)
            after=await wait_state(lambda s:s['stopped_confirmed'],20)
            await asyncio.sleep(3);still=api()
            save({'case':'adapter_exit_independent_guard','before':before,'after':after,'after_wait':still,'exit_wall':killed,'confirmed_wall':time.time(),
                'pass':after['navigation']['status']=='canceled' and not still['motion_guard']['armed'] and still['pose']['speed']<.05})
            spawn('bridge',[str(ROOT/'agent/mhs-venv/bin/python'),'-m','mhs.bridge'],ENV)
            await ready(hub);resumed=api()
            save({'case':'reconnect_does_not_resume_old_motion','state':resumed,'pass':not resumed['motion_guard']['armed'] and resumed['navigation']['status']=='canceled'})
            await depot(hub)
            if not container()['State']['Running']:raise RuntimeError('project_container_not_running')
            command=['docker','exec','-e','ROS_DOMAIN_ID=73',NAME,'bash','-c','source /opt/ros/humble/setup.bash && ros2 lifecycle set /carter1/bt_navigator ']
            deactivate=subprocess.run(command[:-1]+[command[-1]+'deactivate'],capture_output=True,text=True,timeout=30)
            try:
                if deactivate.returncode:raise RuntimeError('Nav2_deactivation_failed:'+deactivate.stdout+deactivate.stderr)
                call=await hub.navigate('station_a')
                result=await hub.finish(call['job'],30) if call.get('accepted') else call
                s=await wait_state(lambda s:s['stopped_confirmed'])
                failed=(s.get('navigation') or {}).get('status')=='failed'
                save({'case':'real_nav2_inactive_rejection','deactivate':deactivate.stdout,'receipt':call,'result':result,'state':s,'pass':failed})
            finally:
                activate=subprocess.run(command[:-1]+[command[-1]+'activate'],capture_output=True,text=True,timeout=30)
                (directory/'nav2-reactivate.log').write_text(activate.stdout+activate.stderr)
                if activate.returncode:raise RuntimeError('Nav2_reactivation_failed')
            await ready(hub)
            start=await begin(hub,'station_a');before=await wait_state(lambda s:s['pose']['speed']>.1)
            closed=time.time()
            result=subprocess.run(CLI+['daemon','stop','--home',str(HOME),'--workspace',str(ROOT)],env=ENV,cwd=ROOT,capture_output=True,text=True,timeout=45)
            after=await wait_state(lambda s:s['stopped_confirmed'],20)
            save({'case':'hub_disconnect_stops_real_motion','before':before,'after':after,'closed_wall':closed,'confirmed_wall':time.time(),'daemon_exit':result.returncode,
                'pass':after['navigation']['status']=='canceled' and not after['motion_guard']['armed']})
        subprocess.run(CLI+['daemon','start','--home',str(HOME),'--workspace',str(ROOT)],env=ENV,cwd=ROOT,capture_output=True,text=True,timeout=45,check=True)
        await wait_state(lambda s:s['motion_guard']['healthy'],30)
        await asyncio.sleep(3);s=api()
        save({'case':'hub_reconnect_does_not_resume_motion','state':s,'pass':not s['motion_guard']['armed'] and s['navigation']['status']=='canceled' and s['stopped_confirmed']})
        async with HubClient(record=directory/'hub.jsonl') as hub:await depot(hub)
    except Exception as e:
        save({'case':'validation_error','error':str(e),'pass':False})
        api('/api/mhs/stop',{'reason':'safety_acceptance_error'})
    finally:
        rec.stop();print('EVIDENCE',directory,flush=True)

if __name__=='__main__':asyncio.run(main())
