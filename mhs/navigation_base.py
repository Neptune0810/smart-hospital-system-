import threading, math, time, uuid
import rclpy
from rclpy.node import Node
from rclpy.action import ActionClient
from rclpy.qos import qos_profile_sensor_data
from geometry_msgs.msg import PoseWithCovarianceStamped, Twist
from nav_msgs.msg import Odometry
from rosgraph_msgs.msg import Clock
from nav2_msgs.action import NavigateToPose
from action_msgs.msg import GoalStatus, GoalStatusArray
from action_msgs.srv import CancelGoal
from unique_identifier_msgs.msg import UUID
from tf2_ros import Buffer
from tf2_msgs.msg import TFMessage
from rclpy.qos import QoSProfile,DurabilityPolicy,ReliabilityPolicy


# These are the only project motion actions. Never turn a restored snapshot
# into a namespace-wide (zero UUID) cancellation request.
MOTION_ACTIONS = frozenset(('/carter1/navigate_to_pose', '/carter1/navigate_through_poses'))
TERMINAL_ACTION_STATUSES = frozenset((4, 5, 6))


def goal_identity(task):
    """Return a validated, nonzero identity; old snapshots stay unreconciled."""
    value = task.get('nav_goal_uuid')
    action = task.get('nav_action_namespace')
    if (not isinstance(value, str) or len(value) != 32
            or any(c not in '0123456789abcdef' for c in value)
            or value == '0' * 32 or action not in MOTION_ACTIONS):
        return None
    return value, action

class Navigator(Node):
    def __init__(self,engine):
        super().__init__('hospital_delivery',namespace='carter1')
        self.engine=engine;self.client=ActionClient(self,NavigateToPose,'navigate_to_pose')
        self.goal_handle=None;self.cancel_requested=False;self.sim=0;self.speed=0;self.angular_speed=0;self.pose=None;self.watchdog_reason=None;self.goal_started_sim=None
        self.goal_handle_tid=None;self.sent_goals={};self.current_goal=None
        self.recovering_goals={};self.recovery_clients={}
        # VOLATILE deliberately refuses retained status from before this API
        # process started. Only a new status observation can reconcile a goal.
        status_qos=QoSProfile(depth=10,durability=DurabilityPolicy.VOLATILE,reliability=ReliabilityPolicy.RELIABLE)
        self.status_subscriptions=[self.create_subscription(GoalStatusArray,action+'/_action/status',
            lambda message,a=action:self.goal_status(a,message),status_qos) for action in sorted(MOTION_ACTIONS)]
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
        self.submit_goal(tid,self.client,goal,'/carter1/navigate_to_pose')
    def submit_goal(self,tid,client,goal,action,feedback_callback=None):
        """Commit the chosen UUID before any request can reach Nav2."""
        identity={'nav_task_id':tid,'nav_goal_uuid':uuid.uuid4().hex,'nav_action_namespace':action}
        if goal_identity(identity) is None:raise ValueError('invalid_project_navigation_identity')
        persist=getattr(self.engine,'nav_goal_identity',None)
        if persist:
            result=persist(tid,identity['nav_goal_uuid'],action)
            if result is not True and not (isinstance(result,dict) and result.get('persisted') is True):
                raise RuntimeError('navigation_goal_identity_not_durably_persisted')
        self.sent_goals[tid]=identity;self.current_goal=identity
        kwargs={'goal_uuid':UUID(uuid=list(bytes.fromhex(identity['nav_goal_uuid'])))}
        if feedback_callback is not None:kwargs['feedback_callback']=feedback_callback
        try:
            future=client.send_goal_async(goal,**kwargs)
            future.add_done_callback(lambda f:self.accepted(tid,f))
        except Exception as error:
            # Once sending starts, an exception is not proof the server did
            # not accept the request. Keep motion locked until real terminal.
            self.goal_uncertain(tid,'goal_submission_uncertain:'+str(error))
    def goal_uncertain(self,tid,reason):
        identity=self.sent_goals.get(tid)
        uncertain=getattr(self.engine,'nav_goal_uncertain',None)
        if identity and uncertain:
            self.cancel_requested=True
            uncertain(tid,identity['nav_goal_uuid'],identity['nav_action_namespace'],reason)
            self.reconcile_navigation(identity)
        else:self.engine.nav_result(tid,'failed',reason)
    def accepted(self,tid,f):
        try:
            handle=f.result()
            identity=self.sent_goals.get(tid)
            if identity is None:return  # Late callback for an already reconciled goal.
            if not handle.accepted:
                self.engine.nav_result(tid,'failed','goal_rejected');self.forget_goal(tid);return
            if identity and bytes(handle.goal_id.uuid).hex()!=identity['nav_goal_uuid']:
                self.goal_uncertain(tid,'accepted_goal_uuid_mismatch');return
            self.goal_handle=handle;self.goal_handle_tid=tid
            if self.cancel_requested:self.goal_handle.cancel_goal_async()
            else:self.engine.nav_result(tid,'running')
            self.goal_handle.get_result_async().add_done_callback(lambda f:self.finished(tid,f))
        except Exception as e:self.goal_uncertain(tid,'goal_acceptance_uncertain:'+str(e))
    def finished(self,tid,f):
        if tid not in self.sent_goals:return
        try:
            status=f.result().status
            if status not in TERMINAL_ACTION_STATUSES:
                self.goal_uncertain(tid,'nonterminal_action_result_'+str(status));return
            # An uncertain task follows the same fresh status reconciliation
            # path even if its original result future completes later.
            if any(key[0]==tid for key in self.recovering_goals):return
            self.engine.nav_result(tid,'failed' if self.watchdog_reason else {GoalStatus.STATUS_SUCCEEDED:'succeeded',GoalStatus.STATUS_CANCELED:'canceled'}.get(status,'failed'),(self.watchdog_reason+';' if self.watchdog_reason else '')+'action_status_'+str(status))
        except Exception as e:
            self.goal_uncertain(tid,'goal_result_uncertain:'+str(e));return
        self.forget_goal(tid)
    def forget_goal(self,tid):
        self.sent_goals.pop(tid,None)
        if self.goal_handle_tid==tid:self.goal_handle=None;self.goal_handle_tid=None
        if self.current_goal and self.current_goal['nav_task_id']==tid:self.current_goal=None
    def reconcile_navigation(self,task):
        """Cancel one persisted goal and await a newly received terminal status.

        CancelGoal responses (including UNKNOWN_GOAL_ID and GOAL_TERMINATED)
        and absent status entries are deliberately never terminal evidence.
        """
        identity=goal_identity(task);tid=task.get('nav_task_id')
        if identity is None or not isinstance(tid,str) or not tid:return False
        value,action=identity;key=(tid,value,action)
        watch=self.recovering_goals.setdefault(key,{'next_cancel_wall':0})
        now=time.monotonic()
        if now<watch['next_cancel_wall']:return False
        pending=watch.get('cancel_future')
        if pending is not None and not pending.done():return False
        watch['next_cancel_wall']=now+1
        try:
            client=self.recovery_clients.get(action)
            if client is None:
                client=self.create_client(CancelGoal,action+'/_action/cancel_goal')
                self.recovery_clients[action]=client
            if not client.service_is_ready():return False
            request=CancelGoal.Request()
            request.goal_info.goal_id=UUID(uuid=list(bytes.fromhex(value)))
            # A zero timestamp with a nonzero UUID cancels exactly that goal.
            request.goal_info.stamp.sec=0;request.goal_info.stamp.nanosec=0
            future=client.call_async(request)
            watch['cancel_future']=future
            # Consume errors without promoting a cancel response to success.
            future.add_done_callback(self.recovery_cancel_response)
        except Exception:pass  # The durable recovery lock remains in force.
        return False
    @staticmethod
    def recovery_cancel_response(future):
        try:future.result()
        except Exception:pass
    def goal_status(self,action,message):
        if action not in MOTION_ACTIONS:return
        # This callback is the freshness boundary: no status is cached for a
        # goal that has not yet been registered for reconciliation.
        for status in message.status_list:
            if status.status not in TERMINAL_ACTION_STATUSES:continue
            try:value=bytes(status.goal_info.goal_id.uuid).hex()
            except (TypeError,ValueError):continue
            for key in tuple(self.recovering_goals):
                tid,expected,expected_action=key
                if (value,action)!=(expected,expected_action):continue
                confirm=getattr(self.engine,'nav_goal_terminal',None)
                if confirm:
                    try:result=confirm(tid,value,action,status.status)
                    except Exception:continue
                    if result is True or isinstance(result,dict) and result.get('confirmed') is True:
                        self.recovering_goals.pop(key,None);self.forget_goal(tid)
    def cancel(self):
        self.cancel_requested=True
        if self.goal_handle:self.goal_handle.cancel_goal_async()
        # A pending acceptance callback honors this flag. Lost/restarted
        # handles use the explicit unknown-task reconciliation path instead.

def launch(engine):
    rclpy.init()
    nav=Navigator(engine);engine.backend=nav
    threading.Thread(target=rclpy.spin,args=(nav,),daemon=True).start()
    return nav
