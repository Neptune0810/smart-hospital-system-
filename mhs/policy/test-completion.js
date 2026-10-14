import assert from 'node:assert/strict';
import {completionFeedback,acceptedLightWatches} from './completion.js';
const state={controller_session:'current',pose_fresh:true,navigation_ready:true,motion_guard:{healthy:true},orders:{o:{status:'onboard',dropoff_station:'station_c'}},receivers:{station_c:true},at_stations:['station_a'],stopped_confirmed:true};
assert.equal(completionFeedback(state,'current').action,'continue');
assert.equal(completionFeedback(state,'history').action,'stop');
assert.equal(completionFeedback({...state,operator_stopped:{}},'current').action,'stop');
assert.equal(completionFeedback({...state,navigation:{status:'running'}},'current').action,'stop');
assert.equal(completionFeedback({...state,orders:{o:{status:'deferred',dropoff_station:'station_c'}},receivers:{station_c:false}},'current').action,'stop');
assert.equal(completionFeedback({...state,orders:{o:{status:'delivered'}},at_stations:['station_c'],mhs_finished:true},'current',true).action,'stop');
assert.equal(completionFeedback({...state,orders:{o:{status:'delivered'}},at_stations:['depot'],mhs_finished:true},'current',true).action,'stop');
assert.equal(completionFeedback({...state,orders:{o:{status:'delivered'}},at_stations:['station_c'],mhs_finished:false,delivery_plan:{steps:[{action:'return',station_id:'depot'}]}},'current').action,'continue');
assert.equal(completionFeedback({...state,mhs_budget:{stopped_reason:'decision_limit'}},'current').action,'stop');
assert.equal(completionFeedback({...state,mhs_outcome:'incomplete'},'current',true).action,'stop');
assert.equal(completionFeedback({...state,mhs_outcome:'incomplete'},'current').terminalVerification,true);
assert.equal(completionFeedback({...state,mhs_finished:true},'current').terminalVerification,true);
assert.equal(completionFeedback({...state,mhs_outcome:'incomplete',stopped_confirmed:false},'current').action,'stop');
console.log('13 V2 completion checks passed');
const attemptWall=1000;
const wardState={controller_session:'current',pose_fresh:true,stopped_confirmed:true,motion_guard:{healthy:true},smart_ward:{watch:{enabled:true},scene_fresh:true,incidents:[{incident_id:'light-01',kind:'low_light',room_id:'room301',status:'awaiting_verification',attempt_start_wall:attemptWall}]}};
const watchRows=[{seq:10,ts:new Date((attemptWall+1)*1000).toISOString(),type:'tool/call',data:{name:'watch_device',toolUseId:'watch-01',args:{device:'hospital-sensor-301',until:{state:'service_done',eq:true}}}},
 {seq:11,type:'tool/result',data:{toolUseId:'watch-01',isError:false}}];
assert.equal(completionFeedback(wardState,'current').action,'continue','an ACK without a native wait must not strand verification');
const watches=acceptedLightWatches(watchRows,wardState,attemptWall+2);
assert.deepEqual([...watches],['light-01']);
assert.equal(completionFeedback(wardState,'current',false,watches).action,'stop','accepted native wait owns the next wake-up');
for(const change of [
 rows=>rows[1].data.isError=true,
 rows=>rows[0].data.args.device='hospital-sensor-303',
 rows=>rows[0].data.args.until={state:'recovered',eq:true},
 rows=>rows[0].data.args.until={state:'service_done',eq:1},
 rows=>rows[0].ts=new Date((attemptWall-1)*1000).toISOString(),
 rows=>rows[0].data.args.timeout=1,
 rows=>rows.pop(),
 rows=>rows.push({seq:12,type:'tool/call',data:{name:'unwatch_device',toolUseId:'unwatch-01'}},{seq:13,type:'tool/result',data:{toolUseId:'unwatch-01',isError:false}})
]){
 const rows=structuredClone(watchRows);change(rows);
 assert.equal(acceptedLightWatches(rows,wardState,attemptWall+2).size,0,'rejected, stale, expired, wrong or canceled waits cannot authorize idle');
}
const retry=structuredClone(wardState);retry.smart_ward.incidents[0].attempt_start_wall=attemptWall+5;
assert.equal(acceptedLightWatches(watchRows,retry,attemptWall+6).size,0,'an earlier adjustment wait cannot cover a retry');
for(const key of ['input','arguments']){
 const rows=structuredClone(watchRows);rows[0].data[key]=rows[0].data.args;delete rows[0].data.args;
 assert.equal(acceptedLightWatches(rows,wardState,attemptWall+2).size,1);
}
console.log('PASS light completion waits: accepted current-attempt registration, rejection, expiry, unwatch and retry boundaries');
