"""Project backend integration checks; distinct from the actual browser acceptance."""
import json,time,uuid,urllib.request,urllib.error
from .manage import ROOT,api

def facade(path='/api/demo/status',body=None,origin='http://127.0.0.1:4194'):
    req=urllib.request.Request('http://127.0.0.1:4194'+path,data=json.dumps(body).encode() if body else None,
        headers={'Content-Type':'application/json','Origin':origin})
    try:
        with urllib.request.urlopen(req,timeout=20) as f:return f.status,json.load(f)
    except urllib.error.HTTPError as e:return e.code,json.load(e)

def main():
    directory=ROOT/'runs/mhs-v2/acceptance'/('workbench-backend-'+time.strftime('%Y%m%d-%H%M%S'))
    directory.mkdir(parents=True,exist_ok=False)
    s=api();body={'action':'open','operation_id':uuid.uuid4().hex,'expected_run_id':s['run_id'],'session_id':None}
    forbidden=facade('/api/demo/command',body,'http://untrusted.invalid')
    assert forbidden[0]==403,forbidden
    opened=facade('/api/demo/command',body);assert opened[0]==200,opened
    duplicate=facade('/api/demo/command',body);assert duplicate==opened,duplicate
    session=opened[1]['result']['new_session'];assert session.startswith('agnes:local:local-dev:')
    status=facade('/api/demo/status?session='+session)
    assert status[1]['viewing_current'],status
    invalid=dict(body,action='urgent',operation_id=uuid.uuid4().hex,session_id='agnes:local:local-dev:cli:workspace:history',station_id='station_a')
    refused=facade('/api/demo/command',invalid);assert refused[0]==400,refused
    assert api()['orders']==s['orders']
    report={'origin_rejected':forbidden,'open':opened,'duplicate':duplicate,'history_rejected':refused,'session':session,'no_orders_created':True,'pass':True}
    (directory/'report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2))
    print(json.dumps({'directory':str(directory),'session':session,'pass':True}))
if __name__=='__main__':main()
