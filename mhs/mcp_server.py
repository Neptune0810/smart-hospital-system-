import json,os,urllib.request,urllib.error
from typing import Literal
from pydantic import BaseModel,Field
from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations

mcp=FastMCP('Hospital MHS Business')
READ=ToolAnnotations(readOnlyHint=True,destructiveHint=False,idempotentHint=True,openWorldHint=False)
WRITE=ToolAnnotations(readOnlyHint=False,destructiveHint=False,idempotentHint=True,openWorldHint=False)

def compact_result(value):
    # Nested event results also contain full Nav2 paths. Removing only the
    # top-level polylines makes a simple state query approach the context limit.
    omitted={'trajectory','navigation_tasks','decision_ids','points','nav2_path','_cells','selected_route','route_options','hospital'}
    if isinstance(value,list):return [compact_result(x) for x in value]
    if isinstance(value,dict):
        result={k:compact_result(v) for k,v in value.items() if k not in omitted}
        if 'smart_ward' in result:
            ward=result['smart_ward']
            ward.pop('operations',None)
            ward.pop('transfers',None)
            ward.pop('model_request_ids',None)
            # Full ACK, immutable commands and sensor traces remain in the
            # project database/evidence; decision context needs identities only.
            for incident in ward.get('incidents',[]):
                for field in ['observations','recovery_readings','scene_ack','command']:
                    if isinstance(incident.get(field),list):incident[field]=incident[field][-3:]
        if isinstance(result.get('events'),list):result['events']=result['events'][-20:]
        if isinstance(result.get('orders'),dict) and 'segment_order_ids' in result:
            ids=set(result['segment_order_ids'])
            history={k:o for k,o in result['orders'].items() if k not in ids}
            # Completed prior segments remain in SQLite, raw state and receipts.
            # Current decision context includes this segment and any carried cargo.
            result['orders']={k:o for k,o in result['orders'].items() if k in ids or o.get('loaded') or o.get('status') not in {'delivered','failed'}}
            result['previous_delivery_summary']={'orders':len(history),'delivered':sum(o.get('status')=='delivered' for o in history.values()),'failed':sum(o.get('status')=='failed' for o in history.values())}
        return result
    return value

class DeliveryItem(BaseModel):
    quantity:int=Field(ge=1,le=20,strict=True,description='必填独立包裹数量。两件填2；服务为每件生成一个订单和包裹，不能把数量仅写进package_label。')
    destination:str=Field(description='命名地点：物资库、A/B/C护士站、药房、检验交接点、急诊接收点、301/302/303/304病房、候诊接待台（旧名称仍为别名）。未知地点先询问。')
    pickup_station:str='depot'
    priority:Literal['normal','urgent']='normal'
    deadline_seconds:float|None=Field(default=None,gt=0,le=7200,description='相对当前仿真时间；省略普通900、急件300秒。不是现实世界承诺。')
    package_label:str=''

class PlanStep(BaseModel):
    action:Literal['navigate','pickup','deliver','wait','return','standby']
    station_id:str
    order_ids:list[str]=Field(default_factory=list,description='关联已有订单编号；取货、交接、等待必填，返库可空。')

@mcp.tool(annotations=WRITE)
def publish_delivery_plan(steps:list[PlanStep],reason:str,planning_version:int,operation_id:str)->dict:
    """发布剩余子任务和一句公开理由，不执行运动。使用最新planning_version并覆盖全部未完成订单；尚未装车的订单必须先安排其pickup_station取货，再安排交接。按当前载货模拟每步，超容量或交接前漏取货会被拒绝。新单或异常后先更新计划，再运动。末尾standby；用户要求返回时末尾return。理由最多180字，过期时读状态再新调用。"""
    return call('publish_delivery_plan',dict(steps=[x.model_dump() for x in steps],reason=reason,planning_version=planning_version,operation_id=operation_id))

def call(name,args):
    req=urllib.request.Request('http://127.0.0.1:4188/api/tool',data=json.dumps(dict(name=name,arguments=args)).encode(),headers={'Content-Type':'application/json'})
    try:
        with urllib.request.urlopen(req,timeout=60 if name=='get_route_options' else 10) as f:r=json.load(f)
        return compact_result(r)
    except urllib.error.HTTPError as e:return json.load(e)

@mcp.tool(annotations=READ)
def get_route_options(station_id:str)->dict:
    """静止时查询ready_actions中的取货或已载货交接地点的最多三条Nav2验证路线。包裹未取，不能先导航交接点；满载不能去取第三件。比较长度、时间（不含模型等待）、人群暴露、安静区距离，随后由你选择MHS navigate_route(route_id)。无未完成订单时可导航最终待命或返回点。途中先stop_device确认静止。"""
    return call('get_route_options',locals())

@mcp.tool(annotations=READ)
def get_state(since_event_id:int=0)->dict:
    """读取真实订单及取货/交接中文标签、剩余截止时间、载货与容量、接收站、定位、导航、事件。ready_actions是当前有包裹可取或已载货可送的地点列表，不自动选择或执行；自己根据优先级、容量和截止时间选目标。未取的包裹不能直接去交接点。历史回执保留，新单或异常后先更新计划。"""
    return call('get_state',locals())
@mcp.tool(annotations=WRITE)
def create_delivery_orders(items:list[DeliveryItem],operation_id:str)->dict:
    """创建点到点配送单；每种取送安排一个items元素，quantity必填独立包裹数量。服务为每件生成独立订单与包裹编号；核对返回数量后告知用户。pickup_station省略为物资库；先到取货点装货，再交接。默认普通900、急件300仿真秒。数量/地点不明先问；操作编号由适配层生成。"""
    return call('create_delivery_orders',dict(items=[x.model_dump() for x in items],operation_id=operation_id))
@mcp.tool(annotations=WRITE)
def pickup_package(order_id:str,operation_id:str)->dict:
    """真实静止于订单pickup_station才可装货，容量两个。地点与容量由服务校验；操作编号由适配层生成。"""
    return call('pickup_package',locals())
@mcp.tool(annotations=WRITE)
def confirm_delivery(order_id:str,package_id:str,operation_id:str)->dict:
    """按匹配包裹在订单指定交接点交接，十个命名业务点均可用；只有成功回执表示交付。接收异常最多两次尝试。恢复后再次尝试用新operation_id；重复原编号返回原拒绝结果。"""
    return call('confirm_delivery',locals())
@mcp.tool(annotations=WRITE)
def defer_order(order_id:str,reason:str,retry_after_sim:float,operation_id:str)->dict:
    """延期但保留订单和载货；retry_after_sim大于当前仿真时间。随后watch_device等待接收站恢复。"""
    return call('defer_order',locals())
@mcp.tool(annotations=WRITE)
def close_order(order_id:str,reason:str,operation_id:str)->dict:
    """明确记录未交付及原因，不生成交接回执；连续接收异常150仿真秒会由业务限制关闭。"""
    return call('close_order',locals())
@mcp.tool(annotations=READ)
def verify_run(run_id:str='')->dict:
    """核验交接回执、未完成订单、complete_task、final_station、final_stopped与剩余载货。默认末站待命；返库只在用户要求时核验。null代表未采集/NA，例如碰撞计数，不能解释为零或无碰撞。"""
    return call('verify_run',locals())
@mcp.tool(annotations=READ)
def wait_event(since_event_id:int=0,timeout_seconds:float=3)->dict:
    """短读取业务事件；导航长任务和接收恢复优先用MHS的完成通知和watch_device。"""
    return call('wait_event',locals())

class ServiceStep(BaseModel):
    action:Literal['create_delivery','pickup','navigate','deliver','set_brightness','verify','wait','standby']
    incident_id:str=''
    device_id:str=''
    station_id:str=''

@mcp.tool(annotations=WRITE)
def create_specimen_delivery(incident_id:str,operation_id:str)->dict:
    """为已检测的标本需求创建唯一运输订单。取送点、标本编号、普通900/急件300仿真秒期限来自需求，不能自填或复制；重复通知返回原订单。先登记急件，再按两位容量安排。"""
    return call('create_specimen_delivery',locals())

@mcp.tool(annotations=WRITE)
def publish_service_plan(planning_version:int,steps:list[ServiceStep],reason:str,operation_id:str)->dict:
    """发布一句公开安排和剩余步骤，不执行设备。覆盖全部未解决需求；每步关联incident_id，standby可空。运输先create_delivery再取送，运输地点须来自订单；取件和交接由对应柜体handoff_order执行。调灯set_brightness、核验verify、等反馈wait。当前planning_version过期先重读状态。有标本运输的批次末步standby指定最终停留点；只有照明且无载货时禁止取送和导航，standby不设station_id，原地等待传感器反馈后核验。载货最多两份。"""
    return call('publish_service_plan',dict(planning_version=planning_version,steps=[x.model_dump(exclude_defaults=True) for x in steps],reason=reason,operation_id=operation_id))

@mcp.tool(annotations=READ)
def verify_services()->dict:
    """按新场景反馈核验标本唯一归属、交接回执、灯光恢复连续读数、未解决与人工事项。命令成功不等于恢复；未采集为NA。处理结束必须调用并如实简短汇报。"""
    return call('verify_services',{})
if __name__=='__main__':
    # During the normal 1.2.x -> 1.3.0 package transaction only, the old
    # installed policy must see its exact eight-tool catalog to unload cleanly.
    # The flag removes the new metadata write; it never adds any permissions.
    from pathlib import Path
    stage=Path(__file__).resolve().parents[1]/'runs/mhs-v2/workbench-catalog-stage'
    if stage.exists():
        catalog=stage.read_text().strip()
        if catalog=='8':mcp.remove_tool('publish_delivery_plan')
        if catalog in {'8','9'}:mcp.remove_tool('get_route_options')
    from .runtime import mode,SMART_MODE
    smart=(stage.read_text().strip()=='smart' if stage.exists() else mode(Path(__file__).resolve().parents[1])==SMART_MODE)
    for name in (['create_delivery_orders','pickup_package','confirm_delivery','publish_delivery_plan','verify_run'] if smart else ['create_specimen_delivery','publish_service_plan','verify_services']):mcp.remove_tool(name)
    mcp.run(transport='stdio')
