"""Bounded operator-only outage/restoration; observes, never prompts the Agent."""
import json,math,subprocess,sys,time
from .manage import ROOT,api,CLI,ENV

def main():
    session=sys.argv[1]
    directory=ROOT/'runs/mhs-v2/acceptance'/('recovery-injection-'+time.strftime('%Y%m%d-%H%M%S'))
    directory.mkdir(parents=True)
    result={'session':session,'source':'operator-only controlled scenario injection'}
    def save(): (directory/'report.json').write_text(json.dumps(result,ensure_ascii=False,indent=2))
    end=time.monotonic()+500
    try:
        while time.monotonic()<end:
            s=api();nav=s.get('navigation') or {};pose=s.get('pose') or {}
            if nav.get('goal_station')=='station_a' and nav.get('status')=='running' and pose.get('speed',0)>.1 and math.hypot(pose['x']+5,pose['y']-10)<3:
                result['before_outage']=s;api('/api/admin/receiver',{'station_id':'station_a','available':False});result['outage']=api();save();break
            time.sleep(.5)
        else:raise RuntimeError('no_actual_A_approach')
        while time.monotonic()<end:
            s=api()
            if s['sim_time']-result['outage']['sim_time']>=120:raise RuntimeError('native_watch_not_observed_before_restoration_deadline')
            subprocess.run(CLI+['export',session,'--format','agnes','--raw','-o',str(directory/'before-restoration-session.jsonl')],env=ENV,cwd=ROOT,check=True,capture_output=True,text=True)
            rows=[json.loads(line) for line in (directory/'before-restoration-session.jsonl').read_text().splitlines()]
            accepted={row['data'].get('toolUseId') for row in rows if row.get('type')=='tool/result' and row['data'].get('isError') is not True}
            watches=[row for row in rows if row.get('type')=='tool/call' and row['data'].get('name')=='watch_device' and row['data'].get('toolUseId') in accepted and row['data'].get('args',{}).get('until',{}).get('state') in {'receiver_a','event_version'}]
            if watches:
                result['native_watches']=watches;result['watch_observed_state']=s;save();break
            time.sleep(5)
        else:raise RuntimeError('native_watch_not_observed')
        restore_at=api()['sim_time']+15
        while api()['sim_time']<restore_at:time.sleep(.5)
        api('/api/admin/receiver',{'station_id':'station_a','available':True});result['restoration']=api();result['pass']=True;save()
    except Exception as error:
        result.update(pass_=False,error=str(error));result['pass']=False
        # Explicitly preserve an unsuccessful scenario rather than hiding it.
        api('/api/admin/receiver',{'station_id':'station_a','available':True});result['restoration_after_test_error']=api();save()
    print(json.dumps({'directory':str(directory),'pass':result['pass'],'error':result.get('error')}),flush=True)
if __name__=='__main__':main()
