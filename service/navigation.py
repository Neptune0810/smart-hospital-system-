import threading, math, time
import rclpy
from rclpy.node import Node
from rclpy.action import ActionClient
from rclpy.qos import qos_profile_sensor_data
from geometry_msgs.msg import PoseWithCovarianceStamped, Twist
from nav_msgs.msg import Odometry
from rosgraph_msgs.msg import Clock
from nav2_msgs.action import NavigateToPose
from action_msgs.msg import GoalStatus
from tf2_ros import Buffer
from tf2_msgs.msg import TFMessage
from rclpy.qos import QoSProfile,DurabilityPolicy

class Navigator(Node):
    def __init__(self,engine):
        super().__init__('hospital_delivery',namespace='carter1')
        self.engine=engine;self.client=ActionClient(self,NavigateToPose,'navigate_to_pose')
        self.goal_handle=None;self.cancel_requested=False;self.sim=0;self.speed=0;self.angular_speed=0;self.pose=None;self.watchdog_reason=None;self.goal_started_sim=None
        self.create_subscription(Clock,'/clock',self.clock,10)
        self.create_subscription(Odometry,'chassis/odom',self.odom,qos_profile_sensor_data)
        self.create_subscription(PoseWithCovarianceStamped,'amcl_pose',self.localization,10)
        self.buffer=Buffer()
        self.create_subscription(TFMessage,'tf',self.tf,100)
        self.create_subscription(TFMessage,'tf_static',self.tf_static,QoSProfile(depth=100,durability=DurabilityPolicy.TRANSIENT_LOCAL))
        self.create_timer(0.1,self.update)
    def ready(self):return self.client.server_is_ready()
    def clock(self,m):self.sim=m.clock.sec+m.clock.nanosec/1e9
    def odom(self,m):
        self.speed=math.hypot(m.twist.twist.linear.x,m.twist.twist.linear.y);self.angular_speed=m.twist.twist.angular.z;self.odom_wall=time.time()
    def tf(self,m):
        for t in m.transforms:self.buffer.set_transform(t,'ROS')
    def tf_static(self,m):
        for t in m.transforms:self.buffer.set_transform_static(t,'ROS')
    def localization(self,m):
        p=m.pose.pose;q=p.orientation
        self.pose=(p.position.x,p.position.y,math.atan2(2*(q.w*q.z+q.x*q.y),1-2*(q.y*q.y+q.z*q.z)))
        self.pose_wall=time.time()
    def update(self):
        try:
            t=self.buffer.lookup_transform('map','base_link',rclpy.time.Time()).transform
            q=t.rotation
            pose=(t.translation.x,t.translation.y,math.atan2(2*(q.w*q.z+q.x*q.y),1-2*(q.y*q.y+q.z*q.z)))
            if time.time()-getattr(self,'odom_wall',0)<3:self.engine.telemetry(*pose,self.speed,self.sim,angular_speed=self.angular_speed)
        except Exception:pass
        with self.engine.lock:
            task=self.engine.nav
            if task and task['status']=='running' and self.goal_started_sim is not None and self.sim-self.goal_started_sim>180 and not self.watchdog_reason:
                self.watchdog_reason='navigation_180_sim_second_deadline';task['status']='canceling';self.engine.event('navigation_watchdog',nav_task_id=task['nav_task_id'],reason=self.watchdog_reason);self.cancel()
    def start(self,tid,station):
        goal=NavigateToPose.Goal();goal.pose.header.frame_id='map';goal.pose.header.stamp=self.get_clock().now().to_msg()
        goal.pose.pose.position.x=float(station['x']);goal.pose.pose.position.y=float(station['y'])
        goal.pose.pose.orientation.z=math.sin(station['yaw']/2);goal.pose.pose.orientation.w=math.cos(station['yaw']/2)
        self.cancel_requested=False;self.watchdog_reason=None;self.goal_started_sim=self.sim
        f=self.client.send_goal_async(goal)
        f.add_done_callback(lambda f:self.accepted(tid,f))
    def accepted(self,tid,f):
        try:
            self.goal_handle=f.result()
            if not self.goal_handle.accepted:self.engine.nav_result(tid,'failed','goal_rejected');return
            if self.cancel_requested:self.goal_handle.cancel_goal_async()
            else:self.engine.nav_result(tid,'running')
            self.goal_handle.get_result_async().add_done_callback(lambda f:self.finished(tid,f))
        except Exception as e:self.engine.nav_result(tid,'failed',str(e))
    def finished(self,tid,f):
        try:
            status=f.result().status
            self.engine.nav_result(tid,'failed' if self.watchdog_reason else {GoalStatus.STATUS_SUCCEEDED:'succeeded',GoalStatus.STATUS_CANCELED:'canceled'}.get(status,'failed'),(self.watchdog_reason+';' if self.watchdog_reason else '')+'action_status_'+str(status))
        except Exception as e:self.engine.nav_result(tid,'failed',str(e))
        self.goal_handle=None
    def cancel(self):
        self.cancel_requested=True
        if self.goal_handle:self.goal_handle.cancel_goal_async()

def launch(engine):
    rclpy.init()
    nav=Navigator(engine);engine.backend=nav
    threading.Thread(target=rclpy.spin,args=(nav,),daemon=True).start()
    return nav
