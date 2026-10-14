"""Read project runtime and preserve a compact, timestamped health snapshot."""
import argparse,json,time
from .manage import ROOT,api,process
from .proximity import people_gate

def main():
    parser=argparse.ArgumentParser();parser.add_argument('--brief',action='store_true');parser.add_argument('--compact',action='store_true');args=parser.parse_args()
    s=api();pose=s.get('pose');crowd=s.get('crowd',{})
    nearest,blocked=people_gate(pose,crowd.get('people',[])) if pose else (None,None)
    row={'checked_wall':time.time(),'processes':{k:process(k) for k in ['sim','bridge','web','daemon','workbench']},
         **{k:s.get(k) for k in ['run_id','sim_time','pose','physical_pose','pose_fresh','navigation_ready','navigation','stopped_confirmed','motion_clear','motion_guard','navigation_progress','mhs_budget','mhs_finished']},
         'nearest_person_m':nearest,'current_yield_gate':blocked,'crowd':crowd,
         'orders':s['orders'],'events':[{'kind':e['kind'],'wall_time':e.get('wall_time'),'sim_time':e.get('sim_time'),'reason':e.get('reason')} for e in s.get('events',[])[-12:]]}
    folder=ROOT/'runs/mhs-v2/diagnostics';folder.mkdir(exist_ok=True)
    p=folder/('health-'+time.strftime('%Y%m%d-%H%M%S')+'.json');p.write_text(json.dumps(row,ensure_ascii=False,indent=2))
    output=dict(row,evidence=str(p.relative_to(ROOT)))
    if args.brief or args.compact:
        output['crowd']={k:crowd.get(k) for k in ['fresh','seed','mode','source']}
        output['orders']={k:{field:o.get(field) for field in ['pickup_station','dropoff_station','priority','status','loaded']} for k,o in s['orders'].items()}
    if args.compact:
        output={k:v for k,v in output.items() if k in {'checked_wall','run_id','sim_time','pose_fresh','navigation_ready','navigation','stopped_confirmed','navigation_progress','mhs_budget','mhs_finished','crowd','orders','evidence'}}
        output['physical_position']=(s.get('physical_pose') or {}).get('position')
        output['recent_events']=row['events'][-3:]
    print(json.dumps(output,ensure_ascii=False))

if __name__=='__main__':main()
