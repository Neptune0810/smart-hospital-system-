"""Real MHS/Nav2 qualification, explicitly separate from model acceptance."""
import argparse,asyncio,hashlib,json,time,uuid
from .manage import ROOT,api
from .hub_client import HubClient
from .record import Recorder

async def still(timeout=20):
    end=time.monotonic()+timeout
    while time.monotonic()<end:
        s=await asyncio.to_thread(api)
        if s['stopped_confirmed']:return s
        await asyncio.sleep(.25)
    raise RuntimeError('actual_stop_not_confirmed')

async def main(stations,route_preference):
    directory=ROOT/'runs/mhs-v2/qualification'/time.strftime('%Y%m%d-%H%M%S');directory.mkdir(parents=True)
    paths=['mhs/hospital_sim.py','mhs/layout.py','mhs/crowd_v2.py','mhs/navigation.py','mhs/navigation_v2.py','mhs/motion_limits.py','mhs/proximity.py','mhs/guard.py','mhs/engine.py','mhs/engine_v2.py','mhs/routes.py','mhs/bridge.py','configs/stations.json','configs/nav2.yaml','configs/hospital-v2.json']
    (directory/'protocol.json').write_text(json.dumps({'started_wall':time.time(),'stations':stations,'route_preference':route_preference,'navigation_timeout_wall_seconds':900,'stop_timeout_wall_seconds':20,'scope':'real MHS/Nav2, not Agent task acceptance','sha256':{p:hashlib.sha256((ROOT/p).read_bytes()).hexdigest() for p in paths}},indent=2))
    rows=[];record=Recorder(directory).start()
    try:
        s=await still();first=s['crowd'];await asyncio.sleep(3);second=(await still())['crowd']
        movement={p['id']:sum((p['position'][i]-next(v for v in first['people'] if v['id']==p['id'])['position'][i])**2 for i in [0,1])**.5 for p in second['people']}
        rows.append({'case':'four_real_walking_people','pass':len(movement)==4 and all(d>.15 for d in movement.values()),'displacement_m':movement,'first':first,'second':second})
        (directory/'report.json').write_text(json.dumps(rows,ensure_ascii=False,indent=2))
        async with HubClient(record=directory/'hub.jsonl') as hub:
            for station in stations:
                s=await still();options=await asyncio.to_thread(api,'/api/mhs/routes',{'station_id':station})
                (directory/(station+'-options.json')).write_text(json.dumps(options,ensure_ascii=False,indent=2))
                if not options['routes']:raise RuntimeError('no_validated_routes:'+station)
                route=min(options['routes'],key=lambda r:(r['crowd_exposure_person_m'],r['length_m'])) if route_preference=='patient_friendly' else options['routes'][0]
                receipt=await hub.rpc('hub/call',{'device':'hospital-robot-01','tool':'navigate_route','arguments':{'route_id':route['route_id'],'operation_id':uuid.uuid4().hex}})
                if not receipt.get('accepted'):raise RuntimeError('MHS_route_not_accepted:'+str(receipt))
                result=await hub.finish(receipt['job'],timeout=900);s=await still()
                row={'case':'arrival_'+station,'pass':result['status']=='done' and station in s['at_stations'],'route':route,'result':result,'state':s}
                rows.append(row);(directory/'report.json').write_text(json.dumps(rows,ensure_ascii=False,indent=2));print(json.dumps({'case':row['case'],'pass':row['pass']}),flush=True)
                if not row['pass']:raise RuntimeError('real_arrival_failed:'+station)
    except Exception as error:
        rows.append({'case':'qualification_error','pass':False,'error':str(error)});print('ERROR',str(error),flush=True)
        try:await asyncio.to_thread(api,'/api/mhs/stop',{'reason':'v2_qualification_failure'})
        except Exception:pass
    finally:
        record.stop();(directory/'report.json').write_text(json.dumps(rows,ensure_ascii=False,indent=2))
        try:(directory/'final-state.json').write_text(json.dumps(await asyncio.to_thread(api),ensure_ascii=False,indent=2))
        except Exception as error:(directory/'final-state-error.txt').write_text(str(error))
        print('EVIDENCE',directory,flush=True)
    return 1 if any(not row['pass'] for row in rows) else 0

if __name__=='__main__':
    parser=argparse.ArgumentParser()
    parser.add_argument('--stations',nargs='+',default=['station_c','lab','room302','emergency','depot','station_a','station_b','pharmacy','waiting','room301'])
    parser.add_argument('--route-preference',choices=['shortest','patient_friendly'],default='shortest')
    args=parser.parse_args()
    raise SystemExit(asyncio.run(main(args.stations,args.route_preference)))
