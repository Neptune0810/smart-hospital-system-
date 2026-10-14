import json,os,subprocess,signal,time
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
CLI=['node','/mnt/sda/omni-yezh/hospital-delivery-agent/agent/agh-runtime/agnes.mjs']
ENV=dict(os.environ,AGH_HOME=str(ROOT/'agent/agh-home'),AGNES_PROFILE='local-dev',AGNES_MCP_STDIO_ALLOWLIST=str(ROOT/'scripts/mcp.sh'))
def invoke(prompt,directory,session=None,timeout=90):
    directory=Path(directory);directory.mkdir(parents=True,exist_ok=True)
    index=max([int(x.stem.split('-')[1]) for x in directory.glob('turn-*.log')]+[-1])+1;log=directory/f'turn-{index:03d}.log'
    cmd=CLI+['-p',prompt,'--mode','json','--cwd',str(directory)]
    if session:cmd+=['--resume',session]
    else:cmd+=['--preset','hospital-delivery']
    started=time.time()
    with log.open('w') as f:
        p=subprocess.Popen(cmd,env=ENV,stdout=f,stderr=subprocess.STDOUT,start_new_session=True)
    try:p.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        os.killpg(p.pid,signal.SIGTERM);p.wait(timeout=10)
        raise TimeoutError('AGH_turn_timeout')
    result=None
    for line in log.read_text(errors='replace').splitlines():
        try:
            o=json.loads(line)
            if o.get('v')=='agnes-cli-result/v1':result=o
        except (ValueError,AttributeError):pass
    if not result:raise RuntimeError('AGH_result_missing:'+str(log))
    result['wall_seconds']=time.time()-started
    (directory/f'turn-{index:03d}.json').write_text(json.dumps(result,ensure_ascii=False,indent=2))
    if p.returncode or result.get('exitCode'):raise RuntimeError('AGH_exit:'+str(result.get('reason')))
    return result
def export(session,directory):
    f=Path(directory)/'agh-session.jsonl'
    subprocess.run(CLI+['export',session,'--format','agnes','--raw','-o',str(f)],env=ENV,check=True,capture_output=True,text=True)
