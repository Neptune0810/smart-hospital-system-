"""Start an independently recorded trial at a verified, retained physical pose."""
import argparse,json,time,uuid
from .manage import ROOT,api
from .session_scope import revoke_previous
from .workbench_check import facade

def main():
    p=argparse.ArgumentParser();p.add_argument('--expected-run-id',required=True);args=p.parse_args()
    before=api()
    if before['run_id']!=args.expected_run_id:raise RuntimeError('run_changed')
    if not before['stopped_confirmed'] or not before['pose_fresh']:raise RuntimeError('fresh_measured_stop_required')
    if any(o['loaded'] for o in before['orders'].values()):raise RuntimeError('retained_cargo_cannot_be_reset_by_trial_helper')
    if before.get('navigation') and before['navigation']['status'] in {'pending','running','canceling'}:raise RuntimeError('active_navigation')
    if before['orders'] and not (before.get('operator_stopped') or before.get('mhs_finished') or before.get('mhs_outcome')):raise RuntimeError('preserve_and_stop_unfinished_run_first')
    folder=ROOT/'runs/mhs-v2/diagnostics'/('business-trial-reset-'+time.strftime('%Y%m%d-%H%M%S'));folder.mkdir(exist_ok=False)
    (folder/'before.json').write_text(json.dumps(before,ensure_ascii=False,indent=2))
    revoke_previous();new=api('/api/admin/reset',{})
    end=time.monotonic()+20
    while time.monotonic()<end:
        after=api()
        if after['pose_fresh'] and after['stopped_confirmed']:break
        time.sleep(.25)
    else:raise RuntimeError('fresh_stationary_pose_not_recovered')
    code,result=facade('/api/demo/command',{'action':'open','operation_id':uuid.uuid4().hex,'expected_run_id':new['run_id'],'session_id':None})
    if code!=200 or not result.get('ok'):raise RuntimeError('new_session_not_opened:'+str(result))
    (folder/'after.json').write_text(json.dumps(api(),ensure_ascii=False,indent=2))
    print(json.dumps({'previous_run':before['run_id'],'run_id':new['run_id'],'session':result['result']['new_session'],'physical_before':before['physical_pose'],'physical_after':after['physical_pose'],'scene_rebuilt':False,'scope':'Business metadata only; original run preserved, real position retained, no movement command'},ensure_ascii=False))

if __name__=='__main__':main()
