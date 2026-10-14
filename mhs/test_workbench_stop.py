"""Real 2s MHS reply + actual webpage run-stop. Not an Agent delivery run."""
import asyncio,json,time
from .manage import ROOT,api
from .hub_client import HubClient
from .record import Recorder
from .test_safety_live import ready,begin,wait_state

async def main():
    if api()['orders'] or api().get('operator_stopped'):raise RuntimeError('requires_fresh_empty_run')
    directory=ROOT/'runs/mhs-v1/acceptance'/('workbench-stop-'+time.strftime('%Y%m%d-%H%M%S'));directory.mkdir(parents=True,exist_ok=False)
    (ROOT/'logs/mhs/workbench-stop-current.json').write_text(json.dumps({'directory':str(directory)}))
    rec=Recorder(directory).start();checks=[]
    try:
        async with HubClient(record=directory/'hub.jsonl') as hub:
            receipt=await begin(hub,'station_a');before=await wait_state(lambda s:s['pose']['speed']>.1,45)
            start=time.monotonic();reply=await hub.rpc('hub/stop',{'device':'hospital-robot-01'});latency=time.monotonic()-start
            after=await wait_state(lambda s:s['stopped_confirmed'],25);result=await hub.finish(receipt['job'],25)
            checks.append({'case':'native_stop_reply_and_actual_stillness','pass':latency<2 and after['navigation']['status']=='canceled','reply_seconds':latency,'before':before,'after':after,'reply':reply,'result':result})
            await ready(hub);back=await hub.navigate('depot');await hub.finish(back['job'],120);await ready(hub)
            receipt=await begin(hub,'station_a');before=await wait_state(lambda s:s['pose']['speed']>.1,45)
            (directory/'before-web-stop.json').write_text(json.dumps(before,ensure_ascii=False,indent=2))
            print('WAITING_WEB_STOP '+str(directory),flush=True)
            after=await wait_state(lambda s:bool(s.get('operator_stopped')) and s['stopped_confirmed'],180)
            result=await hub.finish(receipt['job'],25)
            refused=await hub.navigate('depot')
            await asyncio.sleep(3);still=api()
            checks.append({'case':'web_stop_latches_and_refuses_new_motion','pass':after['navigation']['status']=='canceled' and refused.get('accepted') is False and still['stopped_confirmed'] and not still['motion_guard']['armed'],'before':before,'after':after,'result':result,'new_motion_rejected':refused,'after_wait':still})
        report=api('/api/report');report.update(checks=checks,all_checks_passed=all(c['pass'] for c in checks),acceptance_input='raw MHS + actual webpage stop, not Agent delivery')
        (directory/'report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2))
        (directory/'final-state.json').write_text(json.dumps(api(),ensure_ascii=False,indent=2))
    except Exception as error:
        api('/api/mhs/stop',{'reason':'stop_acceptance_error'})
        (directory/'report.json').write_text(json.dumps({'checks':checks,'all_checks_passed':False,'error':str(error)},ensure_ascii=False,indent=2));raise
    finally:rec.stop();print('EVIDENCE '+str(directory),flush=True)
if __name__=='__main__':asyncio.run(main())
