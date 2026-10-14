"""Focused completion of the Hub-disconnect gate; preserve prior safety report."""
import asyncio,json,time,subprocess
from .manage import ROOT,ENV,HOME,CLI,api
from .hub_client import HubClient
from .test_safety_live import wait_state,ready,begin
from .record import Recorder
async def main():
    directory=ROOT/'runs/mhs-v1/acceptance'/('hub-loss-'+time.strftime('%Y%m%d-%H%M%S'));directory.mkdir(parents=True);rows=[];rec=Recorder(directory).start()
    try:
        async with HubClient(record=directory/'hub.jsonl') as hub:
            call=await begin(hub,'station_a');before=await wait_state(lambda s:s['pose']['speed']>.1)
            closed=time.time()
            stop=subprocess.run(CLI+['daemon','stop','--home',str(HOME),'--workspace',str(ROOT)],env=ENV,cwd=ROOT,capture_output=True,text=True,timeout=45)
            (directory/'daemon-stop.log').write_text(stop.stdout+stop.stderr)
            after=await wait_state(lambda s:s['stopped_confirmed'],20)
            rows.append({'case':'hub_disconnect_stops_real_motion','before':before,'after':after,'closed_wall':closed,'confirmed_wall':time.time(),
                'pass':stop.returncode==0 and after['navigation']['status']=='canceled' and not after['motion_guard']['armed']})
        start=subprocess.run(CLI+['daemon','start','--home',str(HOME),'--workspace',str(ROOT)],env=ENV,cwd=ROOT,capture_output=True,text=True,timeout=45)
        (directory/'daemon-start.log').write_text(start.stdout+start.stderr)
        if start.returncode:raise RuntimeError('project_daemon_restart_failed')
        await wait_state(lambda s:s['motion_guard']['healthy'],35);await asyncio.sleep(3);s=api()
        rows.append({'case':'hub_reconnect_does_not_resume_old_goal','state':s,'pass':s['stopped_confirmed'] and not s['motion_guard']['armed'] and s['navigation']['status']=='canceled'})
        async with HubClient(record=directory/'hub.jsonl') as hub:
            call=await begin(hub,'depot');result=await hub.finish(call['job']);await ready(hub)
            rows.append({'case':'new_goal_after_reconnection','result':result,'state':api(),'pass':result['status']=='done'})
    except Exception as e:
        rows.append({'case':'validation_error','error':str(e),'pass':False});api('/api/mhs/stop',{'reason':'hub_loss_acceptance_error'})
    finally:
        rec.stop();(directory/'report.json').write_text(json.dumps(rows,ensure_ascii=False,indent=2));print(json.dumps([{'case':r['case'],'pass':r['pass'],'error':r.get('error')} for r in rows]));print('EVIDENCE',directory)
if __name__=='__main__':asyncio.run(main())
