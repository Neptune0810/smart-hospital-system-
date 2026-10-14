import assert from 'node:assert/strict';
import {queuedTurnTerminal} from './notification-terminal.js';
const queued={notification_id:'n1',queue_seq:10};
const content=[{type:'text',text:'[设备事件] 一个已登记需求'}];
const stamp=wall=>new Date(wall*1000).toISOString();
function fixture(reason='error'){
 return [{seq:10,ts:stamp(1001),type:'inbox',data:{items:[{itemId:'item1',commandId:'n1',kind:'follow_up',target:'next-turn',content}]}},
  {seq:11,ts:stamp(1002),type:'turn/end',data:{reason:'error'}},
  {seq:12,ts:stamp(1003),type:'inbox',data:{items:[]}},
  {seq:13,ts:stamp(1003),type:'user/message',data:{kind:'follow_up',content}},
  {seq:14,ts:stamp(1003),type:'turn/start',data:{turn:2}},
  {seq:15,ts:stamp(1004),type:'turn/end',data:{reason}}].map(row=>({...row,lane_hex:'6D61696E',origin:row.type==='user/message'?'principal':'system',trust:'trusted'}));
}
assert.equal(queuedTurnTerminal(fixture(),queued,1000).terminal_seq,15);
assert.equal(queuedTurnTerminal(fixture().reverse(),queued,1000).turn_start_seq,14);
assert.equal(queuedTurnTerminal(fixture('completed'),queued,1000).reason,'completed');
assert.equal(queuedTurnTerminal(fixture().slice(0,2),queued,1000),null,'pre-existing turn error is not attributable');
for(const change of [
 rows=>rows[0].data.items[0].commandId='another',
 rows=>rows[0].ts=stamp(999),
 rows=>rows[0].lane_hex='617578',
 rows=>rows[3].origin='system',
 rows=>rows[3].trust='untrusted',
 rows=>rows[3].data.content=[{type:'text',text:'a different message'}],
 rows=>rows[3].data.kind='prompt',
 rows=>rows[3].seq=13.5,
 rows=>rows[4].seq=14.5,
 rows=>rows.splice(2,1),
 rows=>rows[3]={seq:13,ts:stamp(1003),type:'inbox',data:{items:[]}},
 rows=>rows[5]={seq:15,ts:stamp(1004),type:'turn/start',data:{turn:3}}
]){
 const rows=structuredClone(fixture());change(rows);
 assert.equal(queuedTurnTerminal(rows,queued,1000),null,'unknown queue/consumption/turn identity must not be guessed');
}
{
 const rows=structuredClone(fixture());
 // The operator removed the queued input (seq 12), then submitted an identical
 // new follow-up. Its independent claim must never be attributed to the old ACK.
 rows.splice(3,0,{seq:13,ts:stamp(1003),type:'x/core/queued-input-removed',data:{itemId:'item1'}});
 for(const row of rows.slice(4))row.seq+=2;
 assert.equal(queuedTurnTerminal(rows,queued,1000),null,'removed input followed by identical new input is not consumption');
}
const rows=fixture();rows.push({seq:16,ts:stamp(1005),type:'turn/start',data:{turn:3},lane_hex:'6D61696E'},{seq:17,ts:stamp(1006),type:'turn/end',data:{reason:'completed'},lane_hex:'6D61696E'});
assert.equal(queuedTurnTerminal(rows,queued,1000).terminal_seq,15,'later native completion cannot erase queued failure');
console.log('PASS queued terminal ownership: ACK command identity, consumption, correct turn, time boundary and normal completion');
