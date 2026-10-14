"""Allowlisted cameras; never serve an expired or mismatched frame."""
import hashlib
import json
import time

SOURCES = {'third_person': ('latest.jpg', 'latest-frame.json'),
           'first_person': ('first-person.jpg', 'first-person-frame.json')}


def read_frame(root, view='third_person', now=None):
    if view not in SOURCES:
        raise ValueError('unknown_camera_view')
    image, metadata = SOURCES[view]
    data = json.loads((root/'runs'/metadata).read_text())
    raw = (root/'runs'/image).read_bytes()
    age = (time.time() if now is None else now)-data['capture_wall_time']
    if not 0 <= age <= 5 or hashlib.sha256(raw).hexdigest() != data['sha256']:
        raise ValueError('frame_stale_or_replaced')
    return dict(data, view=view), raw
