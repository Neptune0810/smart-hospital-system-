"""Build project-only UI bridge and SDK facade from the pinned official source."""
import hashlib,json,subprocess,time,sys
from .build_runtime import ROOT,SRC,OUT,SHA

BRIDGE='''
// Hospital workbench session bridge v1: metadata only, no parent-controlled RPC.
let hospitalLastSessionBridge = '';
setInterval(() => {
  if (window.parent === window) return;
  const message = {type:'hospital-session-v1',session_id:current?.id ?? null,connected};
  const serialized = JSON.stringify(message);
  // Heartbeats also let a freshly loaded parent learn the unchanged selection.
  hospitalLastSessionBridge = serialized;
  for (const origin of ['http://127.0.0.1:4193','http://127.0.0.1:4194'])
    window.parent.postMessage(message,origin);
},500);
'''

def main():
    actual=subprocess.check_output(['git','rev-parse','HEAD'],cwd=SRC,text=True).strip()
    if actual!=SHA:raise RuntimeError('source_commit_mismatch')
    app=SRC/'packages/web/src/app.ts';original=app.read_text()
    marker='// Hospital workbench session bridge v1:'
    if marker not in original:
        backup=ROOT/'mhs/native-app.original.ts';backup.write_text(original)
        app.write_text(original+BRIDGE)
    text=app.read_text()
    check="""  // Hospital V2: never submit robot work into a newly selected/unbound chat.
  const hospitalExpected = new URL(location.href).searchParams.get('session');
  if (window.parent !== window && hospitalExpected && current?.id !== hospitalExpected) {
    showError(new Error('这个聊天仅供查看，请用院递工作台的“回到本轮聊天”发送任务。'));
    return;
  }
"""
    if '// Hospital V2: never submit' not in text:
        anchor='function submitComposer(): void {\n'
        if anchor not in text:raise RuntimeError('native_composer_anchor_missing')
        text=text.replace(anchor,anchor+check,1)
    if '// Hospital V2 read-only composer' not in text:
        text+='''
// Hospital V2 read-only composer in the embedded workbench.
setInterval(() => {
  const expected = new URL(location.href).searchParams.get('session');
  if (window.parent === window || !expected) return;
  const readonly = current?.id !== expected;
  for (const field of document.querySelectorAll<HTMLTextAreaElement>('#composer textarea, textarea#input')) field.readOnly = readonly;
},500);
'''
    if '// Hospital V2 composer controls' not in text:
        text=text.replace('  composerRuntime.render(composerView)', '''  // Hospital V2 composer controls preserve drafts while denying unbound submissions.
  const hospitalExpectedComposer = new URL(location.href).searchParams.get('session');
  if(window.parent !== window && hospitalExpectedComposer && current?.id !== hospitalExpectedComposer) {
    composerView.send.disabled = true;
    composerView.send.label = '仅查看 · 回到本轮聊天';
  }
  composerRuntime.render(composerView)''',1)
    app.write_text(text)
    if '--gateway-only' not in sys.argv:
        subprocess.run(['corepack','pnpm','--filter','@agnes/cli','build:local','--output-dir',str(OUT)],cwd=SRC,check=True)
    bundler="""const{createRequire}=require('node:module');const req=createRequire(process.cwd()+'/agent/agh-source-mhs/packages/base/package.json');req('esbuild').buildSync({entryPoints:['mhs/workbench.js'],outfile:'mhs/dist/workbench.mjs',bundle:true,platform:'node',format:'esm',target:'node24',nodePaths:[process.cwd()+'/agent/agh-source-mhs/packages/base/node_modules'],banner:{js:\"import{createRequire as __cr}from'node:module';const require=__cr(import.meta.url);\"}})"""
    subprocess.run(['node','-e',bundler],cwd=ROOT,check=True)
    paths=['mhs/workbench.js','mhs/dashboard.js','mhs/dashboard-v2.js','mhs/dashboard.html','mhs/dist/workbench.mjs','agent/agh-runtime-mhs/agnes.mjs','agent/agh-runtime-mhs/daemon.mjs','agent/agh-source-mhs/packages/web/src/app.ts']
    manifest={'base_commit':SHA,'built_wall':time.time(),'patch':'project-local native session metadata bridge','sha256':{p:hashlib.sha256((ROOT/p).read_bytes()).hexdigest() for p in paths}}
    (ROOT/'mhs/workbench-build.json').write_text(json.dumps(manifest,indent=2))
    print(json.dumps(manifest),flush=True)
if __name__=='__main__':main()
