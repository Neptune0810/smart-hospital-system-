"""Hospital room semantics, frozen geometry and non-colliding room signs."""
import hashlib
import json
import math
import os
from pathlib import Path


def station_digest(stations):
    geometry = {k: {p: v[p] for p in ('x', 'y', 'yaw')} for k, v in stations.items()}
    return hashlib.sha256(json.dumps(geometry, sort_keys=True).encode()).hexdigest()


def validate_layout(stations, hospital):
    rooms = {room['id']: room for room in hospital.get('rooms', [])}
    if not rooms:
        return  # Legacy layouts remain readable.
    for ident, point in stations.items():
        if point.get('room_id') not in rooms:
            raise ValueError('layout_room_missing:' + ident)
        if any(not math.isfinite(point[k]) for k in ('x', 'y', 'yaw')):
            raise ValueError('layout_nonfinite_point:' + ident)
        room = rooms[point['room_id']]
        x0, y0, x1, y1 = room['bounds']
        if x0 >= x1 or y0 >= y1:
            raise ValueError('layout_invalid_bounds:' + ident)
        if math.dist([point['x'], point['y']], room['handoff_anchor'][:2]) > 2.5:
            raise ValueError('layout_handoff_too_far_from_room:' + ident)
    for room in rooms.values():
        for ident in room.get('serves', []):
            if ident not in stations:
                raise ValueError('layout_unknown_served_point:' + ident)


def install_scene_signs(root, stage):
    """Only visual sign meshes; walls, collision and robot pose are unchanged."""
    from PIL import Image, ImageDraw, ImageFont
    from pxr import Gf, Sdf, Usd, UsdGeom, UsdShade
    root = Path(root)
    hospital = json.loads((root/'configs/hospital-v2.json').read_text())
    stations = json.loads((root/'configs/stations.json').read_text())
    validate_layout(stations, hospital)
    output = Path(os.environ.get('HOSPITAL_DATA_DIR',str(root/'runs/mhs-v2')))/'layout'/hospital['version']
    output.mkdir(parents=True, exist_ok=True)
    # Use a project-local font when provided, otherwise installed CJK fonts.
    fonts = [root/'assets/layout/NotoSansCJK-Regular.ttc',
             Path('/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc'),
             Path('/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf')]
    font_path = next((p for p in fonts if p.is_file()), None)
    if font_path is None:
        raise RuntimeError('layout_sign_font_unavailable')
    cjk = 'CJK' in font_path.name
    all_names = {name for room in hospital['rooms'] for name in room.get('scene_anchors', [])}
    actual = {}
    cache = UsdGeom.XformCache()
    for prim in stage.Traverse():
        if prim.GetName() in all_names:
            actual[prim.GetName()] = {'path': str(prim.GetPath()),
                                     'world_position': list(cache.GetLocalToWorldTransform(prim).ExtractTranslation())}
    rows = []
    for room in hospital['rooms']:
        missing = [name for name in room.get('scene_anchors', []) if name not in actual]
        if missing:
            raise RuntimeError('layout_scene_anchor_missing:' + ','.join(missing))
        first = actual[room['scene_anchors'][0]]['world_position']
        x0,y0,x1,y1 = room['bounds']
        if not (x0-.15 <= first[0] <= x1+.15 and y0-.15 <= first[1] <= y1+.15):
            raise RuntimeError('layout_anchor_outside_room:' + room['id'])
        image = Image.new('RGB', (1024, 320), '#143d43')
        draw = ImageDraw.Draw(image)
        draw.rounded_rectangle((8, 8, 1016, 312), radius=18, outline='#8fd6c7', width=5)
        title = room['label'] if cjk else room['english']
        draw.text((40, 38), title, font=ImageFont.truetype(str(font_path), 64), fill='white')
        draw.text((40, 142), room['english'], font=ImageFont.truetype(str(font_path), 37), fill='#bfe7df')
        draw.text((40, 228), 'ROBOT HANDOFF OUTSIDE / SIMULATION',
                  font=ImageFont.truetype(str(font_path), 29), fill='#f2d9a2')
        texture = output/(room['id']+'.png'); image.save(texture)
        sign = room['sign']; x, y, z, yaw = sign['pose']
        width, height = sign.get('size', [1.5, .47])
        center = Gf.Vec3d(x, y, z)
        horizontal = Gf.Vec3d(math.cos(yaw), math.sin(yaw), 0)
        corners = [center+horizontal*dx+Gf.Vec3d(0, 0, dz)
                   for dx, dz in [(-width/2, -height/2), (width/2, -height/2),
                                  (width/2, height/2), (-width/2, height/2)]]
        path = '/World/HospitalRoomSigns/'+room['id']
        mesh = UsdGeom.Mesh.Define(stage, path)
        mesh.CreatePointsAttr(corners)
        mesh.CreateFaceVertexCountsAttr([4]); mesh.CreateFaceVertexIndicesAttr([0, 1, 2, 3])
        mesh.CreateDoubleSidedAttr(True)
        mesh.CreateNormalsAttr([(math.sin(yaw), -math.cos(yaw), 0)]*4)
        mesh.SetNormalsInterpolation('vertex')
        mesh.CreateSubdivisionSchemeAttr('none')
        st = UsdGeom.PrimvarsAPI(mesh).CreatePrimvar('st', Sdf.ValueTypeNames.TexCoord2fArray, UsdGeom.Tokens.vertex)
        st.Set([(0, 0), (1, 0), (1, 1), (0, 1)])
        material = UsdShade.Material.Define(stage, path+'/Material')
        shader = UsdShade.Shader.Define(stage, path+'/Material/Surface')
        shader.CreateIdAttr('UsdPreviewSurface')
        shader.CreateInput('roughness', Sdf.ValueTypeNames.Float).Set(.8)
        reader = UsdShade.Shader.Define(stage, path+'/Material/UV')
        reader.CreateIdAttr('UsdPrimvarReader_float2')
        reader.CreateInput('varname', Sdf.ValueTypeNames.Token).Set('st')
        reader.CreateOutput('result', Sdf.ValueTypeNames.Float2)
        sampler = UsdShade.Shader.Define(stage, path+'/Material/Texture')
        sampler.CreateIdAttr('UsdUVTexture')
        sampler.CreateInput('file', Sdf.ValueTypeNames.Asset).Set(texture.resolve().as_uri())
        sampler.CreateInput('sourceColorSpace', Sdf.ValueTypeNames.Token).Set('sRGB')
        sampler.CreateInput('st', Sdf.ValueTypeNames.Float2).ConnectToSource(reader.ConnectableAPI(), 'result')
        sampler.CreateOutput('rgb', Sdf.ValueTypeNames.Float3)
        shader.CreateInput('diffuseColor', Sdf.ValueTypeNames.Color3f).ConnectToSource(sampler.ConnectableAPI(), 'rgb')
        shader.CreateInput('emissiveColor', Sdf.ValueTypeNames.Color3f).ConnectToSource(sampler.ConnectableAPI(), 'rgb')
        shader.CreateOutput('surface', Sdf.ValueTypeNames.Token)
        material.CreateSurfaceOutput().ConnectToSource(shader.ConnectableAPI(), 'surface')
        # RTX uses the MDL render context. Keep a PreviewSurface fallback for
        # other USD viewers, and bind the same actual room texture in OmniPBR.
        mdl = UsdShade.Shader.Define(stage, path+'/Material/MDL')
        mdl.CreateImplementationSourceAttr().Set(UsdShade.Tokens.sourceAsset)
        mdl.SetSourceAsset(Sdf.AssetPath('OmniPBR.mdl'), 'mdl')
        mdl.SetSourceAssetSubIdentifier('OmniPBR', 'mdl')
        mdl.CreateInput('diffuse_texture', Sdf.ValueTypeNames.Asset).Set(texture.resolve().as_uri())
        mdl.CreateInput('diffuse_color_constant', Sdf.ValueTypeNames.Color3f).Set((1, 1, 1))
        mdl.CreateInput('enable_emission', Sdf.ValueTypeNames.Bool).Set(True)
        mdl.CreateInput('emissive_color', Sdf.ValueTypeNames.Color3f).Set((1, 1, 1))
        mdl.CreateInput('emissive_color_texture', Sdf.ValueTypeNames.Asset).Set(texture.resolve().as_uri())
        mdl.CreateInput('emissive_intensity', Sdf.ValueTypeNames.Float).Set(25.0)
        mdl.CreateOutput('out', Sdf.ValueTypeNames.Token)
        material.CreateSurfaceOutput('mdl').ConnectToSource(mdl.ConnectableAPI(), 'out')
        UsdShade.MaterialBindingAPI.Apply(mesh.GetPrim()).Bind(material)
        mesh.GetPrim().CreateAttribute('hospital:roomId', Sdf.ValueTypeNames.String).Set(room['id'])
        rows.append({'room_id': room['id'], 'sign_prim': path,
                     'anchors': {name: actual[name] for name in room.get('scene_anchors', [])},
                     'collision_enabled': False, 'texture_sha256': hashlib.sha256(texture.read_bytes()).hexdigest()})
    proof = {'version': hospital['version'], 'station_geometry_sha256': station_digest(stations),
             'font': str(font_path), 'cjk': cjk, 'rooms': rows,
             'changes': 'visual room signs only; original walls, furniture and robot poses retained'}
    (output/'scene-proof.json').write_text(json.dumps(proof, ensure_ascii=False, indent=2))
    return proof


class LayoutInspection:
    """Named fixed observation cameras for evidence, never a movement command."""
    def __init__(self, root):
        import omni.replicator.core as rep
        from pxr import Gf, UsdGeom
        self.root, self.rep, self.Gf, self.UsdGeom = Path(root), rep, Gf, UsdGeom
        self.views = json.loads((self.root/'configs/hospital-v2.json').read_text()).get('inspection_views', {})
        self.cam = rep.functional.create.camera(position=(0, 0, 1.5), look_at=(1, 0, 1.5), name='HospitalLayoutInspection')
        camera = UsdGeom.Camera(self.cam); camera.GetFocalLengthAttr().Set(15.0)
        camera.GetClippingRangeAttr().Set(Gf.Vec2f(.1, 120))
        self.product = rep.create.render_product(self.cam, (960, 540))
        self.rgb = rep.annotators.get('rgb'); self.rgb.attach(self.product)
        self.product.hydra_texture.set_updates_enabled(False)
        # A saved evidence request must never be replayed after a scene restart.
        command = self.root/'runs/layout-inspection-command.json'
        self.command_id = json.loads(command.read_text()).get('id') if command.exists() else None
        self.pending = None

    def tick(self, sim_time):
        import time
        import numpy as np
        from PIL import Image
        command = self.root/'runs/layout-inspection-command.json'
        if command.exists():
            data = json.loads(command.read_text())
            if data['id'] != self.command_id:
                ident = data['id']
                if not ident.isalnum() or len(ident) > 64 or data['view'] not in self.views:
                    raise ValueError('invalid_layout_inspection_request')
                self.command_id = ident; view = self.views[data['view']]
                matrix = self.Gf.Matrix4d().SetLookAt(self.Gf.Vec3d(*view['eye']), self.Gf.Vec3d(*view['target']), self.Gf.Vec3d(0, 0, 1)).GetInverse()
                self.UsdGeom.Xformable(self.cam).MakeMatrixXform().Set(matrix)
                self.product.hydra_texture.set_updates_enabled(True)
                self.pending = {'command': data, 'view': view, 'warmup': 30}
        if not self.pending:
            return
        self.pending['warmup'] -= 1
        if self.pending['warmup'] > 0:
            return
        pixels = self.rgb.get_data()
        if not isinstance(pixels, np.ndarray) or not pixels.size:
            return
        item = self.pending; folder = self.root/'runs/mhs-v2/layout-inspection'/item['command']['id']
        if folder.exists():
            self.pending = None
            self.product.hydra_texture.set_updates_enabled(False)
            return  # Preserve original proof; an observation cannot crash the sim.
        folder.mkdir(parents=True, exist_ok=False)
        image = folder/'frame.jpg'; Image.fromarray(pixels[:, :, :3]).save(image, quality=92)
        proof = {'request': item['command'], 'camera': item['view'], 'sim_time': sim_time,
                 'capture_wall_time': time.time(), 'sha256': hashlib.sha256(image.read_bytes()).hexdigest(),
                 'image': str(image.relative_to(self.root)), 'robot_motion_commanded': False}
        (folder/'capture.json').write_text(json.dumps(proof, indent=2))
        tmp = self.root/'runs/layout-inspection-ack.tmp'; tmp.write_text(json.dumps(proof)); tmp.replace(self.root/'runs/layout-inspection-ack.json')
        self.pending = None
        self.product.hydra_texture.set_updates_enabled(False)
