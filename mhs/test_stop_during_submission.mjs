import {readFileSync} from 'node:fs';
import vm from 'node:vm';
import assert from 'node:assert/strict';

// Keep an actual task HTTP response pending. Stop must submit independently
// and its acknowledgment must not erase the unresolved task's retry identity.
const source=readFileSync(new URL('./dashboard.js',import.meta.url),'utf8');
const functionText=source.slice(source.indexOf('async function submitDemo('),source.indexOf("demoId('demo-normal').onclick"));
const storage=new Map(),requests=[];
let finishTask;
const context=vm.createContext({
  commandBusy:false,stopBusy:false,latest:{run_id:'test-run'},viewedSession:'test-session',
  crypto:{randomUUID:()=>{throw Error('test must use frozen operation IDs');}},
  sessionStorage:{setItem:(key,value)=>storage.set(key,value),removeItem:key=>storage.delete(key)},
  updateDemo(){},notice(){},showSession(){throw Error('unexpected session switch');},
  AbortSignal,
  fetch:async(url,request)=>{
    const body=JSON.parse(request.body);requests.push(body);
    if(body.action==='stop')return {ok:true,json:async()=>({ok:true,result:{status:'stop_requested'}})};
    return new Promise(resolve=>{finishTask=()=>resolve({ok:true,json:async()=>({ok:true,result:{status:'sent',text:'test task'}})});});
  }
});
vm.runInContext(functionText,context);
const task=vm.runInContext("submitDemo('delivery',{},'task-1')",context);
assert.equal(context.commandBusy,true);
await vm.runInContext("submitDemo('stop',{},'stop-1')",context);
assert.deepEqual(requests.map(x=>x.action),['delivery','stop']);
assert.equal(JSON.parse(storage.get('hospital-demo-pending')).operation_id,'task-1');
assert.equal(storage.has('hospital-demo-pending-stop'),false);
assert.equal(context.commandBusy,true);
assert.equal(context.stopBusy,false);
finishTask();await task;
assert.equal(storage.size,0);
assert.equal(context.commandBusy,false);
console.log('PASS: stop sent before pending delivery response; retry identities isolated.');
