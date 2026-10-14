"""Check frozen source assets, then this startup's actual USD-derived map.

--generated requires --since (startup wall time); historical map/overlay state
must not qualify. This is geometry/provenance verification, never arrival PASS.
"""
import argparse
import hashlib
import json
import math
import struct
import time
import zlib
from pathlib import Path

HERE = Path(__file__).resolve().parent


def sha256(path):
    digest = hashlib.sha256()
    with path.open('rb') as handle:
        for chunk in iter(lambda: handle.read(1024*1024), b''):
            digest.update(chunk)
    return digest.hexdigest()


def child(root, value):
    path = (root/value).resolve()
    if root not in path.parents:
        raise ValueError('scene_path_outside_root')
    return path


def grayscale_pixel_sha256(path):
    """Decode our generated 8-bit grayscale PNG without a Pillow dependency.

PNG/zlib encoder versions can change bytes while occupancy pixels stay exact.
The map producer always saves a non-interlaced mode-L image. Reject other
formats, CRC failures, wrong dimensions or excess decompressed data.
"""
    data = path.read_bytes()
    if data[:8] != b'\x89PNG\r\n\x1a\n':
        raise ValueError('generated_map_not_png')
    cursor, compressed, dimensions = 8, bytearray(), None
    while cursor < len(data):
        length = struct.unpack('>I', data[cursor:cursor+4])[0]
        kind = data[cursor+4:cursor+8]
        contents = data[cursor+8:cursor+8+length]
        crc = struct.unpack('>I',data[cursor+8+length:cursor+12+length])[0]
        if zlib.crc32(kind+contents) & 0xffffffff != crc:
            raise ValueError('generated_map_png_crc_failed')
        cursor += 12+length
        if kind == b'IHDR':
            width,height,depth,color,compression,filtering,interlace = struct.unpack('>IIBBBBB',contents)
            if depth != 8 or color != 0 or compression or filtering or interlace or not 0 < width*height < 4_000_000:
                raise ValueError('generated_map_requires_noninterlaced_8bit_grayscale')
            dimensions = width,height
        elif kind == b'IDAT':
            compressed.extend(contents)
        elif kind == b'IEND':
            break
    if dimensions is None:
        raise ValueError('generated_map_png_header_missing')
    width,height = dimensions
    expected = height*(width+1)
    decoder = zlib.decompressobj()
    packed = decoder.decompress(compressed, expected+1)
    if len(packed) != expected or not decoder.eof or decoder.unconsumed_tail or decoder.unused_data:
        raise ValueError('generated_map_png_data_size_mismatch')
    pixels, previous = bytearray(), bytearray(width)
    for index in range(height):
        start = index*(width+1)
        filter_type = packed[start]
        row = bytearray(packed[start+1:start+1+width])
        if filter_type > 4:
            raise ValueError('generated_map_png_filter_invalid')
        for x in range(width):
            left = row[x-1] if x else 0
            above = previous[x]
            corner = previous[x-1] if x else 0
            if filter_type == 1:
                predictor = left
            elif filter_type == 2:
                predictor = above
            elif filter_type == 3:
                predictor = (left+above)//2
            elif filter_type == 4:
                center = left+above-corner
                distances = abs(center-left),abs(center-above),abs(center-corner)
                predictor = left if distances[0] <= distances[1] and distances[0] <= distances[2] else above if distances[1] <= distances[2] else corner
            else:
                predictor = 0
            row[x] = (row[x]+predictor) & 255
        pixels.extend(row); previous = row
    return {'sha256': hashlib.sha256(pixels).hexdigest(), 'size': [width,height],
            'encoding': 'row-major grayscale8 occupancy pixels'}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--generated', action='store_true')
    parser.add_argument('--since', type=float)
    args = parser.parse_args()
    root = args.root.resolve()
    lock = json.loads((HERE/'scene-inputs.lock.json').read_text(encoding='utf-8'))
    proof = {'wall_time': time.time(), 'root': str(root), 'generated_requested': args.generated,
             'scope': 'Source integrity and newly USD-generated cabinet/map provenance; real Nav2/arrival needs separate tests',
             'files': [], 'errors': []}
    for row in lock['files']:
        path = child(root, row['path'])
        actual = sha256(path) if path.is_file() else None
        proof['files'].append({'path': row['path'], 'expected_sha256': row['sha256'], 'actual_sha256': actual})
        if actual != row['sha256']:
            proof['errors'].append('source_integrity_failed:'+row['path'])
    image = child(root, lock['official_map']['image'])
    if image.exists():
        header = image.read_bytes()[:24]
        if header[:8] != b'\x89PNG\r\n\x1a\n' or list(struct.unpack('>II', header[16:24])) != lock['official_map']['size']:
            proof['errors'].append('map_size_or_format_mismatch')
    if args.generated:
        if args.since is None or not math.isfinite(args.since) or args.since > time.time():
            raise ValueError('generated_requires_valid_startup_wall_time')
        folder = child(root, 'runs/smart-ward-v1/scene')
        paths = [folder/'map/manifest.json', folder/'map/carter_hospital_smart_ward.png',
                 folder/'map/carter_hospital_smart_ward.yaml', folder/'layout-proof.json', folder/'placement-audit.json']
        for path in paths:
            if not path.is_file() or path.stat().st_mtime < args.since:
                proof['errors'].append('map_or_scene_not_generated_this_startup:'+str(path.relative_to(root)))
        if not proof['errors']:
            manifest = json.loads(paths[0].read_text(encoding='utf-8'))
            layout = json.loads(paths[3].read_text(encoding='utf-8'))
            map_sha = sha256(paths[1])
            pixel_digest = grayscale_pixel_sha256(paths[1])
            if (manifest.get('source_image_sha256') != lock['official_map']['sha256']
                    or manifest.get('map_image_sha256') != map_sha
                    or pixel_digest['sha256'] != lock['expected_generated_map']['grayscale_pixel_sha256']
                    or pixel_digest['size'] != lock['official_map']['size']
                    or manifest.get('original_map_modified') is not False):
                proof['errors'].append('generated_map_mismatch_or_original_map_changed')
            actual = {row['id']: row for row in manifest.get('overlays', [])}
            expected = lock['expected_generated_map']['cabinet_envelopes']
            if set(actual) != set(expected):
                proof['errors'].append('generated_cabinet_set_mismatch')
            else:
                for identifier, bounds in expected.items():
                    coordinates = actual[identifier].get('collision_bounds_xy', [])
                    if len(coordinates) != 4 or any(abs(x-y)>1e-7 for x,y in zip(coordinates,bounds)):
                        proof['errors'].append('generated_cabinet_placement_mismatch:'+identifier)
            overlay = Path(layout.get('overlay_path', '')).resolve()
            if (folder not in overlay.parents or not overlay.is_file() or overlay.stat().st_mtime < args.since
                    or layout.get('overlay_sha256') != sha256(overlay)
                    or layout.get('collision_enabled') is not True
                    or layout.get('official_root_layer_saved') is not False):
                proof['errors'].append('new_project_overlay_integrity_failed')
            proof['generated'] = {'map_sha256': map_sha, 'cabinet_envelopes': actual,
                                  'occupancy_pixels': pixel_digest,
                                  'overlay_path': str(overlay), 'startup_wall_time': args.since,
                                  'absolute_metadata_paths_allowed_only_for_this_local_startup': True}
    proof['status'] = 'passed' if not proof['errors'] else 'failed'
    output = args.output.resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open('x', encoding='utf-8') as handle:
        handle.write(json.dumps(proof, ensure_ascii=False, indent=2)+'\n')
    print(json.dumps({'status': proof['status'], 'checked_files': len(proof['files']), 'errors': proof['errors']}, ensure_ascii=False))
    if proof['errors']:
        raise SystemExit(1)


if __name__ == '__main__':
    main()
