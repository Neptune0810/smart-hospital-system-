"""Display-only shoulder camera. Reads a map without changing navigation."""
import json
import math
import re
import time
from pathlib import Path


class FollowingCamera:
    def __init__(self, root):
        self.root = Path(root)
        self.map = None
        self.next_map_attempt = 0.

    def _load_map(self):
        if self.map is not None or time.monotonic() < self.next_map_attempt:
            return
        self.next_map_attempt = time.monotonic()+5.
        try:
            from PIL import Image
            from runtime import map_yaml
            path = map_yaml(self.root)
            text = path.read_text(encoding='utf-8')
            resolution = float(re.search(r'resolution:\s*([^\s#]+)', text).group(1))
            origin = json.loads(re.search(r'origin:\s*(\[[^\]]+\])', text).group(1))
            image_name = re.search(r'image:\s*([^\s#]+)', text).group(1).strip('"\'')
            if abs(origin[2]) > 1e-7 or resolution <= 0:
                return
            self.map = (Image.open(path.parent/image_name).convert('L'), resolution, origin[:2])
        except (OSError, ValueError, AttributeError):
            return

    def _free(self, x, y):
        image, resolution, origin = self.map
        px = math.floor((x-origin[0])/resolution)
        py = image.height-1-math.floor((y-origin[1])/resolution)
        margin = max(1, math.ceil(.10/resolution))
        if px < margin or py < margin or px+margin >= image.width or py+margin >= image.height:
            return False
        return all(image.getpixel((px+dx, py+dy)) > 240
                   for dx, dy in ((0,0),(-margin,0),(margin,0),(0,-margin),(0,margin)))

    def _line_clear(self, start, end):
        if self.map is None:
            return False
        distance = math.dist(start[:2], end[:2])
        samples = max(2, math.ceil(distance/.10))
        return all(self._free(start[0]+(end[0]-start[0])*i/samples,
                              start[1]+(end[1]-start[1])*i/samples)
                   for i in range(1, samples+1))

    def pose(self, transform, settings):
        from pxr import Gf
        self._load_map()
        base = transform.ExtractTranslation()
        forward = transform.TransformDir(Gf.Vec3d(1, 0, 0)).GetNormalized()
        left = transform.TransformDir(Gf.Vec3d(0, 1, 0)).GetNormalized()
        target = Gf.Vec3d(base[0]+forward[0]*settings['look_ahead_m'],
                         base[1]+forward[1]*settings['look_ahead_m'],
                         base[2]+settings['look_at_height_m'])
        back = settings['back_distance_m']
        side = settings['side_offset_m']
        eye = None
        choice = 'overhead_clearance_fallback'
        # Prefer a readable shoulder view, then move inward near walls/corners.
        # Floor occupancy is a conservative display heuristic, not a 3D proof.
        for distance in (back, back*.8, back*.6, back*.4, .4):
            for offset in (side, 0., -side):
                candidate = Gf.Vec3d(base[0]-forward[0]*distance+left[0]*offset,
                                      base[1]-forward[1]*distance+left[1]*offset,
                                      base[2]+settings['height_m'])
                if self._line_clear(list(base), list(candidate)):
                    eye = candidate
                    choice = 'map_clear_shoulder_follow'
                    break
            if eye is not None:
                break
        if eye is None:
            eye = Gf.Vec3d(base[0], base[1], base[2]+settings['height_m'])
            target = Gf.Vec3d(base[0], base[1], base[2]+settings['look_at_height_m'])
            up = Gf.Vec3d(forward[0], forward[1], 0)
        else:
            up = Gf.Vec3d(0, 0, 1)
        matrix = Gf.Matrix4d().SetLookAt(eye, target, up).GetInverse()
        proof = dict(eye_world=list(eye), look_at_world=list(target), view=choice,
                     clearance='read_only_floor_map_heuristic', robot_motion_commanded=False)
        return matrix, proof
