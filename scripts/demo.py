import urllib.request,json,time,uuid,argparse
def request(path,data=None):
 r=urllib.request.Request('http://127.0.0.1:4188'+path,data=json.dumps(data).encode() if data is not None else None,headers={'Content-Type':'application/json'})
 with urllib.request.urlopen(r,timeout=15) as f:
  v=json.load(f);return v.get('result',v)
def tool(name,**a):
 if name not in ['get_state','verify_run','wait_event']:a['operation_id']=uuid.uuid4().hex
 return request('/api/tool',{'name':name,'arguments':a})
def nav(station):
 v=tool('navigate_to',station_id=station);deadline=time.time()+300
 while time.time()<deadline:
  s=tool('get_state');n=s['navigation']
  if n and n['status'] in ['succeeded','failed','canceled']:
   if n['status']!='succeeded':raise RuntimeError(n)
   for settle in range(20):
    s=tool('get_state')
    if station in s['at_stations']:break
    time.sleep(.5)
   if station not in s['at_stations']:raise RuntimeError('arrival_verification_failed:'+station+':'+json.dumps({'ROS':s['pose'],'physical':s.get('physical_pose')}))
   print('ARRIVED',station,s['sim_time'],flush=True);return
  time.sleep(1)
 tool('cancel_navigation',nav_task_id=v['nav_task_id']);raise TimeoutError(station)
def main():
 deadline=time.time()+60
 while time.time()<deadline:
  try:
   state=tool('get_state')
   if state['navigation_ready'] and state['pose_fresh']:break
  except Exception:pass
  time.sleep(1)
 else:raise RuntimeError('navigation_preflight_timeout')
 parser=argparse.ArgumentParser();parser.add_argument('mode',choices=['normal','navigation','cancel']);args=parser.parse_args()
 if args.mode=='navigation':
  for i in range(3):
   for station in ['station_a','station_b','station_c','depot']:nav(station)
 elif args.mode=='cancel':
  n=tool('navigate_to',station_id='station_c');time.sleep(2);tool('cancel_navigation',nav_task_id=n['nav_task_id'])
  for i in range(30):
   if tool('get_state')['navigation']['status'] in ['canceled','succeeded','failed']:break
   time.sleep(1)
  else:raise RuntimeError('cancel_not_confirmed')
  nav('station_a');nav('depot')
 else:
  request('/api/admin/reset',{});nav('depot');s=tool('get_state')
  orders=[dict(order_id='D'+str(i),package_id='P'+str(i),pickup_station='depot',dropoff_station=st,priority='normal',deadline_sim=s['sim_time']+300) for i,st in enumerate(['station_a','station_b'],1)]
  tool('create_orders',orders=orders)
  for o in orders:tool('pickup_package',order_id=o['order_id'])
  for o in orders:nav(o['dropoff_station']);tool('confirm_delivery',order_id=o['order_id'],package_id=o['package_id'])
  nav('depot')
 print(json.dumps(tool('verify_run'),ensure_ascii=False,indent=2))

if __name__=="__main__":main()
