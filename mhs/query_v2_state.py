"""Real Agnes read-only state query via official CLI, not browser acceptance."""
import hashlib,json,subprocess,time,uuid
from .manage import ROOT,CLI,HOME,ENV,api
from .workbench_check import facade

def main():
    s=api();directory=ROOT/'runs/mhs-v2/acceptance'/('state-cli-'+time.strftime('%Y%m%d-%H%M%S'));directory.mkdir(parents=True)
    code,opened=facade('/api/demo/command',{'action':'open','operation_id':uuid.uuid4().hex,'expected_run_id':s['run_id'],'session_id':None})
    if code!=200:raise RuntimeError('canonical_chat_open_failed')
    session=opened['result']['new_session'];prompt='现在机器人在哪里，有哪些取货和交接点？只告诉我当前状态，不要建单、改变安排或移动机器人。'
    protocol={'input_source':'official CLI, not webpage','session':session,'run_id':s['run_id'],'prompt':prompt,'started_wall':time.time(),
              'source_sha256':{p:hashlib.sha256((ROOT/p).read_bytes()).hexdigest() for p in ['mhs/mcp_server.py','mhs/policy/instructions.md','mhs/policy/package.json']}}
    (directory/'protocol.json').write_text(json.dumps(protocol,ensure_ascii=False,indent=2))
    error=None;exit_code=None
    try:
        with (directory/'cli.jsonl').open('w') as f:
            result=subprocess.run(CLI+['--profile','local-dev','--cwd',str(ROOT),'--resume',session,'-p',prompt,'--mode','json'],cwd=ROOT,env=ENV,stdin=subprocess.DEVNULL,stdout=f,stderr=subprocess.STDOUT,timeout=180)
            exit_code=result.returncode
    except subprocess.TimeoutExpired:error='CLI_timeout_180_wall_seconds'
    after=api();report={'input_source':protocol['input_source'],'cli_exit':exit_code,'error':error,'orders_unchanged':after['orders']==s['orders'],
                        'navigation_tasks_unchanged':set(after['navigation_tasks'])==set(s['navigation_tasks']),'wall_seconds':time.time()-protocol['started_wall'],'session':session}
    subprocess.run([str(ROOT/'agent/mhs-venv/bin/python'),'-m','mhs.export_session',session,str(directory)],cwd=ROOT,env=ENV,check=True)
    summary=json.loads((directory/'session-summary.json').read_text())
    rows=[json.loads(line) for line in (directory/'session.jsonl').read_text().splitlines()]
    new_requests=[r for r in rows if r.get('type')=='request/sent' and r['ts']>=time.strftime('%Y-%m-%dT%H:%M:%S',time.gmtime(protocol['started_wall']))]
    report.update(new_model_requests=len(new_requests),pass_=bool(exit_code==0 and new_requests and report['orders_unchanged'] and report['navigation_tasks_unchanged']))
    report['pass']=report.pop('pass_')
    (directory/'report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2))
    print('EVIDENCE',directory,flush=True)

if __name__=='__main__':main()
