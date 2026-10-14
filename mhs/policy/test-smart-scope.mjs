import assert from 'node:assert/strict';
process.env.HOSPITAL_MODE='smart-ward-v1';
const {allowTool}=await import('./scope.js');
const {completionFeedback,verificationKey}=await import('./completion.js');
for(const name of ['get_state','get_route_options','create_specimen_delivery','publish_service_plan','defer_order','close_order','verify_services','wait_event'])assert.equal(allowTool({name:'hospital_'+name}).allow,true,name);
for(const name of ['pickup_package','confirm_delivery','create_delivery_orders','publish_delivery_plan','verify_run'])assert.equal(allowTool({name:'hospital_'+name}).allow,false,name);
for(const [device,tool] of [['hospital-robot-01','navigate_route'],['hospital-cabinet-b','handoff_order'],['hospital-cabinet-c','handoff_order'],['hospital-cabinet-lab','handoff_order'],['hospital-light-301','set_brightness'],['hospital-light-303','set_brightness']]){
 assert.equal(allowTool({name:'call_device',args:{device,tool}}).allow,true);
 assert.equal(allowTool({name:'call_device',args:{device,tool:'drive'}}).allow,false);
}
for(const device of ['hospital-sensor-301','hospital-sensor-303']){
 assert.equal(allowTool({name:'read_device',args:{device}}).allow,true);
 assert.equal(allowTool({name:'call_device',args:{device,tool:'set_brightness'}}).allow,false);
}
assert.equal(allowTool({name:'read_device',args:{device:'another-sensor'}}).allow,false);
const state={run_id:'r',controller_session:'s',mhs_finished:true,stopped_confirmed:true,pose_fresh:true,motion_guard:{healthy:true},smart_ward:{watch:{enabled:true},scene_fresh:true,incidents:[{incident_id:'light-01',kind:'low_light',status:'resolved'}],episode:1,verification_event_version:9}};
assert.equal(completionFeedback(state,'s').terminalVerification,true);
assert.equal(completionFeedback(state,'s',true).action,'stop');
assert.equal(verificationKey(state),verificationKey({run_id:'r',episode:1,verification_event_version:9}));
state.mhs_finished=false;assert.equal(completionFeedback(state,'s').action,'continue');assert.equal(completionFeedback(state,'s').terminalVerification,undefined);
state.mhs_outcome='incomplete';state.smart_ward.recovery_required=true;assert.equal(completionFeedback(state,'s').terminalVerification,true);
state.smart_ward.recovery_required=false;state.mhs_outcome=undefined;
state.smart_ward.incidents[0].status='awaiting_verification';assert.equal(completionFeedback(state,'s').action,'continue');
assert.equal(completionFeedback(state,'s',false,new Set(['light-01'])).action,'stop','only an accepted current-attempt watch permits waiting');
state.smart_ward.incidents[0].status='new';assert.equal(completionFeedback(state,'s').action,'continue');
state.smart_ward.scene_fresh=false;assert.equal(completionFeedback(state,'s').action,'stop');
console.log('PASS smart exact tool/device scope and pure-light completion feedback');
