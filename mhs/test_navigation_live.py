"""Bounded real Hub→MHS→Nav2 validation, kept separate from Agent acceptance."""
import asyncio,json,time,uuid
from .manage import ROOT,api
from .hub_client import HubClient
from .record import Recorder

async def stopped(timeout=20):
    end=time.monotonic()+timeout
    while time.monotonic()<end:
        s=await asyncio.to_thread(api)
        if s['stopped_confirmed']:return s
        await asyncio.sleep(.2)
    raise RuntimeError('physical_stop_not_confirmed')

async def main():
    directory=ROOT/'runs/mhs-v1/acceptance'/('navigation-'+time.strftime('%Y%m%d-%H%M%S'))
    directory.mkdir(parents=True);results=[];rec=Recorder(directory).start()
    try:
        async with HubClient(record=directory/'hub.jsonl') as hub:
            for station in ['station_a','station_b','station_c','depot']:
                await stopped();op=uuid.uuid4().hex;receipt=await hub.navigate(station,op)
                if not receipt.get('accepted'):raise RuntimeError('Hub_navigation_rejected:'+str(receipt))
                result=await hub.finish(receipt['job']);s=await stopped()
                row={'case':'arrival_'+station,'receipt':receipt,'result':result,'state':s,'pass':result['status']=='done'}
                results.append(row);print(json.dumps({'case':row['case'],'pass':row['pass'],'pose':s['pose']},ensure_ascii=False),flush=True)
                (directory/'report.json').write_text(json.dumps(results,ensure_ascii=False,indent=2))
                if not row['pass']:raise RuntimeError('real_navigation_failed')
                if station=='station_a':
                    duplicate=await hub.navigate(station,op);again=await hub.finish(duplicate['job'])
                    results.append({'case':'idempotent_navigation','result':again,'pass':again.get('data',{}).get('navigation',{}).get('nav_task_id')==result.get('data',{}).get('navigation',{}).get('nav_task_id')})
            start=await hub.navigate('station_a');end=time.monotonic()+30
            while time.monotonic()<end:
                s=await asyncio.to_thread(api)
                if s['pose']['speed']>.1:break
                await asyncio.sleep(.2)
            else:raise RuntimeError('no_actual_motion_observed')
            before=s;receipt=await hub.rpc('hub/stop',{'device':'hospital-robot-01'});after=await stopped()
            result=await hub.finish(start['job']);results.append({'case':'cancel_real_motion','before':before,'after':after,'receipt':receipt,'result':result,'pass':after['stopped_confirmed'] and after['navigation']['status']=='canceled'})
            changed=await hub.navigate('depot');result=await hub.finish(changed['job']);after=await stopped()
            results.append({'case':'change_goal_after_stop','result':result,'state':after,'pass':result['status']=='done'})
    except Exception as e:
        results.append({'case':'validation_error','error':str(e),'pass':False});print('ERROR',str(e),flush=True)
        await asyncio.to_thread(api,'/api/mhs/stop',{'reason':'navigation_acceptance_error'})
    finally:
        rec.stop();(directory/'report.json').write_text(json.dumps(results,ensure_ascii=False,indent=2))
        print('EVIDENCE',str(directory),flush=True)

if __name__=='__main__':asyncio.run(main())
