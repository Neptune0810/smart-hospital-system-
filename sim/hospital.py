import faulthandler
faulthandler.enable()
faulthandler.dump_traceback_later(90,repeat=True)
import time, json, os
from pathlib import Path
from isaacsim import SimulationApp
ROOT=Path('/mnt/sda/omni-yezh/hospital-delivery-agent')
app=SimulationApp({"headless":True,"renderer":"RaytracedLighting","active_gpu":1,"multi_gpu":False,"sync_loads":False,"enable_motion_bvh":True,"width":960,"height":540})
import omni, carb
from isaacsim.core.experimental.utils.app import enable_extension
from isaacsim.core.experimental.utils.stage import is_stage_loading
from isaacsim.storage.native import get_assets_root_path
enable_extension("isaacsim.ros2.bridge")
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
app_utils.play()
from pxr import UsdGeom,Gf
import omni.replicator.core as rep
from PIL import Image
import numpy as np
cam=rep.functional.create.camera(position=(4,-4,3),look_at=(0,0,0.5),name="DeliveryDemoCamera")
rp=rep.create.render_product(cam,(960,540))
rgb=rep.annotators.get("rgb");rgb.attach(rp)
body_candidates=[x for x in stage.Traverse() if x.GetName()=="chassis_link" and "Nova_Carter_ROS_1" in str(x.GetPath())]
body=body_candidates[0] if body_candidates else stage.GetPrimAtPath("/World/Nova_Carter_ROS_1")
(ROOT/"evidence/robot_prims.json").write_text(json.dumps([str(x.GetPath()) for x in stage.Traverse() if "Nova_Carter_ROS_1" in str(x.GetPath()) and x.GetName() in ["chassis_link","base_link","Nova_Carter_ROS_1"]],indent=2))
print("HOSPITAL_SIM_READY",flush=True)
faulthandler.cancel_dump_traceback_later()
capture_at=0;frame=0
last_command=None
while app.is_running():
 command=ROOT/"runs/sim-command.json"
 if command.exists():
  cfg=json.loads(command.read_text())
  if cfg["id"]!=last_command:
   last_command=cfg["id"]
   if cfg["command"]=="reset":
    app_utils.stop();app.update();app_utils.play();app.update()
    ack=ROOT/"runs/sim-ack.tmp";ack.write_text(json.dumps({"id":last_command,"command":"reset","wall_time":time.time()}));ack.replace(ROOT/"runs/sim-ack.json")
 app.update()
 if time.monotonic()-capture_at>1:
  capture_at=time.monotonic()
  try:
   pos=UsdGeom.XformCache().GetLocalToWorldTransform(body).ExtractTranslation()
   pose_tmp=ROOT/"runs/physical-pose.tmp";pose_tmp.write_text(json.dumps({"position":list(pos),"wall_time":time.time(),"timeline_time":omni.timeline.get_timeline_interface().get_current_time(),"physics_time":SimulationManager.get_simulation_time()}));pose_tmp.replace(ROOT/"runs/physical-pose.json")
   mat=Gf.Matrix4d().SetLookAt(Gf.Vec3d(pos[0]+2,pos[1]-2,3),Gf.Vec3d(pos[0],pos[1],0.5),Gf.Vec3d(0,0,1)).GetInverse()
   xf=UsdGeom.Xformable(cam)
   xf.MakeMatrixXform().Set(mat)
   data=rgb.get_data()
   if isinstance(data,np.ndarray) and data.size:
    img=Image.fromarray(data[:,:,:3]);tmp=ROOT/"runs/latest.tmp.jpg";img.save(tmp,quality=82);tmp.replace(ROOT/"runs/latest.jpg")
    record=ROOT/"runs/recording.json"
    if record.exists():
     cfg=json.loads(record.read_text());directory=(ROOT/"runs"/cfg["directory"]).resolve()
     if (ROOT/"runs").resolve() in directory.parents:
      directory.mkdir(parents=True,exist_ok=True);img.save(directory/f"{frame:08d}.jpg",quality=82)
      with (directory/"frames.jsonl").open("a") as out:out.write(json.dumps({"frame":frame,"wall_time":time.time(),"timeline_time":omni.timeline.get_timeline_interface().get_current_time(),"physics_time":SimulationManager.get_simulation_time()})+"\n")
      frame+=1
  except Exception as e:
   if frame==0:print("CAPTURE_WARNING",str(e),flush=True)

app_utils.stop();app.close()
