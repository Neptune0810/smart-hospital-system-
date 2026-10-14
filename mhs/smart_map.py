"""Project-only occupancy map for the smart-ward cabinet overlay.

The official map is never edited.  Cabinet collision bounds, including the
closed-envelope sliding doors, are conservatively projected into a copy.
This module has no Isaac imports and can be checked before a GPU launch.
"""
import hashlib
import json
import math
import re
from pathlib import Path

from PIL import Image, ImageDraw


def load_map_metadata(path):
    path = Path(path)
    text = path.read_text(encoding='utf-8')
    def value(name):
        match = re.search(r'^\s*'+re.escape(name)+r'\s*:\s*([^#\r\n]+)', text, re.M)
        if not match:
            raise ValueError('map_field_missing:'+name)
        return match.group(1).strip()
    origin = json.loads(value('origin'))
    resolution = float(value('resolution'))
    if resolution <= 0 or len(origin) != 3 or abs(float(origin[2])) > 1e-7:
        raise ValueError('unsupported_map_geometry')
    if int(value('negate')) != 0:
        raise ValueError('negated_map_not_supported')
    image = value('image').strip('\"\'')
    return {'yaml': path, 'image': (path.parent/image).resolve(),
            'origin': [float(v) for v in origin], 'resolution': resolution,
            'occupied_thresh': float(value('occupied_thresh')),
            'free_thresh': float(value('free_thresh'))}


def bounds_to_pixels(bounds, metadata, size):
    """Return inclusive conservative pixels, rejecting out-of-map geometry."""
    x0, y0, x1, y1 = map(float, bounds)
    if not all(math.isfinite(v) for v in (x0, y0, x1, y1)) or x0 >= x1 or y0 >= y1:
        raise ValueError('invalid_collision_bounds')
    ox, oy = metadata['origin'][:2]
    resolution = metadata['resolution']
    width, height = size
    left = math.floor((x0-ox)/resolution)
    right = math.ceil((x1-ox)/resolution)-1
    top = height-math.ceil((y1-oy)/resolution)
    bottom = height-1-math.floor((y0-oy)/resolution)
    if left < 0 or top < 0 or right >= width or bottom >= height:
        raise ValueError('cabinet_outside_navigation_map')
    return [left, top, right, bottom]


def write_overlay_map(source_yaml, output_directory, cabinets, protected_points=(), clearance=.70):
    """Rasterize real cabinet footprints and publish a self-contained map copy.

    ``cabinets`` contains {id, collision_bounds_xy}; all collision components
    must stay within that envelope.  ``protected_points`` is the frozen set of
    navigation goals: adding a cabinet must not reduce their existing clearance.
    """
    metadata = load_map_metadata(source_yaml)
    original = Image.open(metadata['image']).convert('L')
    raster = original.copy()
    draw = ImageDraw.Draw(raster)
    overlays = []
    identifiers = set()
    for cabinet in cabinets:
        identifier = cabinet['id']
        if identifier in identifiers:
            raise ValueError('duplicate_cabinet_id')
        identifiers.add(identifier)
        bounds = [float(v) for v in cabinet['collision_bounds_xy']]
        for point in protected_points:
            x, y = float(point['x']), float(point['y'])
            nearest_x = min(max(x, bounds[0]), bounds[2])
            nearest_y = min(max(y, bounds[1]), bounds[3])
            if math.hypot(x-nearest_x, y-nearest_y) < clearance:
                raise ValueError('cabinet_intrudes_navigation_clearance:'+identifier)
        pixels = bounds_to_pixels(bounds, metadata, raster.size)
        draw.rectangle(pixels, fill=0)
        overlays.append({'id': identifier, 'collision_bounds_xy': bounds, 'pixels': pixels})
    output = Path(output_directory)
    output.mkdir(parents=True, exist_ok=True)
    image_path = output/'carter_hospital_smart_ward.png'
    yaml_path = output/'carter_hospital_smart_ward.yaml'
    temp_image = output/'carter_hospital_smart_ward.tmp.png'
    raster.save(temp_image)
    temp_image.replace(image_path)
    content = '\n'.join([
        'image: '+image_path.name,
        'resolution: '+str(metadata['resolution']),
        'origin: '+json.dumps(metadata['origin']),
        'negate: 0',
        'occupied_thresh: '+str(metadata['occupied_thresh']),
        'free_thresh: '+str(metadata['free_thresh']), '',
    ])
    temp_yaml = output/'carter_hospital_smart_ward.tmp.yaml'
    temp_yaml.write_text(content, encoding='utf-8')
    temp_yaml.replace(yaml_path)
    source_bytes = metadata['image'].read_bytes()
    proof = {'source_yaml': str(Path(source_yaml)), 'source_image': str(metadata['image']),
             'source_image_sha256': hashlib.sha256(source_bytes).hexdigest(),
             'map_yaml': str(yaml_path), 'map_image': str(image_path),
             'map_image_sha256': hashlib.sha256(image_path.read_bytes()).hexdigest(),
             'size': list(raster.size), 'resolution': metadata['resolution'], 'origin': metadata['origin'],
             'overlays': overlays, 'original_map_modified': False,
             'scope': 'Cabinet collision-envelope projection. Actual Nav2 and physical arrival require separate qualification.'}
    tmp = output/'manifest.tmp.json'
    tmp.write_text(json.dumps(proof, ensure_ascii=False, indent=2), encoding='utf-8')
    tmp.replace(output/'manifest.json')
    return proof
