"""Read-only continuous IRA walking proof; preserve short-window failures."""
import hashlib,json,math,time
from .manage import ROOT,api

def main():
    folder=ROOT/'runs/mhs-v2/diagnostics'/('people-path-'+time.strftime('%Y%m%d-%H%M%S'))
    folder.mkdir(exist_ok=False)
    first=api();start=time.monotonic();samples=[];last=None;distance={};previous={}
    protocol={'run_id':first['run_id'],'seed':first['crowd']['seed'],'mode':first['crowd']['mode'],'required_sim_span':5,'wall_limit':60,'minimum_cumulative_distance_m':.5,'scope':'Read-only actual IRA trajectories, not Agent acceptance','sha256':{name:hashlib.sha256((ROOT/name).read_bytes()).hexdigest() for name in ['mhs/crowd_v2.py','mhs/qualify_v2_people.py']}}
    (folder/'protocol.json').write_text(json.dumps(protocol,indent=2))
    while time.monotonic()-start<60:
        state=api();crowd=state['crowd']
        if state['run_id']!=first['run_id']:raise RuntimeError('run_changed')
        if not crowd['fresh'] or len(crowd['people'])!=4:raise RuntimeError('four_fresh_actual_people_required')
        if crowd['wall_time']!=last:
            for person in crowd['people']:
                ident=person['id'];point=person['position'][:2]
                distance[ident]=distance.get(ident,0)+(math.dist(previous[ident],point) if ident in previous else 0)
                previous[ident]=point
            samples.append(crowd);last=crowd['wall_time']
            with (folder/'samples.jsonl').open('a') as out:out.write(json.dumps(crowd)+'\n')
        if samples[-1]['sim_time']-samples[0]['sim_time']>=5:break
        time.sleep(.5)
    span=samples[-1]['sim_time']-samples[0]['sim_time']
    result={'pass':span>=5 and len(distance)==4 and all(d>=.5 for d in distance.values()),'sim_span':span,'samples':len(samples),'distance_m':distance,'source':'IRA_actual_character_runtime','no_scene_or_motion_changes':True,'prior_short_window_failure_preserved':True}
    (folder/'report.json').write_text(json.dumps(result,indent=2));print(json.dumps({'directory':str(folder),**result}))

if __name__=='__main__':main()
