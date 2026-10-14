import pathlib,subprocess,json,time,hashlib,os
ROOT=pathlib.Path(__file__).resolve().parents[1]
SRC=ROOT/'agent/agh-source-mhs';OUT=ROOT/'agent/agh-runtime-mhs'
SHA='57d0c34b33527294042b7ab8a60d7c7c934e932e'
def main():
    shim=ROOT/'agent/mhs-bin';shim.mkdir(exist_ok=True)
    (shim/'pnpm').write_text('#!/usr/bin/env bash\nexec /usr/bin/corepack pnpm "$@"\n');(shim/'pnpm').chmod(0o755)
    os.environ['PATH']=str(shim)+':'+os.environ['PATH']
    actual=subprocess.check_output(['git','rev-parse','HEAD'],cwd=SRC,text=True).strip()
    if actual!=SHA:raise RuntimeError('source_commit_mismatch')
    manager=json.loads((SRC/'package.json').read_text())['packageManager']
    print('PINNED_SOURCE',actual,'PACKAGE_MANAGER',manager,flush=True)
    subprocess.run(['corepack','pnpm','install','--frozen-lockfile'],cwd=SRC,check=True)
    client=SRC/'packages/mhs/plugin/client/agnes.client.json'
    d=json.loads(client.read_text());d['client']['publicConfig']['hubUrl']='ws://127.0.0.1:4192/ws/hub';client.write_text(json.dumps(d,indent=2)+'\n')
    subprocess.run(['corepack','pnpm','--filter','@agnes/cli','build:local','--output-dir',str(OUT)],cwd=SRC,check=True)
    subprocess.run(['corepack','pnpm','--filter','@agnes/mhs','build:plugin'],cwd=SRC,check=True)
    bundler="const{createRequire}=require('node:module');const req=createRequire(process.cwd()+'/agent/agh-source-mhs/packages/base/package.json');req('esbuild').buildSync({entryPoints:['mhs/policy/index.js'],outfile:'mhs/policy/dist/index.mjs',bundle:true,platform:'node',format:'esm',target:'node24',nodePaths:[process.cwd()+'/agent/agh-source-mhs/packages/base/node_modules'],banner:{js:\"import{createRequire as __cr}from'node:module';const require=__cr(import.meta.url);\"}})"
    subprocess.run(['node','-e',bundler],cwd=ROOT,check=True)
    venv=ROOT/'agent/mhs-venv'
    if not (venv/'bin/pip').exists():
        bootstrap=ROOT/'agent/python-bootstrap'
        subprocess.run(['python3','-m','pip','install','--target',str(bootstrap),'virtualenv'],check=True)
        seed_env=dict(os.environ,PYTHONPATH=str(bootstrap))
        subprocess.run(['python3','-m','virtualenv','--app-data',str(ROOT/'agent/virtualenv-cache'),str(venv)],env=seed_env,check=True)
    subprocess.run([str(venv/'bin/pip'),'install','-e',str(SRC/'packages/mhs/python'),'mcp>=1.25,<2','pydantic'],check=True)
    freeze=subprocess.check_output([str(venv/'bin/pip'),'freeze'],text=True)
    (ROOT/'mhs/requirements.lock.txt').write_text(freeze)
    hashes={n:hashlib.sha256((OUT/n).read_bytes()).hexdigest() for n in ['agnes.mjs','worker.mjs','daemon.mjs']}
    manifest=dict(commit=SHA,package_manager=manager,hub_port=4192,web_port=4189,build_wall_time=time.time(),runtime_sha256=hashes,
        plugin_client_sha256=hashlib.sha256(client.read_bytes()).hexdigest())
    (ROOT/'mhs/build-manifest.json').write_text(json.dumps(manifest,indent=2))
    print('BUILD_COMPLETE',json.dumps(manifest),flush=True)
if __name__=='__main__':main()
