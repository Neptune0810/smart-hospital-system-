"""Apply the persisted V2 DWB caps while the project robot is stationary."""
import json,time,urllib.request
import rclpy
from rclpy.node import Node
from rclpy.parameter import Parameter
from rcl_interfaces.srv import SetParameters

def main():
    with urllib.request.urlopen('http://127.0.0.1:4188/api/state',timeout=5) as f:s=json.load(f)
    if not s['stopped_confirmed']:raise RuntimeError('physical_stop_not_confirmed')
    rclpy.init();node=Node('hospital_v2_limit_configuration',namespace='carter1')
    client=node.create_client(SetParameters,'controller_server/set_parameters')
    if not client.wait_for_service(timeout_sec=5):raise RuntimeError('project_controller_unavailable')
    req=SetParameters.Request()
    req.parameters=[Parameter(k,value=v).to_parameter_msg() for k,v in {'FollowPath.max_vel_x':.5,'FollowPath.max_speed_xy':.5,'FollowPath.max_vel_theta':.45}.items()]
    future=client.call_async(req);rclpy.spin_until_future_complete(node,future,timeout_sec=10)
    if not future.done():raise RuntimeError('controller_configuration_timeout')
    rows=[{'successful':v.successful,'reason':v.reason} for v in future.result().results]
    if not all(x['successful'] for x in rows):raise RuntimeError(str(rows))
    print(json.dumps({'wall_time':time.time(),'run_id':s['run_id'],'controller_limits':{p.name:p.value for p in [Parameter(k,value=v) for k,v in {'FollowPath.max_vel_x':.5,'FollowPath.max_speed_xy':.5,'FollowPath.max_vel_theta':.45}.items()]},'results':rows}))
    node.destroy_node();rclpy.shutdown()

if __name__=='__main__':main()
