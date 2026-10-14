"""Real render product positioned from the physical chassis transform (+X forward)."""
import hashlib
import json
import time


class FirstPersonCamera:
    def __init__(self, root):
        from pxr import UsdGeom, Gf
        import omni.replicator.core as rep
        self.root, self.UsdGeom, self.Gf = root, UsdGeom, Gf
        self.cam = rep.functional.create.camera(position=(0, 0, .6), look_at=(2, 0, .6), name='DeliveryFirstPersonCamera')
        camera = UsdGeom.Camera(self.cam)
        camera.GetFocalLengthAttr().Set(12.0)
        camera.GetClippingRangeAttr().Set(Gf.Vec2f(.05, 100.0))
        # Replicator creates its own xform ops. Collapse them once, preserving
        # the initial pose before the first physical chassis update.
        xform = UsdGeom.Xformable(self.cam)
        self._last_matrix = xform.GetLocalTransformation()
        self._matrix_op = xform.MakeMatrixXform()
        self._matrix_op.Set(self._last_matrix)
        self.product = rep.create.render_product(self.cam, (960, 540))
        self.rgb = rep.annotators.get('rgb')
        self.rgb.attach(self.product)

    def position(self, body, transform=None, *, cached=True):
        Gf, UsdGeom = self.Gf, self.UsdGeom
        # The diagnostic baseline must read the chassis itself and perform the
        # original per-call xform stack authoring, even with a shared transform.
        if not cached or transform is None:
            transform = UsdGeom.XformCache().GetLocalToWorldTransform(body)
        eye = transform.Transform(Gf.Vec3d(.45, 0, .60))
        target = transform.Transform(Gf.Vec3d(3.45, 0, .55))
        up = transform.TransformDir(Gf.Vec3d(0, 0, 1)).GetNormalized()
        matrix = Gf.Matrix4d().SetLookAt(eye, target, up).GetInverse()
        if not cached:
            self._matrix_op = UsdGeom.Xformable(self.cam).MakeMatrixXform()
            self._matrix_op.Set(matrix)
            self._last_matrix = matrix
        elif matrix != self._last_matrix:
            self._matrix_op.Set(matrix)
            self._last_matrix = matrix
        self.mount = dict(chassis_prim=str(body.GetPath()), eye_world=list(eye), look_at_world=list(target), up_world=list(up),
                          eye_local_m=[.45, 0, .60], look_at_local_m=[3.45, 0, .55], focal_length_mm=12.0)

    def capture(self, sim_time):
        import numpy as np
        from PIL import Image
        data = self.rgb.get_data()
        if not isinstance(data, np.ndarray) or not data.size:
            return
        image = self.root/'runs/first-person.jpg'
        temp = self.root/'runs/first-person.tmp.jpg'
        Image.fromarray(data[:, :, :3]).save(temp, quality=82)
        temp.replace(image)
        meta = dict(capture_wall_time=time.time(), sim_time=sim_time, sha256=hashlib.sha256(image.read_bytes()).hexdigest(),
                    width=960, height=540, view='first_person', camera=self.mount)
        temp = self.root/'runs/first-person-frame.tmp'
        temp.write_text(json.dumps(meta))
        temp.replace(self.root/'runs/first-person-frame.json')


def patch_first_person_source(source):
    replacements = [
        ('from camera_config import read_camera_config', 'from camera_config import read_camera_config\nfrom first_person_camera import FirstPersonCamera'),
        ('print("HOSPITAL_SIM_READY",flush=True)', 'first_person=FirstPersonCamera(ROOT)\nprint("HOSPITAL_SIM_READY",flush=True)'),
        ('   xf.MakeMatrixXform().Set(mat)\n   app.update()\n   data=rgb.get_data()',
         '   xf.MakeMatrixXform().Set(mat)\n   first_person.position(body)\n   app.update()\n   first_person.capture(SimulationManager.get_simulation_time())\n   data=rgb.get_data()'),
    ]
    for old, new in replacements:
        if source.count(old) != 1:
            raise RuntimeError('unexpected_first_person_source: '+old[:60])
        source = source.replace(old, new)
    return source
