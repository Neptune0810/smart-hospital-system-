"""Bounded backend diagnostic helper; never counts as webpage click acceptance."""
import argparse,json,uuid
from .manage import api,ROOT
from .workbench_check import facade
def main():
    p=argparse.ArgumentParser();p.add_argument('action',choices=['normal','urgent','pause','resume','stop','reset','status']);a=p.parse_args()
    if a.action=='status':print(json.dumps(facade()[1],ensure_ascii=False));return
    s=api();body={'action':a.action,'operation_id':uuid.uuid4().hex,'expected_run_id':s['run_id'],'session_id':s.get('controller_session')}
    if a.action=='normal':body['stations']=['station_a','station_c']
    if a.action=='urgent':body['station_id']='station_b'
    if a.action in {'pause','resume'}:body.update(action='receiver',station_id='station_a',available=a.action=='resume')
    status,result=facade('/api/demo/command',body)
    folder=ROOT/'runs/mhs-v2'/s['run_id'];folder.mkdir(exist_ok=True)
    with (folder/'backend-diagnostic-commands.jsonl').open('a') as f:f.write(json.dumps({'request':body,'response':result,'status':status,'source':'backend diagnostic, not browser'},ensure_ascii=False)+'\n')
    print(json.dumps(result,ensure_ascii=False));
    if status!=200:raise SystemExit(1)
if __name__=='__main__':main()
