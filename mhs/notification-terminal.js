// The caller supplies only this controller's bounded ledger window. Never infer
// ownership from "a failed turn happened after enqueue": that can be the turn
// which was already running when followUp was accepted.
export function queuedTurnTerminal(rows,queued,budgetStartWall){
  if(!Number.isSafeInteger(queued.queue_seq)||queued.queue_seq<1||!Number.isFinite(budgetStartWall))return null;
  const ordered=[...rows].sort((a,b)=>a.seq-b.seq);
  const accepted=ordered.find(row=>row.seq===queued.queue_seq&&row.type==='inbox');
  if(accepted?.lane_hex!=='6D61696E'||accepted.origin!=='system'||accepted.trust!=='trusted')return null;
  const item=accepted?.data?.items?.find(input=>input.commandId===queued.notification_id);
  if(!item?.itemId||item.kind!=='follow_up'||item.target!=='next-turn'||!Array.isArray(item.content)||!item.content.length)return null;
  const acceptedWall=Date.parse(accepted.ts)/1000;
  if(!Number.isFinite(acceptedWall)||acceptedWall<budgetStartWall)return null;
  const removed=ordered.find(row=>row.seq>accepted.seq&&row.type==='inbox'&&Array.isArray(row.data?.items)&&!row.data.items.some(input=>input.itemId===item.itemId));
  if(!removed)return null;
  // Pinned Core acceptInput writes these three rows in one transaction. A
  // removeQueued call also removes the item, but cannot supply this exact proof.
  const message=ordered.find(row=>row.seq===removed.seq+1&&row.type==='user/message');
  const boundary=ordered.find(row=>row.seq===removed.seq+2&&row.type==='turn/start');
  if(!message||message.origin!=='principal'||message.trust!=='trusted'||message.lane_hex!==accepted.lane_hex||message.data?.kind!=='follow_up'||!boundary||boundary.lane_hex!==accepted.lane_hex||JSON.stringify(message.data?.content)!==JSON.stringify(item.content))return null;
  const terminal=ordered.find(row=>row.seq>boundary.seq&&(row.type==='turn/start'||row.type==='turn/end'));
  if(!terminal||terminal.type!=='turn/end'||terminal.lane_hex!==accepted.lane_hex)return null;
  const terminalWall=Date.parse(terminal.ts)/1000;
  if(!Number.isFinite(terminalWall)||terminalWall<budgetStartWall)return null;
  return {notification_id:queued.notification_id,queue_seq:queued.queue_seq,item_id:item.itemId,
    consumed_inbox_seq:removed.seq,user_message_seq:message.seq,turn_start_seq:boundary.seq,
    terminal_seq:terminal.seq,reason:terminal.data?.reason??'unknown',terminal};
}
