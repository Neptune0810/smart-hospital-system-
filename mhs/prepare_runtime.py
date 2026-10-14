"""Generate project-specific launch and simulation copies; leave v6 sources intact."""
import pathlib,subprocess,tarfile,io,json,hashlib
from .camera_config import patch_camera_source
ROOT=pathlib.Path(__file__).resolve().parents[1]
OUT=ROOT/'mhs'
def container_file(path):
    r=subprocess.run(['docker','cp','hospital-delivery-runtime:'+path,'-'],capture_output=True,check=True)
    with tarfile.open(fileobj=io.BytesIO(r.stdout)) as t:
        entries=[x for x in t if x.isfile()]
        if len(entries)!=1:raise RuntimeError('unexpected_docker_archive')
        return t.extractfile(entries[0]).read().decode()
def main():
    launch=OUT/'nav2_launch';launch.mkdir(exist_ok=True)
    evidence={}
    for name in ['bringup_launch.py','localization_launch.py','navigation_launch.py']:
        s=container_file('/opt/ros/humble/share/nav2_bringup/launch/'+name)
        evidence[name]=dict(original_sha256=hashlib.sha256(s.encode()).hexdigest())
        if name=='bringup_launch.py':
            s=s.replace("launch_dir = os.path.join(bringup_dir, 'launch')","launch_dir = '/project/mhs/nav2_launch'")
        if name=='navigation_launch.py':
            if s.count("('cmd_vel_smoothed', 'cmd_vel')")!=2:raise RuntimeError('unexpected_nav2_velocity_remapping')
            s=s.replace("('cmd_vel_smoothed', 'cmd_vel')","('cmd_vel_smoothed', 'mhs/nav_cmd_vel')")
            import re
            s,count=re.subn(r"(package='nav2_behaviors'[\s\S]*?remappings=remappings)(\))",r"\1 + [('cmd_vel', 'mhs/nav_cmd_vel')]\2",s)
            if count!=2:raise RuntimeError('unexpected_nav2_behavior_remapping')
        (launch/name).write_text(s)
        evidence[name]['mhs_sha256']=hashlib.sha256(s.encode()).hexdigest()
    s=(ROOT/'launch/navigation.launch.py').read_text()
    s=s.replace("get_package_share_directory('nav2_bringup')+'/launch/bringup_launch.py'","'/project/mhs/nav2_launch/bringup_launch.py'")
    (OUT/'navigation.launch.py').write_text(s)
    s=(ROOT/'sim/hospital.py').read_text()
    s='import hashlib\n'+s
    # A camera directly above the robot stays below the ceiling and avoids
    # putting a trailing camera inside nearby walls. Display only; no physics.
    s=s.replace('position=(4,-4,3)','position=(1.8,-1.8,1.8)')
    s=s.replace('Gf.Vec3d(pos[0]+2,pos[1]-2,3)','Gf.Vec3d(pos[0],pos[1],pos[2]+1.8)')
    s=s.replace('Gf.Vec3d(0,0,1)).GetInverse()','Gf.Vec3d(0,1,0)).GetInverse()')
    s=s.replace('xf.MakeMatrixXform().Set(mat)\n   data=rgb.get_data()','xf.MakeMatrixXform().Set(mat)\n   app.update()\n   data=rgb.get_data()')
    needle='tmp.replace(ROOT/"runs/latest.jpg")'
    if s.count(needle)!=1:raise RuntimeError('unexpected_camera_capture')
    s=s.replace(needle,needle+'\n    meta_tmp=ROOT/"runs/latest-frame.tmp";meta_tmp.write_text(json.dumps({"capture_wall_time":time.time(),"sim_time":SimulationManager.get_simulation_time(),"sha256":hashlib.sha256((ROOT/"runs/latest.jpg").read_bytes()).hexdigest(),"width":960,"height":540}));meta_tmp.replace(ROOT/"runs/latest-frame.json")')
    s=patch_camera_source(s)
    (OUT/'hospital_sim.py').write_text(s)
    for p in [OUT/'hospital_sim.py',OUT/'navigation.launch.py',*launch.glob('*.py')]:compile(p.read_text(),str(p),'exec')
    (OUT/'runtime-provenance.json').write_text(json.dumps(evidence,indent=2))
    print(json.dumps(evidence))
if __name__=='__main__':main()
