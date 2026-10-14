import json,urllib.request,urllib.error
from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations
mcp=FastMCP('Hospital Delivery')
def call(name,args):
    req=urllib.request.Request('http://127.0.0.1:4188/api/tool',data=json.dumps({'name':name,'arguments':args}).encode(),headers={'Content-Type':'application/json'})
    try:
        with urllib.request.urlopen(req,timeout=10) as f:result=json.load(f)
        if name in ['get_state','wait_event'] and isinstance(result.get('result'),dict):
            result['result'].pop('trajectory',None)
        return result
    except urllib.error.HTTPError as e:return json.load(e)
@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True,destructiveHint=False,idempotentHint=True,openWorldHint=False))
def get_state(since_event_id:int=0)->dict:
    """Read live robot pose, orders, capacity, navigation and receiver events."""
    return call('get_state',locals())
@mcp.tool(annotations=ToolAnnotations(readOnlyHint=False,destructiveHint=False,idempotentHint=True,openWorldHint=False))
def create_orders(orders:list[dict],operation_id:str)->dict:
    """Create validated orders with order_id, package_id, pickup_station=depot, dropoff_station, priority=normal|urgent and deadline_sim."""
    return call('create_orders',locals())
@mcp.tool(annotations=ToolAnnotations(readOnlyHint=False,destructiveHint=False,idempotentHint=True,openWorldHint=False))
def pickup_package(order_id:str,operation_id:str)->dict:
    """Load package only when stopped at depot and capacity permits."""
    return call('pickup_package',locals())
@mcp.tool(annotations=ToolAnnotations(readOnlyHint=False,destructiveHint=False,idempotentHint=True,openWorldHint=False))
def navigate_to(station_id:str,operation_id:str)->dict:
    """Start asynchronous Nav2 goal to depot/station_a/station_b/station_c. Poll state for completion."""
    return call('navigate_to',locals())
@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True,destructiveHint=False,idempotentHint=True,openWorldHint=False))
def wait_event(since_event_id:int=0,timeout_seconds:float=3)->dict:
    """Wait up to five wall seconds for real execution or order events."""
    return call('wait_event',locals())
@mcp.tool(annotations=ToolAnnotations(readOnlyHint=False,destructiveHint=False,idempotentHint=True,openWorldHint=False))
def cancel_navigation(nav_task_id:str,operation_id:str)->dict:
    """Request cancellation. Wait for canceled or another terminal result before starting a new goal."""
    return call('cancel_navigation',locals())
@mcp.tool(annotations=ToolAnnotations(readOnlyHint=False,destructiveHint=False,idempotentHint=True,openWorldHint=False))
def confirm_delivery(order_id:str,package_id:str,operation_id:str)->dict:
    """Simulated handoff. Requires correct package, destination proximity and available receiver."""
    return call('confirm_delivery',locals())
@mcp.tool(annotations=ToolAnnotations(readOnlyHint=False,destructiveHint=False,idempotentHint=True,openWorldHint=False))
def defer_order(order_id:str,reason:str,retry_after_sim:float,operation_id:str)->dict:
    """Keep order and payload while recording a deferred delivery."""
    return call('defer_order',locals())
@mcp.tool(annotations=ToolAnnotations(readOnlyHint=False,destructiveHint=False,idempotentHint=True,openWorldHint=False))
def close_order(order_id:str,reason:str,operation_id:str)->dict:
    """Record failure with reason. Never mark unconfirmed handoff as delivered."""
    return call('close_order',locals())
@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True,destructiveHint=False,idempotentHint=True,openWorldHint=False))
def verify_run(run_id:str='')->dict:
    """Return program verified receipts, unfinished orders and metrics. Missing measurements remain null."""
    return call('verify_run',locals())
if __name__=='__main__':mcp.run(transport='stdio')
