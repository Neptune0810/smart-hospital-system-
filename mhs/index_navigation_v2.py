"""Publish evidence-backed station status; original attempts are immutable."""
import json,time
from .manage import ROOT
from .layout import station_digest

def main():
    stations=json.loads((ROOT/'configs/stations.json').read_text())
    points={k:{'navigation_status':'not_run','arrival_evidence':None,'latest_attempt':None} for k in stations}
    for path in sorted((ROOT/'runs/mhs-v2/qualification').glob('*/report.json')):
        for row in json.loads(path.read_text()):
            name=row.get('case','')
            if not name.startswith('arrival_') or name[8:] not in points:continue
            previous=row.get('state',{}).get('stations',{}).get(name[8:],{})
            current=stations[name[8:]]
            # A renamed/moved ID is not evidence that its new position was reached.
            if any(abs(previous.get(k,float('inf'))-current[k])>1e-5 for k in ['x','y','yaw']):continue
            point=points[name[8:]];point['latest_attempt']={'report':str(path.relative_to(ROOT)),'passed':row.get('pass'),'reason':row.get('state',{}).get('navigation',{}).get('reason') or row.get('error')}
            if row.get('pass') is True:point['arrival_evidence']=str(path.relative_to(ROOT))
            point['navigation_status']='arrival_verified' if row.get('pass') is True else 'last_attempt_failed'
    result={'updated_wall':time.time(),'station_geometry_sha256':station_digest(stations),'scope':'Actual MHS/Nav2 arrivals matching current coordinates; no Agent delivery claim or guarantee of future arrivals','points':points}
    p=ROOT/'configs/navigation-qualification-v2.json';tmp=p.with_suffix('.tmp');tmp.write_text(json.dumps(result,ensure_ascii=False,indent=2));tmp.replace(p)
    print(json.dumps(result,ensure_ascii=False))

if __name__=='__main__':main()
