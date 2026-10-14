"""Bounded, read-only wall-time health sampling."""
import json,time
from .manage import ROOT,api
folder=ROOT/'runs/mhs-v2/diagnostics'/('health-'+time.strftime('%Y%m%d-%H%M%S'));folder.mkdir(parents=True)
rows=[]
for _ in range(20):
    s=api();rows.append({'wall_time':time.time(),'sim_time':s['sim_time'],'pose_fresh':s['pose_fresh'],'navigation_ready':s['navigation_ready'],'crowd_fresh':s['crowd']['fresh'],'navigation_health':s['navigation_health']});time.sleep(1)
report={'rows':rows,'pass':all(r['pose_fresh'] and r['navigation_ready'] and r['crowd_fresh'] for r in rows),'source':'actual project API snapshots'}
(folder/'report.json').write_text(json.dumps(report,indent=2));print(json.dumps({'directory':str(folder),'pass':report['pass'],'max_lifecycle_age_wall_seconds':max(r['navigation_health']['lifecycle_age_wall_seconds'] for r in rows),'sim_seconds':rows[-1]['sim_time']-rows[0]['sim_time']}))
raise SystemExit(0 if report['pass'] else 1)
