"""Index immutable V2 attempts; never merge failed trials into later passes."""
import json,time
from .manage import ROOT,api,process

def main():
    base=ROOT/'runs/mhs-v2';attempts=[]
    reports=set((base/'qualification').glob('*/report.json'))|{p.with_name('report.json') for p in (base/'qualification').glob('*/protocol.json')}
    for path in sorted(reports):
        rows=json.loads(path.read_text()) if path.exists() else [];protocol_path=path.parent/'protocol.json'
        protocol=json.loads(protocol_path.read_text()) if protocol_path.exists() else {}
        expected=protocol.get('cases') or ['four_real_walking_people',*['arrival_'+s for s in protocol.get('stations',[])]]
        present={r['case'] for r in rows}
        attempts.append({'directory':str(path.parent.relative_to(ROOT)),'started_wall':protocol.get('started_wall',protocol_path.stat().st_mtime if protocol_path.exists() else path.stat().st_mtime),'report_exists':path.exists(),'expected':expected,'passed':[r['case'] for r in rows if r.get('pass') is True],
                         'failed':[{'case':r['case'],'error':r.get('error') or r.get('result',{}).get('detail')} for r in rows if r.get('pass') is False],
                         'not_run_or_pending':[name for name in expected if name not in present or any(r['case']==name and r.get('pass') is None for r in rows)],
                         'scope':protocol.get('scope','See original report; not model acceptance'),'report':str(path.relative_to(ROOT))})
    attempts.sort(key=lambda row:row['started_wall'])
    try:state=api();health={key:state.get(key) for key in ['run_id','sim_time','navigation','pose_fresh','navigation_ready','stopped_confirmed','motion_guard','mhs_budget']}
    except Exception as error:health={'error':str(error)}
    result={'checked_wall':time.time(),'processes':{name:process(name) for name in ['sim','bridge','web','daemon','workbench']},'health':health,'qualification_attempts':attempts,
            'acceptance_reports':[str(p.relative_to(ROOT)) for p in sorted((base/'acceptance').rglob('report.json'))],
            'note':'Original attempts retained. Navigation checks, protocol checks and real Agent delivery are separate; pending or missing is not passed.'}
    path=base/('summary-'+time.strftime('%Y%m%d-%H%M%S')+'.json');path.write_text(json.dumps(result,ensure_ascii=False,indent=2))
    print(json.dumps({'file':str(path),'attempt_count':len(attempts),'latest_attempt':attempts[-1] if attempts else None}))

if __name__=='__main__':main()
