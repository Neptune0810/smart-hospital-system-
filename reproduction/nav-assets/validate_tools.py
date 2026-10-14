"""Bounded offline negative checks of acquisition/path guards, no services."""
import ast
import hashlib
import gzip
import importlib.util
import json
import tempfile
import time
import struct
import urllib.request
import zlib
from pathlib import Path

HERE = Path(__file__).resolve().parent


def load(name, filename):
    spec = importlib.util.spec_from_file_location(name, HERE/filename)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def main():
    checks = []
    def record(name, function):
        function()
        checks.append({'name': name, 'passed': True})
    def rejected(function, expected):
        try:
            function()
        except expected:
            return
        raise AssertionError('Expected guard rejection')
    assets = load('assets', 'prepare_assets.py')
    scene = load('scene', 'check_scene.py')
    registry = load('registry', 'fetch_ros_archive.py')
    resolver = load('resolver', 'resolve_nav_lock.py')
    lock = json.loads((HERE/'people-assets.lock.json').read_text(encoding='utf-8'))
    record('194_file_frozen_public_origin_lock', lambda: assets.validate_lock(lock))
    record('asset_traversal_lock_rejected', lambda: rejected(lambda: assets.validate_lock(
        dict(lock, files=[dict(row, local='../outside') if index == 0 else row for index,row in enumerate(lock['files'])])), ValueError))
    apt_sources = {'/etc/apt/sources.list': '# http://help.ubuntu.com/community/UpgradeNotes\n# deb https://not-a-repository.example/ ignored\ndeb [arch=amd64 signed-by=/path/key] http://archive.ubuntu.com/ubuntu jammy main # https://comment.example\ndeb-src http://security.ubuntu.com/ubuntu jammy-security main\n',
                   '/etc/apt/sources.list.d/ros.sources': 'Types: deb\nURIs: http://packages.ros.org/ros2/ubuntu\n https://repo.ros2.org/ubuntu\nSuites: jammy\nSigned-By:\n -----BEGIN PGP PUBLIC KEY BLOCK-----\n https://not-an-apt-origin.example/document\n -----END PGP PUBLIC KEY BLOCK-----\n\nTypes: deb\nURIs: http://disabled.example/ubuntu\nEnabled: no\n'}
    def verify_apt_parser():
        parsed = resolver.parse_active_apt_sources(apt_sources)
        if len(parsed) != 4:
            raise AssertionError('effective apt repository count wrong')
    record('apt_comments_signature_and_disabled_source_ignored', verify_apt_parser)
    record('actual_unofficial_list_uri_rejected', lambda: rejected(lambda: resolver.parse_active_apt_sources({'/etc/apt/sources.list':'deb https://unofficial.example/ubuntu jammy main'}),ValueError))
    record('actual_unofficial_deb822_uri_rejected', lambda: rejected(lambda: resolver.parse_active_apt_sources({'/etc/apt/unofficial.sources':'Types: deb\nURIs: https://unofficial.example/ros\nSuites: jammy\n'}),ValueError))
    record('empty_active_apt_repository_rejected', lambda: rejected(lambda: resolver.parse_active_apt_sources({'/etc/apt/sources.list':'# deb https://packages.ros.org/ros2/ubuntu jammy main'}),ValueError))
    with tempfile.TemporaryDirectory(prefix='guard-', dir=HERE) as temporary:
        root = Path(temporary).resolve()
        record('scene_path_traversal_rejected', lambda: rejected(lambda: scene.child(root,'../outside'), ValueError))
        row = dict(lock['files'][0], local='assets/people-v2/probe.usd')
        record('missing_asset_does_not_create_file', lambda: rejected(lambda: assets.acquire(root,row,False), FileNotFoundError))
        target = root/row['local']
        target.parent.mkdir(parents=True)
        target.write_bytes(b'corrupt')
        before = target.read_bytes()
        record('corrupt_existing_asset_rejected', lambda: rejected(lambda: assets.acquire(root,row,True), ValueError))
        if target.read_bytes() != before:
            raise AssertionError('guard overwrote existing object')
        checks.append({'name':'corrupt_existing_asset_unchanged','passed':True})
        outside = dict(row,local='../outside')
        record('acquisition_path_traversal_rejected', lambda: rejected(lambda: assets.acquire(root,outside,True), ValueError))
        payload = b'bounded-uncompressed-layer-fixture'
        compressed = root/'fixture.gz'
        with gzip.open(compressed,'wb') as handle:
            handle.write(payload)
        item = {'digest':'sha256:'+hashlib.sha256(compressed.read_bytes()).hexdigest(),
                'mediaType':'application/vnd.oci.image.layer.v1.tar+gzip'}
        expected = 'sha256:'+hashlib.sha256(payload).hexdigest()
        target = registry.unpack_layer(compressed,item,expected,root,1024)
        if target.read_bytes() != payload:
            raise AssertionError('unpacked bytes changed')
        checks.append({'name':'registry_gzip_diff_id_verified','passed':True})
        record('registry_wrong_diff_id_rejected',lambda: rejected(lambda: registry.unpack_layer(compressed,item,'sha256:'+'0'*64,root,1024),ValueError))
        if target.read_bytes() != payload:
            raise AssertionError('rejection changed correct layer')
        def chunk(kind,value):
            return struct.pack('>I',len(value))+kind+value+struct.pack('>I',zlib.crc32(kind+value)&0xffffffff)
        png = root/'grayscale.png'
        png.write_bytes(b'\x89PNG\r\n\x1a\n'+chunk(b'IHDR',struct.pack('>IIBBBBB',1,1,8,0,0,0,0))+
                        chunk(b'IDAT',zlib.compress(b'\x00\xfe'))+chunk(b'IEND',b''))
        if scene.grayscale_pixel_sha256(png)['sha256'] != hashlib.sha256(b'\xfe').hexdigest():
            raise AssertionError('PNG pixel decoder mismatch')
        checks.append({'name':'stdlib_grayscale_png_pixel_digest','passed':True})
        redirect = registry.SafeRedirect()
        request = urllib.request.Request('https://registry-1.docker.io/v2/library/ros/blobs/probe',headers={'Authorization':'Bearer public-test-value'})
        redirected = redirect.redirect_request(request,None,302,'Found',{},'https://download-cdn.example/image')
        if redirected.get_header('Authorization') is not None:
            raise AssertionError('cross-origin redirect retained credential')
        checks.append({'name':'public_token_removed_on_cross_origin_redirect','passed':True})
    for filename in ('prepare_assets.py','check_scene.py','verify_official_stage.py','build_nav.py','fetch_ros_archive.py','resolve_nav_lock.py'):
        record('syntax:'+filename, lambda name=filename: ast.parse((HERE/name).read_text(encoding='utf-8')))
    result = {'status':'passed','wall_time':time.time(),'checks':checks,
              'scope':'Offline acquisition and path guard checks; no image build, model, GPU, scene or navigation acceptance'}
    target = HERE/('tools-offline-validation-'+str(time.time_ns())+'.json')
    with target.open('x',encoding='utf-8') as handle:
        handle.write(json.dumps(result,indent=2)+'\n')
    print(json.dumps(result,indent=2))


if __name__ == '__main__':
    main()
