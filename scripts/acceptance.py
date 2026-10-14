#!/usr/bin/env python3
import json,time,uuid,traceback
from pathlib import Path
from demo import nav,tool,request
from experiment import scene_reset,error_probe,put
ROOT=Path(__file__).resolve().parents[1]
gates={'navigation':{'status':'running'},'delivery':{'status':'pending'},'agent':{'status':'pending'},'recovery':{'status':'pending'}}
def save():put(ROOT/'evidence/acceptance.json',gates)
def main():
    scene_reset();save()
    try:
        for cycle in range(3):
            for station in ['station_a','station_b','station_c','depot']:
                nav(station);gates['navigation'].update(cycles=cycle,latest_station=station);save()
            gates['navigation']['cycles']=cycle+1;save()
        n=tool('navigate_to',station_id='station_c');time.sleep(2);tool('cancel_navigation',nav_task_id=n['nav_task_id'])
        deadline=time.time()+30
        while time.time()<deadline:
            status=tool('get_state')['navigation']['status']
            if status in ['canceled','failed','succeeded']:break
            time.sleep(.5)
        else:raise RuntimeError('cancel_not_confirmed')
        if status!='canceled':raise RuntimeError('cancel_not_canceled:'+status)
        nav('station_a');nav('depot')
        gates['navigation'].update(status='passed',cancel_terminal=status,retarget_verified=True);save()
        gates['delivery']['status']='running';save();request('/api/admin/reset',{})
        s=tool('get_state')
        orders=[dict(order_id='D'+str(i),package_id='P'+str(i),pickup_station='depot',dropoff_station=station,priority='normal',deadline_sim=s['sim_time']+300) for i,station in enumerate(['station_a','station_b'],1)]
        op=uuid.uuid4().hex
        payload={'name':'create_orders','arguments':{'orders':orders,'operation_id':op}}
        first=request('/api/tool',payload);second=request('/api/tool',payload)
        if first!=second:raise RuntimeError('idempotent_create_failed')
        for o in orders:tool('pickup_package',order_id=o['order_id'])
        probes=[error_probe('confirm_delivery',order_id='D1',package_id='P1'),error_probe('confirm_delivery',order_id='D1',package_id='WRONG')]
        for o in orders:
            nav(o['dropoff_station']);receipt=tool('confirm_delivery',order_id=o['order_id'],package_id=o['package_id'])
            repeat=tool('confirm_delivery',order_id=o['order_id'],package_id=o['package_id'])
            if receipt['order']['receipt']!=repeat['receipt']:raise RuntimeError('duplicate_receipt_mismatch')
        nav('depot');report=tool('verify_run')
        if not all(v['passed'] for v in probes) or report['delivered']!=2 or report['error_deliveries'] or report['unfinished']:raise RuntimeError('delivery_gate_failed')
        gates['delivery'].update(status='passed',probes=probes,report=report);save()
        print('NAVIGATION_AND_DELIVERY_PASSED',flush=True)
    except Exception as e:
        key='delivery' if gates['navigation']['status']=='passed' else 'navigation'
        gates[key].update(status='failed',error=str(e),traceback=traceback.format_exc());save();raise
if __name__=='__main__':main()
