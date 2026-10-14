"""Read-only ROS/Isaac stillness evidence for recovery of an unavailable API."""
import json,time,math,sys
from pathlib import Path
import rclpy
from nav_msgs.msg import Odometry
from geometry_msgs.msg import Twist
from action_msgs.srv import CancelGoal
from rclpy.qos import qos_profile_sensor_data
rclpy.init();node=rclpy.create_node('hospital_mhs_recovery_check');samples=[]
stopping='--stop' in sys.argv
publisher=node.create_publisher(Twist,'/carter1/cmd_vel',10) if stopping else None
if stopping:
    cancels=[]
    for action in ['navigate_to_pose','navigate_through_poses']:
        cancel=node.create_client(CancelGoal,'/carter1/'+action+'/_action/cancel_goal');cancels.append(cancel)
        if cancel.wait_for_service(timeout_sec=2):cancel.call_async(CancelGoal.Request())
def observe(m):
    t=m.header.stamp.sec+m.header.stamp.nanosec/1e9
    p=json.loads(Path('/project/runs/physical-pose.json').read_text())
    speed=math.hypot(m.twist.twist.linear.x,m.twist.twist.linear.y);angular=m.twist.twist.angular.z
    if time.time()-p['wall_time']>=5 or abs(t-p['physics_time'])>=3 or speed>=.05 or abs(angular)>=.1:
        samples.clear();return
    samples.append({'sim':t,'speed':speed,'angular':angular,'wall':time.time(),'physical':p})
node.create_subscription(Odometry,'/carter1/chassis/odom',observe,qos_profile_sensor_data)
end=time.monotonic()+12
while time.monotonic()<end:
    if publisher:publisher.publish(Twist())
    rclpy.spin_once(node,timeout_sec=.2)
    if len(samples)>1 and samples[-1]['sim']-samples[0]['sim']>=1:
        print(json.dumps({'stopped_confirmed':True,'first':samples[0],'last':samples[-1]}));node.destroy_node();rclpy.shutdown();break
else:
    node.destroy_node();rclpy.shutdown();raise RuntimeError('fresh_ROS_and_physical_stop_not_confirmed')
