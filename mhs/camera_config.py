"""Display camera settings only; never changes the robot or navigation."""
import json, math
from pathlib import Path

DEFAULTS = dict(height_m=1.8, focal_length_mm=12.0, look_at_height_m=0.6,
                back_distance_m=2.4, side_offset_m=0.65, look_ahead_m=0.8)
LIMITS = dict(height_m=(1.2, 2.4), focal_length_mm=(8.0, 24.0), look_at_height_m=(0.0, 1.2),
              back_distance_m=(0.4, 3.5), side_offset_m=(-1.0, 1.0), look_ahead_m=(0.0, 3.0))

def read_camera_config(path):
    values = json.loads(Path(path).read_text()) if Path(path).exists() else {}
    if not isinstance(values, dict) or set(values) - set(DEFAULTS):
        raise ValueError('invalid_display_camera_fields')
    result = dict(DEFAULTS, **values)
    for key, value in result.items():
        low, high = LIMITS[key]
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or not low <= value <= high:
            raise ValueError('invalid_display_camera_' + key)
    return result

def patch_camera_source(source):
    """Apply the same camera change to the generated Isaac entry point."""
    replacements = [
        ('from pathlib import Path', 'from pathlib import Path\nfrom camera_config import read_camera_config'),
        ('cam=rep.functional.create.camera(position=(1.8,-1.8,1.8),look_at=(0,0,0.5),name="DeliveryDemoCamera")',
         'camera_settings=read_camera_config(ROOT/"mhs/camera.json")\ncam=rep.functional.create.camera(position=(0,0,camera_settings["height_m"]),look_at=(0,0,camera_settings["look_at_height_m"]),name="DeliveryDemoCamera")\nUsdGeom.Camera(cam).GetFocalLengthAttr().Set(camera_settings["focal_length_mm"])'),
        ('   mat=Gf.Matrix4d().SetLookAt(Gf.Vec3d(pos[0],pos[1],pos[2]+1.8),Gf.Vec3d(pos[0],pos[1],0.5),Gf.Vec3d(0,1,0)).GetInverse()',
         '   camera_settings=read_camera_config(ROOT/"mhs/camera.json")\n   UsdGeom.Camera(cam).GetFocalLengthAttr().Set(camera_settings["focal_length_mm"])\n   mat=Gf.Matrix4d().SetLookAt(Gf.Vec3d(pos[0],pos[1],pos[2]+camera_settings["height_m"]),Gf.Vec3d(pos[0],pos[1],camera_settings["look_at_height_m"]),Gf.Vec3d(0,1,0)).GetInverse()'),
        ('"sha256":hashlib.sha256((ROOT/"runs/latest.jpg").read_bytes()).hexdigest(),"width":960,"height":540}',
         '"sha256":hashlib.sha256((ROOT/"runs/latest.jpg").read_bytes()).hexdigest(),"width":960,"height":540,"camera":camera_settings}'),
    ]
    for old, new in replacements:
        if source.count(old) != 1:
            raise RuntimeError('unexpected_camera_source: ' + old[:65])
        source = source.replace(old, new)
    try:
        from .first_person_camera import patch_first_person_source
    except ImportError:
        from first_person_camera import patch_first_person_source
    return patch_first_person_source(source)
