"""Create a separate profile and use normal inspect/add/trust/enable flows."""
import json,re
from .manage import ROOT,HOME,ENV,CLI
from .resource import call

def recorded(args):
    out=call(args)
    (ROOT/'logs/mhs'/('setup-'+args[0]+'-'+args[1]+'.log')).write_text(out)
    return out

def main():
    profile=HOME/'profiles/local-dev/profile.yaml'
    if not profile.exists():
        config=json.loads((ROOT/'agent/profile.template.json').read_text())
        config['presets']={'default':'standard','allowed':['standard']}
        config['packages']=[]
        profile.parent.mkdir(parents=True,exist_ok=True)
        profile.write_text(json.dumps(config,indent=2))
    for source,ident in [('file:./agent/agh-source-mhs/packages/mhs/plugin','@agnes/mhs'),('file:./mhs/policy','hospital-mhs-policy')]:
        preview=recorded(['package','inspect',source])
        lock=json.loads((HOME/'profiles/local-dev/agnes-lock.json').read_text())
        if ident not in lock['packages']:recorded(['package','add',source])
        operations=json.loads((HOME/'data/daemon/package-operations/operations.json').read_text())['operations']
        inspected=[x['operation']['preview'] for x in operations if x['operation'].get('preview',{}).get('id')==ident]
        checked=inspected[-1]
        if checked['blockers']:raise RuntimeError('package_preview_blocked:'+ident)
        (ROOT/'logs/mhs'/('preview-'+ident.replace('/','_')+'.json')).write_text(json.dumps(checked,indent=2))
        recorded(['package','trust',ident,checked['integrity'],checked['capabilityHash']])
        recorded(['package','enable',ident])
    listing=recorded(['mcp','list'])
    out=recorded(['mcp','get','hospital-business']) if 'hospital-business' in listing else recorded(['mcp','add','hospital-business','--name','Hospital business','--stdio',str(ROOT/'mhs/mcp.sh')])
    revision=re.search(r'revision=([a-f0-9]{64})',out).group(1)
    recorded(['mcp','trust','hospital-business','--expected-revision',revision])
    current=recorded(['mcp','get','hospital-business'])
    revision=re.search(r'revision=([a-f0-9]{64})',current).group(1)
    recorded(['mcp','test','hospital-business','--expected-revision',revision])
    # Generic open-world MCP rows stay disabled. The project plugin owns the
    # fixed closed-world catalog and calls the same service through real MCP.
    config=json.loads(profile.read_text());config['presets']={'default':'hospital-mhs','allowed':['hospital-mhs']}
    config['packages']=[{'id':'hospital-mhs-policy','source':'file:./mhs/policy','enabled':True},
        {'id':'@agnes/mhs','source':'file:./agent/agh-source-mhs/packages/mhs/plugin','enabled':True}]
    profile.write_text(json.dumps(config,indent=2))
    recorded(['package','status'])

if __name__=='__main__':main()
