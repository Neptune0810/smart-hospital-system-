"""Operator scenario injection only; never sends an Agent continuation prompt."""
import json, math, sys, time
from .manage import ROOT, api

def main():
    directory=ROOT/'runs/mhs-v2/acceptance'/('permanent-injection-'+time.strftime('%Y%m%d-%H%M%S'))
    directory.mkdir(parents=True)
    result={'session':sys.argv[1], 'source':'operator-only permanent receiver outage'}
    def save(): (directory/'report.json').write_text(json.dumps(result,ensure_ascii=False,indent=2))
    end=time.monotonic()+1100
    try:
        while time.monotonic()<end:
            s=api(); nav=s.get('navigation') or {}; pose=s.get('pose') or {}
            if nav.get('goal_station')=='station_a' and nav.get('status')=='running' and pose.get('speed',0)>.1 and math.hypot(pose['x']+5,pose['y']-10)<3:
                result['before_outage']=s
                api('/api/admin/receiver',{'station_id':'station_a','available':False})
                result['outage']=api();save();break
            time.sleep(.5)
        else: raise RuntimeError('no_actual_loaded_A_approach')
        while time.monotonic()<end:
            s=api()
            failures=[o for o in s['orders'].values() if o['status']=='failed']
            if failures:
                result['terminal_failure_state']=s
                result['pass']=all(o.get('reason')=='receiver_unavailable_150_sim_seconds' for o in failures)
                # Preserve the exact business failure fields, rather than infer a receipt.
                result['failure_orders']=failures;save();break
            if (s.get('mhs_budget') or {}).get('stopped_reason'): raise RuntimeError('task_budget_before_receiver_terminal')
            time.sleep(1)
        else: raise RuntimeError('no_terminal_receiver_failure')
    except Exception as error:
        result.update({'pass':False,'error':str(error)});save()
    print(json.dumps({'directory':str(directory),'pass':result.get('pass'),'error':result.get('error')}),flush=True)
if __name__=='__main__': main()
