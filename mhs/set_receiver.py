"""Operator-only scenario injection, never exposed to the Agent."""
import json,sys,time
from .manage import ROOT,api

def main():
    station,availability=sys.argv[1:]
    if station not in {'station_a','station_b','station_c'} or availability not in {'on','off'}:raise ValueError('named_receiver_and_on_off_required')
    before=api();result=api('/api/admin/receiver',{'station_id':station,'available':availability=='on'});after=api()
    record={'wall_time':time.time(),'station':station,'available':availability=='on','run_id':before['run_id'],'before':before,'result':result,'after':after,'source':'explicit operator acceptance injection, not an Agent tool'}
    with (ROOT/'runs/mhs-v2/operator-receiver.jsonl').open('a') as f:f.write(json.dumps(record,ensure_ascii=False)+'\n')
    print(json.dumps({'run_id':after['run_id'],'station':station,'available':availability=='on','sim_time':after['sim_time'],'event':after['latest_event_id']}))
if __name__=='__main__':main()
