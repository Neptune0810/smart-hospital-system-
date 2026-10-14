"""Normal AGH package/MCP CLI, with confirmation limited to reviewed project sources."""
import os,pty,select,subprocess,sys,time
from .manage import CLI,ENV,ROOT

def call(args,timeout=120):
    approved_sources={'file:./mhs/policy','file:./agent/agh-source-mhs/packages/mhs/plugin'}
    confirm=(args[:2]==['package','add'] and args[2] in approved_sources) or (args[:2] in [['package','trust'],['package','enable']] and args[2] in ['@agnes/mhs','hospital-mhs-policy'])
    confirm=confirm or (args[:3]==['mcp','add','hospital-business'] and '--stdio' in args and args[args.index('--stdio')+1]==str(ROOT/'mhs/mcp.sh')) or (args[:3] in [['mcp','trust','hospital-business'],['mcp','test','hospital-business']] and '--expected-revision' in args)
    master,slave=pty.openpty()
    p=subprocess.Popen(CLI+args,env=ENV,cwd=ROOT,stdin=slave,stdout=slave,stderr=slave)
    os.close(slave);out='';answered=False;started=time.monotonic()
    while time.monotonic()-started<timeout:
        if select.select([master],[],[],.2)[0]:
            try:
                chunk=os.read(master,65536).decode(errors='replace');out+=chunk
                print(chunk,end='',flush=True)
                if confirm and '[y/N]' in out and not answered:
                    os.write(master,b'y\n');answered=True
            except OSError:break
        if p.poll() is not None:break
    if p.poll() is None:p.terminate()
    code=p.wait();os.close(master)
    if code:raise RuntimeError('AGH_resource_exit:'+str(code))
    return out

if __name__=='__main__':call(sys.argv[1:])
