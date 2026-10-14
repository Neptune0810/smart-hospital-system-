"""Passive evidence capture for one Web-created delivery run."""
import json,time,signal,sys
from .manage import ROOT,api
from .record import Recorder
stop=False
def interrupt(*_):
    global stop
    stop=True

def main():
    label=sys.argv[1] if len(sys.argv)>1 else 'web'
    directory=ROOT/'runs/mhs-v2/acceptance'/(label+'-'+time.strftime('%Y%m%d-%H%M%S'))
    directory.mkdir(parents=True);rec=Recorder(directory).start()
    (ROOT/'logs/mhs/observer-current.json').write_text(json.dumps({'directory':str(directory),'label':label}))
    signal.signal(signal.SIGTERM,interrupt);signal.signal(signal.SIGINT,interrupt)
    end=time.monotonic()+1250;begun=False;last=None
    try:
        while not stop and time.monotonic()<end:
            s=api();begun=begun or bool(s['orders'])
            key=(s['latest_event_id'],s.get('navigation',{}).get('status') if s.get('navigation') else None,s.get('mhs_finished'))
            if key!=last:
                with (directory/'state.jsonl').open('a') as f:f.write(json.dumps({'observed_wall':time.time(),'state':s},ensure_ascii=False)+'\n')
                last=key
            if begun and (s.get('mhs_finished') or (s.get('mhs_budget') or {}).get('stopped_reason')):break
            time.sleep(1)
        (directory/'report.json').write_text(json.dumps(api('/api/report'),ensure_ascii=False,indent=2))
    finally:
        rec.stop();print('EVIDENCE',directory,flush=True)

if __name__=='__main__':main()
