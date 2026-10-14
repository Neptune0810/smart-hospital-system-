"""Nav2 routes and independent, local patient-proximity motion gate."""
import math, threading, time
import numpy as np
from geometry_msgs.msg import PoseStamped, Twist
from nav_msgs.msg import OccupancyGrid
from PIL import Image,ImageDraw
from array import array
from nav2_msgs.msg import CostmapFilterInfo,Costmap,SpeedLimit
from nav2_msgs.action import NavigateThroughPoses, ComputePathThroughPoses
from rclpy.action import ActionClient
from rclpy.qos import QoSProfile,DurabilityPolicy,ReliabilityPolicy
import rclpy
from .navigation import MHSNavigator
from .motion_limits import limited_twist
from .proximity import people_gate,ContinuousBlock

class V2Navigator(MHSNavigator):
    def __init__(self,engine):
        super().__init__(engine)
        self.route_client=ActionClient(self,NavigateThroughPoses,'navigate_through_poses')
        self.planner=ActionClient(self,ComputePathThroughPoses,'compute_path_through_poses')
        qos=QoSProfile(depth=1,durability=DurabilityPolicy.TRANSIENT_LOCAL,reliability=ReliabilityPolicy.RELIABLE)
        self.mask_pub=self.create_publisher(OccupancyGrid,'route_mask',qos)
        self.info_pub=self.create_publisher(CostmapFilterInfo,'route_filter_info',qos)
        self.speed_pub=self.create_publisher(SpeedLimit,'speed_limit',10)
        self.create_timer(.1,self.publish_speed_limit,clock=self.wall_clock)
        self.costmap_stamps={'global':float('-inf'),'local':float('-inf')}
        self.costmap_walls={'global':float('-inf'),'local':float('-inf')}
        for kind in ['global','local']:
            # Humble OccupancyGridUpdate stamps are zero. costmap_raw is a full
            # actual master grid published with the ROS clock on every cycle.
            self.create_subscription(Costmap,kind+'_costmap/costmap_raw',lambda message,k=kind:self.costmap_seen(k,message),qos)
        self.blocked=ContinuousBlock();self.last_metric_wall=0
        self.latest_route_feedback=None
        self.create_timer(.1,self.track_people_gate,clock=self.wall_clock)
        self.safety_metrics={'min_person_distance_m':None,'speed_limit_violations':0,'samples':0,'yield_stops':0,'source':'simulation_people_truth_and_real_odometry'}
        self.publish_info();self.publish_mask(None)
    def costmap_seen(self,kind,message):
        self.costmap_stamps[kind]=message.header.stamp.sec+message.header.stamp.nanosec/1e9
        self.costmap_walls[kind]=time.monotonic()
    def ready(self):return super().ready() and self.route_client.server_is_ready() and self.planner.server_is_ready()
    def health(self):
        return {**super().health(),'ros_time':self.get_clock().now().nanoseconds/1e9,'costmap_stamps':{k:v if math.isfinite(v) else None for k,v in self.costmap_stamps.items()},'costmap_age_wall_seconds':{k:time.monotonic()-v if math.isfinite(v) else None for k,v in self.costmap_walls.items()}}
    def publish_info(self):
        message=CostmapFilterInfo();message.header.frame_id='map';message.type=0
        message.filter_mask_topic='/carter1/route_mask';message.base=0.;message.multiplier=1.;self.info_pub.publish(message)
    def publish_mask(self,route):
        routes=self.engine.routes;w,h=routes.image_size;resolution=routes.resolution
        # Humble 1.1.20 computes changed bounds in master-grid cells. A coarse
        # mask leaves old forbidden cells outside that small update region.
        # Match the actual map geometry so a mask change refreshes the whole map.
        mask=Image.new('L',(w,h),100 if route else 0);draw=ImageDraw.Draw(mask)
        pixel=lambda p:((p[0]-routes.origin[0])/resolution,h-(p[1]-routes.origin[1])/resolution)
        if route:
            # Corridor includes space for local yielding, not another remote corridor.
            points=[pixel(p) for p in route['points']];radius=1.5/resolution
            if len(points)>1:draw.line(points,fill=0,width=round(radius*2)+1)
            for x,y in points:draw.ellipse((x-radius,y-radius,x+radius,y+radius),fill=0)
        for z in routes.cfg['zones']:
            if z['id'] in self.engine.s.get('closed_zones',[]):
                a=pixel([z['bounds'][0],z['bounds'][3]]);b=pixel([z['bounds'][2],z['bounds'][1]]);draw.rectangle((*a,*b),fill=100)
        message=OccupancyGrid();message.header.frame_id='map';message.header.stamp=self.get_clock().now().to_msg()
        message.info.resolution=resolution;message.info.width=w;message.info.height=h
        message.info.origin.position.x=routes.origin[0];message.info.origin.position.y=routes.origin[1];message.info.origin.orientation.w=1.
        message.data=array('b',np.asarray(mask,dtype=np.int8)[::-1,:].tobytes());self.mask_pub.publish(message)
    def sync_mask(self,route,abort=None,local=False,timeout=15):
        marker=max(self.sim,self.get_clock().now().nanoseconds/1e9);wall=time.monotonic();self.publish_mask(route)
        end=time.monotonic()+timeout;kinds=['global','local'] if local else ['global']
        while any(self.costmap_stamps[k]<marker+.02 or self.costmap_walls[k]<=wall for k in kinds):
            if abort and abort():raise RuntimeError('route_start_canceled_before_goal')
            if time.monotonic()>end:raise RuntimeError('route_mask_actual_costmap_update_timeout:'+str(self.health()))
            time.sleep(.05)
        with self.engine.lock:
            self.engine.event('route_mask_confirmed',route_id=route['route_id'] if route else None,marker_sim=marker,costmap_stamps=dict(self.costmap_stamps));self.engine.save()
    def prepare_route_query(self):self.sync_mask(None)
    def finish_route_query(self):self.sync_mask(None)
    def publish_speed_limit(self):
        with self.engine.lock:
            s=self.engine;crowd=s.routes.crowd()
            if not s.pose or not crowd['fresh']:return
            p=[s.pose['x'],s.pose['y']]
            # Inform DWB before entering a slower zone, rather than distorting
            # its simulated command after the trajectory has been chosen.
            ahead=[p[0]+1.5*math.cos(s.pose['yaw']),p[1]+1.5*math.sin(s.pose['yaw'])]
            cap=min(s.routes.speed_limit(p,crowd),s.routes.speed_limit(ahead,crowd))
            nearest=min((math.dist(p,h['position'][:2]) for h in crowd['people']),default=float('inf'))
            cap=min(cap,max(.05,(nearest-.95)*.25))
            message=SpeedLimit();message.header.stamp=self.get_clock().now().to_msg()
            message.percentage=True;message.speed_limit=100.*cap/.5;self.speed_pub.publish(message)
    def poses(self,route):
        points=route['points'];sample=points[::4]
        if not sample or sample[-1]!=points[-1]:sample.append(points[-1])
        result=[]
        for index,p in enumerate(sample):
            goal=PoseStamped();goal.header.frame_id='map';goal.header.stamp=self.get_clock().now().to_msg()
            goal.pose.position.x=float(p[0]);goal.pose.position.y=float(p[1])
            yaw=self.engine.stations[route['destination']]['yaw'] if index==len(sample)-1 else math.atan2(sample[index+1][1]-p[1],sample[index+1][0]-p[0])
            goal.pose.orientation.z=math.sin(yaw/2);goal.pose.orientation.w=math.cos(yaw/2);result.append(goal)
        return result
    def wait_future(self,future,timeout=12):
        event=threading.Event();future.add_done_callback(lambda _:event.set())
        if not event.wait(timeout):raise ValueError('nav2_route_validation_timeout')
        return future.result()
    def validate_route(self,route):
        if self.engine.nav and self.engine.nav['status'] in {'pending','running','canceling'}:raise ValueError('route_query_requires_stopped_navigation')
        # Validate under the same corridor restriction used during execution.
        # A path on the unrestricted map alone is not proof this route works.
        self.sync_mask(route)
        goal=ComputePathThroughPoses.Goal();goal.goals=self.poses(route)[1:];goal.use_start=False
        handle=self.wait_future(self.planner.send_goal_async(goal))
        if not handle.accepted:raise ValueError('nav2_route_plan_rejected')
        result=self.wait_future(handle.get_result_async())
        if result.status!=4 or not result.result.path.poses:raise ValueError('nav2_route_unreachable')
        route['nav2_path']=[[p.pose.position.x,p.pose.position.y] for p in result.result.path.poses]
    def start_route(self,tid,route):
        self.cancel_requested=False;self.watchdog_reason=None;self.goal_started_sim=self.sim;self.blocked=ContinuousBlock()
        def start_after_map_update():
            try:
                self.sync_mask(route,abort=lambda:self.cancel_requested,local=True)
                with self.engine.lock:
                    if self.cancel_requested:
                        self.engine.nav_result(tid,'canceled','canceled_before_goal');self.publish_mask(None);return
                    if not self.engine.guard.armed or not self.engine.fresh():raise RuntimeError('motion_guard_not_ready_after_mask_update')
                    goal=NavigateThroughPoses.Goal();goal.poses=self.poses(route)[1:]
                    self.submit_goal(tid,self.route_client,goal,'/carter1/navigate_through_poses',
                                     feedback_callback=lambda m:self.route_feedback(tid,m))
            except Exception as error:
                self.engine.nav_result(tid,'canceled' if self.cancel_requested else 'failed',str(error));self.publish_mask(None)
        threading.Thread(target=start_after_map_update,daemon=True).start()
    def route_feedback(self,tid,message):
        with self.engine.lock:
            if not self.engine.nav or self.engine.nav['nav_task_id']!=tid:return
            remaining=message.feedback.distance_remaining
            if not math.isfinite(remaining):return
            self.latest_route_feedback={'nav_task_id':tid,'distance_remaining_m':max(0.,float(remaining)),'wall_time':time.time(),'source':'actual_Nav2_action_feedback'}
    def finished(self,tid,future):
        super().finished(tid,future);self.publish_mask(None)
    def track_people_gate(self):
        # Nav2 recovery waits may stop publishing cmd_vel. Continue observing
        # real people then, so a clear interval actually resets the 10 s timer.
        with self.engine.lock:
            s=self.engine
            if not s.nav or s.nav['status'] not in {'pending','running'}:
                self.blocked=ContinuousBlock();return
            crowd=s.routes.crowd()
            if not s.pose or not crowd['fresh']:self.zero();s.guard.trip('crowd_telemetry_stale');return
            _,stop=people_gate(s.pose,crowd['people'])
            transition=self.blocked.observe(stop,self.sim)
            if transition=='started':self.safety_metrics['yield_stops']+=1;s.event('patient_yield_started')
            elif transition=='finished':s.event('patient_yield_finished')
            if stop:self.zero()
            if self.blocked.duration(self.sim)>=10 and s.nav['status']=='running':
                self.watchdog_reason='crowd_blocked_10_sim_seconds';s.nav['status']='canceling';self.cancel();s.event('route_replan_required',reason=self.watchdog_reason)
    def command(self,message):
        with self.engine.lock:
            s=self.engine;crowd=s.routes.crowd()
            if not s.pose or not crowd['fresh']:
                self.zero();s.guard.trip('crowd_telemetry_stale');return
            p=[s.pose['x'],s.pose['y']];nearest,stop=people_gate(s.pose,crowd['people'])
            cap=s.routes.speed_limit(p,crowd)
            if stop:
                self.zero()
            else:
                # Slow before the personal-distance boundary, not at contact.
                cap=min(cap,max(.05,(nearest-.95)*.25))
                try:message.linear.x,message.angular.z=limited_twist(message.linear.x,message.angular.z,cap)
                except ValueError:self.zero();s.guard.trip('invalid_nav_command');return
                super().command(message)
            if time.time()-self.last_metric_wall>=.2:
                self.last_metric_wall=time.time();m=self.safety_metrics;m['samples']+=1
                if math.isfinite(nearest):m['min_person_distance_m']=min(m['min_person_distance_m'] or nearest,nearest)
                if s.pose['speed']>cap+.05:m['speed_limit_violations']+=1

def launch(engine):
    rclpy.init();node=V2Navigator(engine);engine.backend=node
    threading.Thread(target=rclpy.spin,args=(node,),daemon=True).start();return node
