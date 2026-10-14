#!/usr/bin/env python3
"""Warm DDS discovery before requesting official Nav2 lifecycle startup."""
import json,os,time,uuid
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
NODES=['map_server','amcl','controller_server','smoother_server','planner_server','behavior_server','bt_navigator','waypoint_follower','velocity_smoother']
def main():
    import rclpy
    from nav2_msgs.srv import ManageLifecycleNodes
    from lifecycle_msgs.srv import GetState
    ident=os.environ.get('HOSPITAL_BOOTSTRAP_ID',uuid.uuid4().hex)
    def record(phase,**fields):
        data=dict(id=ident,phase=phase,wall_time=time.time(),**fields)
        p=ROOT/'logs/nav-bootstrap.tmp';p.write_text(json.dumps(data,ensure_ascii=False,indent=2));p.replace(ROOT/'logs/nav-bootstrap.json')
        print(json.dumps(data,ensure_ascii=False),flush=True)
    rclpy.init();node=rclpy.create_node('hospital_nav_bootstrap')
    managers={name:node.create_client(ManageLifecycleNodes,'/carter1/lifecycle_manager_'+name+'/manage_nodes') for name in ['localization','navigation']}
    states={name:node.create_client(GetState,'/carter1/'+name+'/get_state') for name in NODES}
    def wait_result(future,timeout):
        end=time.monotonic()+timeout
        while not future.done() and time.monotonic()<end:rclpy.spin_once(node,timeout_sec=.1)
        if not future.done():future.cancel();raise TimeoutError('lifecycle_service_response_timeout')
        return future.result()
    try:
        record('discovering')
        end=time.monotonic()+60
        while time.monotonic()<end:
            graph=dict(node.get_service_names_and_types())
            if all(c.service_is_ready() for c in list(managers.values())+list(states.values())) and all('/carter1/'+name+'/change_state' in graph for name in NODES):break
            rclpy.spin_once(node,timeout_sec=.2)
        else:raise TimeoutError('nav2_lifecycle_interfaces_not_discovered')
        # Nav2 has already created its change_state clients. Let reply readers match.
        record('discovery_warmup',warmup_wall_seconds=10)
        end=time.monotonic()+10
        while time.monotonic()<end:rclpy.spin_once(node,timeout_sec=.2)
        for name,client in managers.items():
            record('starting_'+name)
            req=ManageLifecycleNodes.Request();req.command=ManageLifecycleNodes.Request.STARTUP
            reply=wait_result(client.call_async(req),45)
            if not reply or not reply.success:raise RuntimeError('nav2_'+name+'_startup_rejected')
        observed={}
        for name,client in states.items():
            reply=wait_result(client.call_async(GetState.Request()),5)
            observed[name]=dict(id=reply.current_state.id,label=reply.current_state.label)
        if any(v['id']!=3 for v in observed.values()):raise RuntimeError('nav2_nodes_not_all_active:'+json.dumps(observed))
        record('passed',states=observed)
    except Exception as e:
        record('failed',error=str(e));raise
    finally:node.destroy_node();rclpy.shutdown()
if __name__=='__main__':main()
