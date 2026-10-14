"""Occupancy and real Nav2 path checks; neither commands robot movement."""
import argparse
import hashlib
import json
import time
from pathlib import Path
from PIL import Image, ImageFilter
from .layout import station_digest, validate_layout
from .routes import Routes


def main(nav2=False):
    root=Path(__file__).resolve().parents[1]
    stations=json.loads((root/'configs/stations.json').read_text())
    hospital=json.loads((root/'configs/hospital-v2.json').read_text())
    validate_layout(stations,hospital)
    routes=Routes(root)
    image=Image.open(root/'maps/carter_hospital_navigation.png').convert('L')
    mask=image.point(lambda p:255 if p>240 else 0).filter(ImageFilter.MinFilter(29))
    directory=root/'runs/mhs-v2/layout-check'/time.strftime('%Y%m%d-%H%M%S')
    directory.mkdir(parents=True,exist_ok=False)
    proof={'version':hospital['version'],'station_geometry_sha256':station_digest(stations),
           'scope':'0.70m occupancy clearance and actual Nav2 path computation; NOT physical arrival or Agent delivery',
           'source_sha256':{p:hashlib.sha256((root/p).read_bytes()).hexdigest() for p in ['configs/stations.json','configs/hospital-v2.json','maps/carter_hospital_navigation.png','mhs/layout.py','mhs/routes.py']},
           'points':{}}
    for ident,point in stations.items():
        px=round((point['x']-routes.origin[0])/routes.resolution)
        py=image.height-1-round((point['y']-routes.origin[1])/routes.resolution)
        free=0<=px<image.width and 0<=py<image.height and mask.getpixel((px,py))>240
        cells=routes.search(routes.snap([0,0]),routes.snap([point['x'],point['y']]),'shortest',{'people':[]},[],{})
        row={'clearance_070m':free,'connected_to_entrance':cells is not None,'nav2_path':'not_run'}
        if nav2:
            from .manage import api
            try:
                result=api('/api/mhs/routes',{'station_id':ident})
                (directory/(ident+'-paths.json')).write_text(json.dumps(result,ensure_ascii=False,indent=2))
                row.update(nav2_path='passed' if result.get('routes') or result.get('already_at_destination') else 'failed',route_count=len(result.get('routes',[])),rejected=result.get('rejected',[]))
            except Exception as error:
                row.update(nav2_path='failed',error=str(error))
        row['pass']=bool(free and cells is not None and (not nav2 or row['nav2_path']=='passed'))
        proof['points'][ident]=row
        (directory/'report.json').write_text(json.dumps(proof,ensure_ascii=False,indent=2))
        print(json.dumps({'point':ident,**row}),flush=True)
    proof['pass']=all(p['pass'] for p in proof['points'].values())
    (directory/'report.json').write_text(json.dumps(proof,ensure_ascii=False,indent=2))
    print('LAYOUT_CHECK',directory,'PASS',proof['pass'],flush=True)
    return 0 if proof['pass'] else 1


if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--nav2',action='store_true')
    raise SystemExit(main(parser.parse_args().nav2))
