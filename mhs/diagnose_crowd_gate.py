"""Compare an original blocked interval with recorded real people positions."""
import argparse,bisect,json,math,time
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]

def main():
    parser=argparse.ArgumentParser();parser.add_argument('report');parser.add_argument('--station',default='station_b');args=parser.parse_args()
    path=(ROOT/args.report).resolve()
    if ROOT/'runs/mhs-v2/qualification' not in path.parents:raise ValueError('report_outside_project_qualification')
    rows=json.loads(path.read_text());case=next(r for r in rows if r['case']=='arrival_'+args.station)
    state=case['state'];events=state['events'];end=max(e['wall_time'] for e in events if e['kind']=='route_replan_required')
    start=max(e['wall_time'] for e in events if e['kind']=='patient_yield_started' and e['wall_time']<end)
    points=sorted(state['trajectory']);times=[p[0] for p in points];samples=[]
    with (ROOT/'runs/mhs-v2/crowd/positions.jsonl').open() as f:
        for line in f:
            row=json.loads(line)
            if not start<=row['wall_time']<=end:continue
            i=bisect.bisect_right(times,row['sim_time'])-1
            if i<0:continue
            p=points[i];nearest=min(math.hypot(p[1]-h['position'][0],p[2]-h['position'][1]) for h in row['people'])
            samples.append({'wall_time':row['wall_time'],'sim_time':row['sim_time'],'robot_xy':p[1:],'robot_sample_sim':p[0],'nearest_person_m':nearest,'clear_at_any_heading':nearest>1.9})
    result={'original_report':str(path.relative_to(ROOT)),'station':args.station,'blocked_start_wall':start,'replan_wall':end,'samples':len(samples),'nearest_person_range_m':[min(s['nearest_person_m'] for s in samples),max(s['nearest_person_m'] for s in samples)] if samples else None,'unconditional_clear_samples':[s for s in samples if s['clear_at_any_heading']],
            'method':'ROS map trajectory matched by simulation time against IRA actual people truth. Distance >1.9 m exceeds the entire original front rectangle (maximum radius 1.677 m), with sampling margin; heading is not inferred. Original report and source evidence retained.'}
    directory=ROOT/'runs/mhs-v2/diagnostics'/('crowd-gate-'+time.strftime('%Y%m%d-%H%M%S'));directory.mkdir(parents=True)
    (directory/'report.json').write_text(json.dumps(result,indent=2));(directory/'samples.json').write_text(json.dumps(samples))
    print(json.dumps({'directory':str(directory),'samples':len(samples),'clear_samples':len(result['unconditional_clear_samples']),'first_clear':next((s for s in samples if s['clear_at_any_heading']),None)}))

if __name__=='__main__':main()
