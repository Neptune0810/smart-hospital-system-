import hashlib
import faulthandler
faulthandler.enable()
faulthandler.dump_traceback_later(90,repeat=True)
import time, json, os
from pathlib import Path
from camera_config import read_camera_config
from first_person_camera import FirstPersonCamera
from follow_camera import FollowingCamera
from sim_performance import SimPerformance
from isaacsim import SimulationApp
ROOT=Path('/mnt/sda/omni-yezh/hospital-delivery-agent')
# Camera authoring and the application CPU pool are independent candidates.
# Physics timestep, PhysX threads, camera content/rates and safety stay fixed.
def read_performance_config():
 config_path=ROOT/'configs/simulation-performance.json'
 if not config_path.exists():return {'variant':'baseline','cpu_threads':32}
 config=json.loads(config_path.read_text())
 if (not isinstance(config,dict) or set(config)!={'variant','cpu_threads'}
     or config['variant'] not in ('baseline','camera_cache')
     or type(config['cpu_threads']) is not int or config['cpu_threads'] not in (16,32)):
  raise ValueError('simulation_performance_config_invalid')
 return config
performance_config=read_performance_config()
performance=SimPerformance(ROOT,default_variant=performance_config['variant'])
launch_config={"headless":True,"disable_viewport_updates":True,"renderer":"RaytracedLighting","active_gpu":1,"multi_gpu":False,"sync_loads":False,"enable_motion_bvh":True,"width":960,"height":540}
launch_config['limit_cpu_threads']=performance_config['cpu_threads']
app=SimulationApp(launch_config)
import omni, carb
from isaacsim.core.experimental.utils.app import enable_extension
from isaacsim.core.experimental.utils.stage import is_stage_loading
from isaacsim.storage.native import get_assets_root_path
enable_extension("isaacsim.ros2.bridge")
enable_extension("isaacsim.replicator.agent.core")
app.update()
assets=get_assets_root_path()
print("ASSETS_ROOT",assets,flush=True)
path=assets+"/Isaac/Samples/ROS2/Scenario/multiple_robot_carter_hospital_navigation.usd"
print("OPEN_START",path,flush=True)
omni.usd.get_context().open_stage(path)
print("OPEN_RETURN",flush=True)
early_stage=omni.usd.get_context().get_stage()
for path_remove in [str(node.GetPath()) for node in early_stage.Traverse() if node.GetName() in ["Nova_Carter_ROS_2","Nova_Carter_ROS_3"]]:
 early_stage.RemovePrim(path_remove)
print("UPDATES_START",flush=True)
app.update();app.update()
print("UPDATES_RETURN",flush=True)
load_start=time.monotonic();last_progress=-20
while is_stage_loading() and time.monotonic()-load_start<120:
 app.update()
 if time.monotonic()-load_start-last_progress>=20:
  last_progress=time.monotonic()-load_start
  print("LOADING_STATUS",omni.usd.get_context().get_stage_loading_status(),flush=True)
print("LOADING_END",omni.usd.get_context().get_stage_loading_status(),flush=True)
stage=omni.usd.get_context().get_stage()
from layout import install_scene_signs
layout_proof=install_scene_signs(ROOT,stage)
print('HOSPITAL_LAYOUT_READY',layout_proof['version'],len(layout_proof['rooms']),flush=True)
roots=[str(p.GetPath()) for p in stage.GetPseudoRoot().GetChildren()]
(ROOT/"evidence/stage_roots.json").write_text(json.dumps(roots,indent=2))
print("STAGE_ROOTS",roots,flush=True)
print("SINGLE_ROBOT_ROOTS",[str(p.GetPath()) for p in stage.GetPseudoRoot().GetChildren()],flush=True)
sensor_inventory=[]
for prim in stage.Traverse():
 if prim.GetTypeName() in ['OmniLidar','OmniRadar','Camera']:
  sensor_inventory.append({'path':str(prim.GetPath()),'type':prim.GetTypeName(),'attributes':{k:str(prim.GetAttribute(k).Get()) for k in ['omni:sensor:tickRate','omni:sensor:Core:scanRateBaseHz'] if prim.HasAttribute(k)}})
(ROOT/'evidence/sensor-inventory.json').write_text(json.dumps(sensor_inventory,indent=2))

from isaacsim.core.simulation_manager import SimulationManager
SimulationManager.setup_simulation(dt=1.0/60.0,device="cpu")
import isaacsim.core.experimental.utils.app as app_utils
from crowd_v2 import HospitalCrowd
crowd=HospitalCrowd(ROOT,stage,seed=int(os.environ.get("HOSPITAL_PEOPLE_SEED","101")))
crowd_start=time.monotonic()
while not crowd.ready():
 app.update()
 if time.monotonic()-crowd_start>300:raise RuntimeError("crowd_setup_timeout")
character_load_start=time.monotonic()
while is_stage_loading():
 app.update()
 if time.monotonic()-character_load_start>180:raise RuntimeError('character_asset_load_timeout')
for _ in range(10):app.update()
app_utils.play()
from pxr import UsdGeom,Gf
import omni.replicator.core as rep
from PIL import Image
import numpy as np
camera_settings=read_camera_config(ROOT/"mhs/camera.json")
cam=rep.functional.create.camera(position=(0,0,camera_settings["height_m"]),look_at=(0,0,camera_settings["look_at_height_m"]),name="DeliveryDemoCamera")
UsdGeom.Camera(cam).GetFocalLengthAttr().Set(camera_settings["focal_length_mm"])
# Collapse Replicator's matrix ops once, preserving its initial camera pose.
third_camera_xform=UsdGeom.Xformable(cam)
third_camera_last_matrix=third_camera_xform.GetLocalTransformation()
third_camera_matrix_op=third_camera_xform.MakeMatrixXform()
third_camera_matrix_op.Set(third_camera_last_matrix)
third_camera_last_focal=camera_settings['focal_length_mm']
following_camera=FollowingCamera(ROOT)
third_camera_publication=dict(camera_settings)
rp=rep.create.render_product(cam,(960,540))
rgb=rep.annotators.get("rgb");rgb.attach(rp)
body_candidates=[x for x in stage.Traverse() if x.GetName()=="chassis_link" and "Nova_Carter_ROS_1" in str(x.GetPath())]
body=body_candidates[0] if body_candidates else stage.GetPrimAtPath("/World/Nova_Carter_ROS_1")
(ROOT/"evidence/robot_prims.json").write_text(json.dumps([str(x.GetPath()) for x in stage.Traverse() if "Nova_Carter_ROS_1" in str(x.GetPath()) and x.GetName() in ["chassis_link","base_link","Nova_Carter_ROS_1"]],indent=2))
first_person=FirstPersonCamera(ROOT)
smart_ward=None
presentation=None
if os.environ.get('HOSPITAL_MODE')=='smart-ward-v1':
 from smart_scene import SmartWardScene
 smart_ward=SmartWardScene(ROOT,stage,body)
 print('SMART_WARD_SCENE_READY',smart_ward.map_proof['map_yaml'],flush=True)
 try:
  from presentation_scene import PresentationScene
  presentation=PresentationScene(ROOT,stage)
  print('HOSPITAL_PRESENTATION_READY',len(presentation.lights),flush=True)
 except Exception as presentation_error:
  print('HOSPITAL_PRESENTATION_UNAVAILABLE',repr(presentation_error),flush=True)
from layout import LayoutInspection
layout_inspection=LayoutInspection(ROOT)
frame_probe=None
frame_probe_proof={'enabled':False}
fresh_camera_stream=os.environ.get('HOSPITAL_MODE')=='smart-ward-v1'
camera_sink=None
camera_rate_proof=[]
if fresh_camera_stream:
 enable_extension('isaacsim.sensors.experimental.rtx')
 from camera_frame_clock import set_camera_tick_rate
 from camera_jpeg_sink import CameraJpegSink
 camera_rate=float(os.environ.get('HOSPITAL_CAMERA_TICK_RATE','20'))
 if camera_rate not in (5.,20.,60.):raise ValueError('unsupported_camera_tick_rate')
 for camera_prim in (cam,first_person.cam):
  camera_rate_proof.append(set_camera_tick_rate(str(UsdGeom.Camera(camera_prim).GetPrim().GetPath()),camera_rate))
 camera_sink=CameraJpegSink(ROOT)
if fresh_camera_stream or (os.environ.get('HOSPITAL_MODE')=='smart-ward-v1' and os.environ.get('HOSPITAL_CAMERA_CLOCK_PROBE')=='1'):
 probe_dir=ROOT/'runs/smart-ward-v1/acceptance'/('camera-clock-probe-'+str(time.time_ns()))
 try:
  from camera_frame_clock import CameraFrameProbe
  frame_probe=CameraFrameProbe({'third_person':(rp,rgb),'first_person':(first_person.product,first_person.rgb)},output_dir=probe_dir,performance=performance if performance.enabled else None)
  frame_probe_proof={'enabled':True,'directory':str(probe_dir),'production_jpeg_unchanged':not fresh_camera_stream,
   'fresh_camera_stream':fresh_camera_stream,'camera_rates':camera_rate_proof,
   'module_sha256':hashlib.sha256((ROOT/'mhs/camera_frame_clock.py').read_bytes()).hexdigest()}
 except Exception as probe_error:
  frame_probe_proof={'enabled':True,'directory':str(probe_dir),'initialization_error':str(probe_error),'production_jpeg_unchanged':True}
  print('CAMERA_CLOCK_PROBE_ERROR',repr(probe_error),flush=True)
  if fresh_camera_stream:raise RuntimeError('fresh_camera_clock_required') from probe_error
if os.environ.get('HOSPITAL_MODE')=='smart-ward-v1':
 viewport_observation={'updates_enabled':None,'status':'unavailable'}
 try:
  from omni.kit.viewport.utility import get_active_viewport
  active_viewport=get_active_viewport()
  if active_viewport is None:
   viewport_observation['status']='no_active_viewport'
  else:
   observed_updates=active_viewport.updates_enabled
   if isinstance(observed_updates,bool):
    viewport_observation={'updates_enabled':observed_updates,'status':'observed'}
   else:
    viewport_observation.update(status='unexpected_value',value_repr=repr(observed_updates))
 except Exception as viewport_error:
  viewport_observation.update(status='error',error_type=type(viewport_error).__name__,error=str(viewport_error))
 launch_wall=time.time()
 launch_stamp=time.strftime('%Y%m%d-%H%M%S',time.gmtime(launch_wall))+'-'+str(time.time_ns())
 launch_proof={
  'schema_version':1,'mode':'smart-ward-v1','wall_time':launch_wall,
  'wall_time_utc':time.strftime('%Y-%m-%dT%H:%M:%SZ',time.gmtime(launch_wall)),
  'capture_stage':'scene_ready','requested_launch_config':dict(launch_config),
  'actual_viewport':viewport_observation,
  'actual_cpu_thread_limits':{setting:carb.settings.get_settings().get(setting) for setting in (
   '/plugins/carb.tasking.plugin/threadCount','/plugins/omni.tbb.globalcontrol/maxThreadCount',
   '/persistent/physics/numThreads')},
  'camera_clock_probe':frame_probe_proof,
  'source':{'path':str(Path(__file__).resolve()),'sha256':hashlib.sha256(Path(__file__).read_bytes()).hexdigest()},
  'official_sources':[
   'https://docs.isaacsim.omniverse.nvidia.com/6.0.1/reference_material/sim_performance_optimization_handbook.html#scene-and-rendering-optimizations',
   'https://docs.isaacsim.omniverse.nvidia.com/6.0.1/py/source/extensions/isaacsim.simulation_app/docs/index.html'],
 }
 launch_dir=ROOT/'runs/smart-ward-v1'
 acceptance_dir=launch_dir/'acceptance'
 acceptance_dir.mkdir(parents=True,exist_ok=True)
 launch_payload=json.dumps(launch_proof,indent=2)
 with (acceptance_dir/('performance-launch-'+launch_stamp+'.json')).open('x') as launch_snapshot:
  launch_snapshot.write(launch_payload)
 launch_tmp=launch_dir/('simulation-launch-'+launch_stamp+'.tmp')
 launch_tmp.write_text(launch_payload)
 launch_tmp.replace(launch_dir/'simulation-launch.json')
print("HOSPITAL_SIM_READY",flush=True)
faulthandler.cancel_dump_traceback_later()
capture_at=0;frame=0
last_physical_capture_sim=None
last_command=None
camera_config_at=0
camera_publish_status={}
# app_update includes render/physics callbacks. These spans are inclusive and
# must not be summed as independent wall-time components.
performance_settings={}
performance_physics={'status':'disabled','steps':0,'dt_total':0.,'pre_step_monotonic':None}
if performance.enabled:
 for setting_path in (
  '/plugins/carb.tasking.plugin/threadCount',
  '/plugins/omni.tbb.globalcontrol/maxThreadCount',
  '/persistent/exts/omni.anim.navigation.core/navMesh/config/autoRebakeOnChanges',
  '/persistent/physics/numThreads',
  '/rtx/sensors/supportMultiTickRate',
  '/rtx/sensors/perSensorTickTlas',
  '/app/runLoops/main/rateLimitEnabled',
  '/app/runLoops/main/rateLimitFrequency',
 ):
  try:
   value=carb.settings.get_settings().get(setting_path)
   performance_settings[setting_path]=value if value is None or isinstance(value,(str,bool,int,float)) else repr(value)
  except Exception as settings_error:
   performance_settings[setting_path]={'status':'unavailable','error':repr(settings_error)}
 # Event-boundary duration includes other physics callbacks; it is not pure PhysX.
 try:
  from isaacsim.core.simulation_manager import SimulationEvent
  def performance_physics_pre(step_dt,context):
   performance_physics['pre_step_monotonic']=time.perf_counter()
  def performance_physics_post(step_dt,context):
   now=time.perf_counter()
   started=performance_physics['pre_step_monotonic']
   if started is not None:performance.record_duration('physics_step_event',now-started)
   performance_physics['pre_step_monotonic']=None
   performance_physics['steps']+=1
   performance_physics['dt_total']+=float(step_dt)
  performance_callback_handles=[
   SimulationManager.register_callback(performance_physics_pre,SimulationEvent.PHYSICS_PRE_STEP),
   SimulationManager.register_callback(performance_physics_post,SimulationEvent.PHYSICS_POST_STEP),
  ]
  performance_physics['status']='registered'
 except Exception as physics_probe_error:
  performance_physics.update(status='unavailable',error=repr(physics_probe_error))
performance_extra={}
performance_extra_at=0.

def publish_performance():
 global performance_extra,performance_extra_at
 if not performance.enabled:return
 now=time.monotonic()
 if now-performance_extra_at>=1.:
  performance_extra_at=now
  frame_status=frame_probe.status() if frame_probe is not None else None
  performance_extra={
   'settings_readback':performance_settings,
   'physics_event_probe':{key:value for key,value in performance_physics.items() if key!='pre_step_monotonic'},
   'frame_counts':None if frame_status is None else {name:state['completed_seq'] for name,state in frame_status['sources'].items()},
   'frame_rejection_counts':None if frame_status is None else frame_status['counts'],
   'camera_rates':camera_rate_proof,
   'launch_config':launch_config,
   'measurement_scope':'inclusive_spans_not_additive',
  }
 performance.publish(SimulationManager.get_simulation_time(),extra=performance_extra)

def position_following_cameras():
 global third_camera_matrix_op,third_camera_last_matrix,third_camera_last_focal,third_camera_publication
 transform=UsdGeom.XformCache().GetLocalToWorldTransform(body)
 pos=transform.ExtractTranslation()
 focal=camera_settings['focal_length_mm']
 cached=performance.variant()=='camera_cache'
 if not cached or focal!=third_camera_last_focal:
  UsdGeom.Camera(cam).GetFocalLengthAttr().Set(focal)
  third_camera_last_focal=focal
 mat,camera_pose=following_camera.pose(transform,camera_settings)
 third_camera_publication=dict(camera_settings,**camera_pose)
 if not cached:
  third_camera_matrix_op=UsdGeom.Xformable(cam).MakeMatrixXform()
  third_camera_matrix_op.Set(mat)
  third_camera_last_matrix=mat
 elif mat!=third_camera_last_matrix:
  third_camera_matrix_op.Set(mat)
  third_camera_last_matrix=mat
 first_person.position(body,transform=transform,cached=cached)

def publish_completed_camera(view,settings):
 return camera_sink.publish(view,frame_probe.latest(view,max_age_seconds=.35),camera=settings,simulation_playing=smart_ward.timeline.is_playing())

def sample_runtime():
 sample_sim=SimulationManager.get_simulation_time()
 if presentation:performance.call('presentation_poll',presentation.poll,sample_sim)
 if smart_ward:performance.call('smart_scene_tick',smart_ward.tick,sample_sim)
 performance.call('crowd_tick',crowd.tick,sample_sim)
 performance.call('layout_tick',layout_inspection.tick,sample_sim)

while app.is_running():
 performance.poll_control()
 command=ROOT/"runs/sim-command.json"
 if command.exists():
  cfg=json.loads(command.read_text())
  if cfg["id"]!=last_command:
   last_command=cfg["id"]
   if cfg["command"]=="reset":
    if smart_ward:
     ack=ROOT/'runs/sim-ack.tmp';ack.write_text(json.dumps({'id':last_command,'command':'reset','status':'rejected','error':'smart_ward_requires_managed_reset','wall_time':time.time()}));ack.replace(ROOT/'runs/sim-ack.json')
    else:
     app_utils.stop();app.update();app_utils.play();app.update()
     ack=ROOT/"runs/sim-ack.tmp";ack.write_text(json.dumps({"id":last_command,"command":"reset","wall_time":time.time()}));ack.replace(ROOT/"runs/sim-ack.json")
 if smart_ward:performance.call('scene_controls',smart_ward.poll_controls,SimulationManager.get_simulation_time())
 if fresh_camera_stream:
  if time.monotonic()-camera_config_at>=1:
   camera_settings=read_camera_config(ROOT/'mhs/camera.json');camera_config_at=time.monotonic()
  performance.call('camera_follow',position_following_cameras)
 performance.call('app_update',app.update)
 sample_runtime()
 if fresh_camera_stream:
  local_mount={key:first_person.mount[key] for key in ('chassis_prim','eye_local_m','look_at_local_m','focal_length_mm')}
  for view,settings in (('first_person',local_mount),('third_person',third_camera_publication)):
   if smart_ward.timeline.is_playing() and not camera_sink.should_publish(view,time.monotonic()):
    continue
   result=performance.call('camera_publish',publish_completed_camera,view,settings)
   previous=camera_publish_status.setdefault(view,{})
   if result.get('written'):previous['last_publication']=result['metadata']
   if result.get('reason')=='publication_error' and previous.get('last_error')!=result.get('error'):
    print('CAMERA_JPEG_PUBLICATION_ERROR',view,result.get('error_type'),result.get('error'),flush=True)
   previous.update(reason=result.get('reason'),last_error=result.get('error'),checked_wall_time=time.time())
   if result.get('written') and view=='third_person':
    record=ROOT/'runs/recording.json'
    if record.exists():
     cfg=json.loads(record.read_text());directory=(ROOT/'runs'/cfg['directory']).resolve()
     if (ROOT/'runs').resolve() in directory.parents:
      directory.mkdir(parents=True,exist_ok=True);result['image'].save(directory/f'{frame:08d}.jpg',quality=82)
      with (directory/'frames.jsonl').open('a') as out:out.write(json.dumps({'frame':frame,'wall_time':result['metadata']['capture_wall_time'],'physics_time':result['metadata']['sim_time'],'completed_seq':result['metadata']['completed_seq']})+'\n')
      frame+=1
 publish_performance()
 if time.monotonic()-capture_at>1:
  capture_at=time.monotonic()
  if frame_probe:
   try:
    probe_status=frame_probe.status()
    probe_status.update(wall_time=time.time(),sim_time=SimulationManager.get_simulation_time(),directory=frame_probe_proof['directory'],jpeg_publication=camera_publish_status)
    probe_tmp=ROOT/'runs/smart-ward-v1/camera-probe-status.tmp'
    probe_tmp.write_text(json.dumps(probe_status))
    probe_tmp.replace(ROOT/'runs/smart-ward-v1/camera-probe-status.json')
   except Exception as probe_status_error:
    print('CAMERA_CLOCK_PROBE_STATUS_ERROR',str(probe_status_error),flush=True)
  try:
   pos=UsdGeom.XformCache().GetLocalToWorldTransform(body).ExtractTranslation()
   physical_sim=SimulationManager.get_simulation_time()
   if not smart_ward or (smart_ward.timeline.is_playing() and (last_physical_capture_sim is None or physical_sim>last_physical_capture_sim)):
    pose_tmp=ROOT/"runs/physical-pose.tmp";pose_tmp.write_text(json.dumps({"position":list(pos),"wall_time":time.time(),"timeline_time":omni.timeline.get_timeline_interface().get_current_time(),"physics_time":physical_sim}));pose_tmp.replace(ROOT/"runs/physical-pose.json")
    last_physical_capture_sim=physical_sim
   if fresh_camera_stream:continue
   camera_settings=read_camera_config(ROOT/"mhs/camera.json")
   UsdGeom.Camera(cam).GetFocalLengthAttr().Set(camera_settings["focal_length_mm"])
   mat,camera_pose=following_camera.pose(UsdGeom.XformCache().GetLocalToWorldTransform(body),camera_settings)
   xf=UsdGeom.Xformable(cam)
   xf.MakeMatrixXform().Set(mat)
   first_person.position(body,cached=performance.variant()=='camera_cache')
   performance.call('app_update',app.update)
   # This update also advances physics. Keep independent physical velocity,
   # people and layout feedback synchronized with every application step.
   sample_runtime()
   first_person.capture(SimulationManager.get_simulation_time())
   data=rgb.get_data()
   if isinstance(data,np.ndarray) and data.size:
    img=Image.fromarray(data[:,:,:3]);tmp=ROOT/"runs/latest.tmp.jpg";img.save(tmp,quality=82);tmp.replace(ROOT/"runs/latest.jpg")
    meta_tmp=ROOT/"runs/latest-frame.tmp";meta_tmp.write_text(json.dumps({"capture_wall_time":time.time(),"sim_time":SimulationManager.get_simulation_time(),"sha256":hashlib.sha256((ROOT/"runs/latest.jpg").read_bytes()).hexdigest(),"width":960,"height":540,"camera":camera_settings}));meta_tmp.replace(ROOT/"runs/latest-frame.json")
    record=ROOT/"runs/recording.json"
    if record.exists():
     cfg=json.loads(record.read_text());directory=(ROOT/"runs"/cfg["directory"]).resolve()
     if (ROOT/"runs").resolve() in directory.parents:
      directory.mkdir(parents=True,exist_ok=True);img.save(directory/f"{frame:08d}.jpg",quality=82)
      with (directory/"frames.jsonl").open("a") as out:out.write(json.dumps({"frame":frame,"wall_time":time.time(),"timeline_time":omni.timeline.get_timeline_interface().get_current_time(),"physics_time":SimulationManager.get_simulation_time()})+"\n")
      frame+=1
  except Exception as e:
   if frame==0:print("CAPTURE_WARNING",str(e),flush=True)

if frame_probe:
 try:frame_probe.close()
 except Exception as probe_close_error:print('CAMERA_CLOCK_PROBE_CLOSE_ERROR',str(probe_close_error),flush=True)
app_utils.stop();app.close()
