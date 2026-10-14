import threading,math,time
import rclpy
from geometry_msgs.msg import Twist
from lifecycle_msgs.srv import GetState
from rclpy.clock import Clock,ClockType
from .navigation_base import Navigator
from .runtime_diagnostics import configure, trace_span, record, actual_freshness, status as diagnostic_status

class MHSNavigator(Navigator):
    def __init__(self,engine):
        self.odometry=None;self.odom_origin=None
        super().__init__(engine)
        self._diagnostic_inbound={};self._diagnostic_update={};self._diagnostic_rejected_at=0
        self._diagnostic_startup_error=None
        try:configure(engine,snapshot_getter=self.diagnostics_snapshot)
        except Exception as error:self._diagnostic_startup_error=type(error).__name__
        self.lifecycle_state=None;self.lifecycle_wall=0;self.lifecycle_future=None
        self.lifecycle=self.create_client(GetState,'bt_navigator/get_state')
        # Communication freshness and the 3 s lease are wall-time contracts.
        # Their callbacks must also run when ROS simulation time pauses or slows.
        self.wall_clock=Clock(clock_type=ClockType.STEADY_TIME)
        self.create_timer(1,self.check_lifecycle,clock=self.wall_clock)
        self.output=self.create_publisher(Twist,'cmd_vel',10)
        self.create_subscription(Twist,'mhs/nav_cmd_vel',self.command,10)
        self.create_timer(.1,self.safety_tick,clock=self.wall_clock)
    def check_lifecycle(self):
        if self.lifecycle_future and not self.lifecycle_future.done():return
        if not self.lifecycle.service_is_ready():self.lifecycle_state=None;return
        self.lifecycle_future=self.lifecycle.call_async(GetState.Request())
        self.lifecycle_future.add_done_callback(self.lifecycle_result)
    def lifecycle_result(self,future):
        try:self.lifecycle_state=future.result().current_state.id;self.lifecycle_wall=time.time()
        except Exception:self.lifecycle_state=None
    def ready(self):
        return self.lifecycle_state==3 and time.time()-self.lifecycle_wall<5 and super().ready()
    def health(self):
        return {'lifecycle_state':self.lifecycle_state,'lifecycle_age_wall_seconds':time.time()-self.lifecycle_wall if self.lifecycle_wall else None,'safety_clock':'steady_time','navigation_action_available':super().ready(),
            'runtime_diagnostics':dict(diagnostic_status(self.engine),startup_error=self._diagnostic_startup_error)}
    def _diagnostic_received(self,name,source_stamp=None):
        try:
            wall=time.time();mono=time.monotonic();prior=self._diagnostic_inbound.get(name)
            self._diagnostic_inbound[name]={'received_wall_time':wall,'received_monotonic_time':mono,
                'source_stamp_seconds':source_stamp,'gap_wall_seconds':mono-prior['received_monotonic_time'] if prior else None,
                'receipt_time_basis':'ROS_callback_entry_not_DDS_transport_arrival'}
        except Exception:pass
    def diagnostics_snapshot(self):
        # No fresh()/state()/health(), lock acquisition, file reads or SQLite.
        now=time.time();s=self.engine
        age=lambda stamp:now-stamp if isinstance(stamp,(float,int)) and stamp else None
        return {'snapshot_wall_time':now,'sim_time':self.sim,
            'clock_callback':self._diagnostic_inbound.get('clock'),
            'odom_callback':self._diagnostic_inbound.get('odom'),
            'tf_callback':self._diagnostic_inbound.get('tf'),
            'update_callback':self._diagnostic_inbound.get('update'),
            'safety_callback':self._diagnostic_inbound.get('safety'),
            'odom_wall_time':getattr(self,'odom_wall',None),
            'odom_age_wall_seconds':age(getattr(self,'odom_wall',None)),
            'clock_progress_wall_time':getattr(self,'clock_progress_wall',None),
            'clock_progress_age_wall_seconds':age(getattr(self,'clock_progress_wall',None)),
            'last_committed_tf_stamp':getattr(self,'last_tf_stamp',None),
            'update_latest':self._diagnostic_update,
            'pose_wall_time':s.last_pose_wall,'pose_age_wall_seconds':age(s.last_pose_wall),
            'actual_freshness_result':actual_freshness(s),
            'guard':{'armed':s.guard.armed,'reason':s.guard.reason,'connected':s.guard.connected,
                'last_lease_monotonic_time':s.guard.last_lease},
            'latest_atomic':getattr(s,'latest_atomic',getattr(s,'_runtime_latest_atomic',None)),
            'latest_save':getattr(s,'_runtime_latest_save',None),
            'snapshot_is_nonatomic_observation':True}
    def _diagnostic_rejection(self,event,**fields):
        try:
            active=self.engine.nav and self.engine.nav['status'] in {'pending','running','canceling'}
            now=time.monotonic()
            if active and now-self._diagnostic_rejected_at>=1:
                self._diagnostic_rejected_at=now
                record(self.engine,event,snapshot=self.diagnostics_snapshot(),**fields)
        except Exception:pass
    def odom(self,message):
        stamp=getattr(getattr(message,'header',None),'stamp',None)
        self._diagnostic_received('odom',stamp.sec+stamp.nanosec/1e9 if stamp is not None else None)
        with trace_span(self.engine,'ros.odom'):
            super().odom(message)
            p=message.pose.pose;q=p.orientation
            yaw=math.atan2(2*(q.w*q.z+q.x*q.y),1-2*(q.y*q.y+q.z*q.z))
            if self.odom_origin is None:self.odom_origin=(p.position.x,p.position.y,yaw)
            self.odometry=dict(x=p.position.x-self.odom_origin[0],y=p.position.y-self.odom_origin[1],
                yaw=math.degrees(yaw-self.odom_origin[2]),v=message.twist.twist.linear.x,w=math.degrees(self.angular_speed))
    def clock(self,message):
        self._diagnostic_received('clock',message.clock.sec+message.clock.nanosec/1e9)
        with trace_span(self.engine,'ros.clock'):
            previous=self.sim
            super().clock(message)
            if self.sim!=previous:self.clock_progress_wall=time.time()
    def tf(self,message):
        try:self._diagnostic_received('tf',max((t.header.stamp.sec+t.header.stamp.nanosec/1e9 for t in message.transforms),default=None))
        except Exception:pass
        with trace_span(self.engine,'ros.tf'):super().tf(message)
    def update(self):
        self._diagnostic_received('update')
        with trace_span(self.engine,'ros.update'):
            try:
                with trace_span(self.engine,'ros.tf_lookup'):
                    stamped=self.buffer.lookup_transform('map','base_link',rclpy.time.Time())
                stamp=stamped.header.stamp.sec+stamped.header.stamp.nanosec/1e9
                fresh=(time.time()-getattr(self,'odom_wall',0)<3 and time.time()-getattr(self,'clock_progress_wall',0)<3 and abs(self.sim-stamp)<3)
                self._diagnostic_update={'lookup_wall_time':time.time(),'tf_stamp_seconds':stamp,
                    'sim_minus_tf_seconds':self.sim-stamp,'original_ros_fresh_result':fresh,
                    'duplicate_tf':stamp==getattr(self,'last_tf_stamp',None)}
                if fresh and stamp!=getattr(self,'last_tf_stamp',None):
                    t=stamped.transform;q=t.rotation
                    yaw=math.atan2(2*(q.w*q.z+q.x*q.y),1-2*(q.y*q.y+q.z*q.z))
                    with trace_span(self.engine,'ros.engine_telemetry'):
                        self.engine.telemetry(t.translation.x,t.translation.y,yaw,self.speed,self.sim,angular_speed=self.angular_speed)
                    self.last_tf_stamp=stamp
                elif not fresh:self._diagnostic_rejection('ros_update_freshness_rejected')
            except Exception as error:
                self._diagnostic_update={'lookup_wall_time':time.time(),'error_type':type(error).__name__,'error':str(error)[:300]}
                self._diagnostic_rejection('ros_update_exception',error_type=type(error).__name__)
            with trace_span(self.engine,'ros.watchdog_lock_wait'):
                self.engine.lock.acquire()
            try:
                task=self.engine.nav
                if task and task['status']=='running' and self.goal_started_sim is not None and self.sim-self.goal_started_sim>1200 and not self.watchdog_reason:
                    self.watchdog_reason='navigation_1200_sim_second_deadline';task['status']='canceling';self.engine.event('navigation_watchdog',nav_task_id=task['nav_task_id'],reason=self.watchdog_reason);self.cancel()
            finally:self.engine.lock.release()
    def command(self,message):
        with self.engine.lock:
            if self.engine.nav and self.engine.nav['status'] in {'pending','running'} and self.engine.guard.tick(self.engine.fresh()):self.output.publish(message)
            else:self.zero()
    def zero(self):self.output.publish(Twist())
    def safety_tick(self):
        self._diagnostic_received('safety')
        with trace_span(self.engine,'ros.safety'):
            self.engine.tick()
            if not self.engine.guard.armed or not self.engine.nav or self.engine.nav['status'] not in {'pending','running'}:self.zero()

def launch(engine):
    rclpy.init();node=MHSNavigator(engine);engine.backend=node
    threading.Thread(target=rclpy.spin,args=(node,),daemon=True).start()
    return node
