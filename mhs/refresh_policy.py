import json,subprocess
from .manage import ROOT,HOME,ENV,CLI,MODE,SMART_MODE,stop_process,container,process
from .resource import call

def main():
    stage=ROOT/'runs/mhs-v2/workbench-catalog-stage'
    stage.parent.mkdir(parents=True,exist_ok=True)
    installed=json.loads((HOME/'profiles/local-dev/agnes-lock.json').read_text()).get('packages',{}).get('hospital-mhs-policy',{})
    version=str(installed.get('version','1.3.0'))
    stage.write_text('smart' if version.startswith('3.') and MODE==SMART_MODE else '10' if version.startswith(('2.','3.')) else '8' if version.startswith('1.2.') else '9')
    stop_process('bridge');stop_process('web')
    subprocess.run(CLI+['daemon','stop','--home',str(HOME),'--workspace',str(ROOT)],env=ENV,cwd=ROOT,check=True,timeout=45)
    p=HOME/'profiles/local-dev/profile.yaml';d=json.loads(p.read_text())
    d['presets']={'default':'standard','allowed':['standard']}
    d['packages']=[x for x in d.get('packages',[]) if x['id']!='hospital-mhs-policy']
    p.write_text(json.dumps(d,indent=2))
    bundler="const{createRequire}=require('node:module');const req=createRequire(process.cwd()+'/agent/agh-source-mhs/packages/base/package.json');req('esbuild').buildSync({entryPoints:['mhs/policy/index.js'],outfile:'mhs/policy/dist/index.mjs',bundle:true,platform:'node',format:'esm',target:'node24',nodePaths:[process.cwd()+'/agent/agh-source-mhs/packages/base/node_modules'],banner:{js:\"import{createRequire as __cr}from'node:module';const require=__cr(import.meta.url);\"}})"
    subprocess.run(['node','-e',bundler],cwd=ROOT,check=True)
    call(['package','inspect','file:./mhs/policy'])
    lock=json.loads((HOME/'profiles/local-dev/agnes-lock.json').read_text())
    if 'hospital-mhs-policy' in lock['packages']:
        call(['package','disable','hospital-mhs-policy'])
        call(['package','remove','hospital-mhs-policy'])
    call(['package','add','file:./mhs/policy'])
    operations=json.loads((HOME/'data/daemon/package-operations/operations.json').read_text())['operations']
    previews=[x['operation']['preview'] for x in operations if x['operation'].get('preview',{}).get('id')=='hospital-mhs-policy']
    preview=previews[-1]
    if preview['blockers']:raise RuntimeError('policy_preview_blocked')
    (ROOT/'logs/mhs/preview-policy-refresh.json').write_text(json.dumps(preview,indent=2))
    call(['package','trust','hospital-mhs-policy',preview['integrity'],preview['capabilityHash']])
    stage.unlink(missing_ok=True)
    call(['package','enable','hospital-mhs-policy'])
    if (container() or {}).get('State',{}).get('Running') and process('sim'):
        from .restart_workbench import main as restart
        restart()
    else:
        config=json.loads(p.read_text());config['presets']={'default':'hospital-mhs','allowed':['hospital-mhs']};config['packages']=[{'id':'hospital-mhs-policy','source':'file:./mhs/policy','enabled':True},{'id':'@agnes/mhs','source':'file:./agent/agh-source-mhs/packages/mhs/plugin','enabled':True}];p.write_text(json.dumps(config,indent=2))
        # Package operations may auto-start a temporary daemon on a random port.
        # Leave it stopped so manage.start creates the fixed 4195 endpoint.
        subprocess.run(CLI+['daemon','stop','--home',str(HOME),'--workspace',str(ROOT)],env=ENV,cwd=ROOT,check=True,timeout=45)
        stop_process('daemon')
        print('Policy installed; runtime stopped.')

if __name__=='__main__':main()
