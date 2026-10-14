import json,time
from .manage import api
s=api();r=api('/api/report')
print(json.dumps({'wall':time.time(),'run_id':s['run_id'],'sim_time':s['sim_time'],'pose':s['pose'],
    'navigation':s['navigation'],'guard':s['motion_guard'],'budget':s.get('mhs_budget'),
    'delivered':r['delivered'],'total':r['total'],'unfinished':r['unfinished'],'failed':r['failed'],
    'returned':r.get('returned_to_depot'),'event':s['latest_event_id'],'finished':s.get('mhs_finished'),
    'loaded':sum(bool(o.get('loaded')) for o in s['orders'].values()),
    'orders':[{k:o.get(k) for k in ['order_id','dropoff_station','status','loaded','priority','deadline_sim']} for o in s['orders'].values()]},ensure_ascii=False))
