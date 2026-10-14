import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import vm from 'node:vm';

// Isolated checks: never claim or edit a live robot conversation.
const path='/mnt/sda/omni-yezh/hospital-delivery-agent/runs/mhs-v2/session-bindings.json';
let bindings={revoked:{run_id:'old',revoked:true}},identity={run_id:'run-1',controller_session:'current'},fail=false;
const writes=[];
const context=vm.createContext({
  Date,JSON,AbortSignal,
  readFileSync:(file)=>{assert.equal(file,path);return JSON.stringify(bindings);},
  writeFileSync:(file,raw)=>{assert.equal(file,path+'.tmp');bindings=JSON.parse(raw);writes.push(file);},
  renameSync:(from,to)=>{assert.equal(from,path+'.tmp');assert.equal(to,path);},
  fetch:async(url)=>{
    assert.equal(url,'http://127.0.0.1:4188/api/mhs/identity');
    return {ok:!fail,json:async()=>fail?{error:'identity unavailable'}:identity};
  }
});
const source=readFileSync(new URL('./session-scope.js',import.meta.url),'utf8').trimStart().replace(/^import[^\n]*\n/,'').replace('export async function','async function');
vm.runInContext(source,context);
const check=session=>context.checkSessionRun(session);
assert.equal((await check(null)).allow,false);
assert.equal((await check('revoked')).allow,false);
assert.equal((await check('history')).allow,false);assert.equal(writes.length,0);
assert.equal((await check('current')).allow,true);assert.equal(bindings.current.run_id,'run-1');
identity={...identity,run_id:'run-2'};
assert.equal((await check('current')).allow,false);assert.equal(writes.length,1);
fail=true;await assert.rejects(()=>check('current'),/project_identity_unavailable/);
console.log('PASS: missing, revoked, non-owner, changed-run and unavailable identity fail closed; live bindings untouched.');
