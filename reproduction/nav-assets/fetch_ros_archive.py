"""Download the public ROS Docker image over HTTPS and verify a loadable archive.

Use this when Docker's daemon cannot reach Docker Hub but this process can.
Standard HTTP_PROXY/HTTPS_PROXY/NO_PROXY are honored. Anonymous public-pull
tokens exist only in memory and are never written or printed. Default --no-load
produces an archive that may be transferred to a different authorized machine.
"""
import argparse
import concurrent.futures
import gzip
import hashlib
import json
import re
import shutil
import subprocess
import tarfile
import threading
import time
import urllib.parse
import urllib.request
import uuid
from pathlib import Path

REPOSITORY = 'library/ros'
REGISTRY = 'https://registry-1.docker.io/v2/'+REPOSITORY
ACCEPT = ', '.join(['application/vnd.oci.image.index.v1+json',
                    'application/vnd.docker.distribution.manifest.list.v2+json',
                    'application/vnd.oci.image.manifest.v1+json',
                    'application/vnd.docker.distribution.manifest.v2+json'])
PLATFORM = {'os': 'linux', 'architecture': 'amd64'}
LOCAL_TAG = 'smart-hospital-upstream/ros:humble-base'


def sha256(path):
    digest = hashlib.sha256()
    with path.open('rb') as source:
        for chunk in iter(lambda: source.read(1024*1024), b''):
            digest.update(chunk)
    return digest.hexdigest()


def digest_hex(value):
    if not re.fullmatch(r'sha256:[0-9a-f]{64}', value):
        raise ValueError('unsupported_registry_digest')
    return value.split(':', 1)[1]


def write_new_json(path, value):
    with path.open('x', encoding='utf-8') as output:
        output.write(json.dumps(value, indent=2)+'\n')


class SafeRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, response, code, message, headers, new_url):
        if urllib.parse.urlsplit(new_url).scheme != 'https':
            raise ValueError('registry_redirect_requires_https')
        result = super().redirect_request(request,response,code,message,headers,new_url)
        if result and urllib.parse.urlsplit(request.full_url).netloc != urllib.parse.urlsplit(new_url).netloc:
            result.remove_header('Authorization')
        return result


class PublicRegistry:
    def __init__(self):
        self.opener = urllib.request.build_opener(SafeRedirect())
        self.token, self.token_time = None, 0
        self.lock = threading.Lock()

    def authorization(self):
        with self.lock:
            if self.token is None or time.monotonic()-self.token_time > 180:
                query = urllib.parse.urlencode({'service':'registry.docker.io','scope':'repository:'+REPOSITORY+':pull'})
                with self.opener.open('https://auth.docker.io/token?'+query, timeout=60) as response:
                    data = json.loads(response.read(128*1024))
                self.token = data.get('token') or data.get('access_token')
                if not isinstance(self.token,str) or not self.token:
                    raise RuntimeError('public_pull_token_missing')
                self.token_time = time.monotonic()
            return 'Bearer '+self.token

    def open(self, url, timeout=180):
        # Only the official registry request gets a public-pull credential.
        if not url.startswith(REGISTRY+'/'):
            raise ValueError('unexpected_registry_url')
        request = urllib.request.Request(url,headers={'Authorization':self.authorization(),'Accept':ACCEPT})
        return self.opener.open(request,timeout=timeout)

    def manifest(self, reference):
        if not re.fullmatch(r'(sha256:[0-9a-f]{64}|[A-Za-z0-9_.-]{1,128})', reference):
            raise ValueError('invalid_manifest_reference')
        with self.open(REGISTRY+'/manifests/'+reference, timeout=60) as response:
            raw = response.read(4*1024*1024+1)
        if len(raw) > 4*1024*1024:
            raise RuntimeError('registry_manifest_size_limit')
        digest = 'sha256:'+hashlib.sha256(raw).hexdigest()
        if reference.startswith('sha256:') and digest != reference:
            raise RuntimeError('registry_manifest_sha256_mismatch')
        return json.loads(raw), raw, digest

    def blob(self, item, cache):
        name = digest_hex(item['digest'])
        if not isinstance(item.get('size'),int) or not 0 < item['size'] <= 2*1024*1024*1024:
            raise ValueError('invalid_registry_blob_size')
        target = cache/'blobs'/(name+'.blob')
        if cache not in target.resolve().parents:
            raise ValueError('registry_blob_symlink_outside_owned_cache')
        target.parent.mkdir(exist_ok=True)
        if target.exists():
            if target.stat().st_size != item['size'] or sha256(target) != name:
                raise ValueError('existing_registry_blob_integrity_failed:'+name)
            print('BLOB_REUSED '+name[:12]+' '+str(item['size']),flush=True)
            return target
        last_error = None
        for attempt in range(1,4):
            temporary = target.with_name(target.name+'.partial-'+uuid.uuid4().hex)
            try:
                digest, count = hashlib.sha256(), 0
                with self.open(REGISTRY+'/blobs/'+item['digest']) as response, temporary.open('xb') as output:
                    for chunk in iter(lambda: response.read(1024*1024), b''):
                        count += len(chunk)
                        if count > item['size']:
                            raise ValueError('registry_blob_size_exceeded')
                        output.write(chunk); digest.update(chunk)
                if count != item['size'] or digest.hexdigest() != name:
                    raise ValueError('downloaded_registry_blob_integrity_failed')
                if target.exists():
                    raise FileExistsError('concurrent_registry_cache_writer')
                temporary.replace(target)
                print('BLOB_VERIFIED '+name[:12]+' '+str(count),flush=True)
                return target
            except Exception as error:
                last_error = error
                print('BLOB_RETRY '+name[:12]+' '+str(attempt)+' '+type(error).__name__,flush=True)
                if attempt < 3:
                    time.sleep(attempt*2)
            finally:
                temporary.unlink(missing_ok=True)
        raise RuntimeError('registry_blob_fetch_failed:'+name+':'+type(last_error).__name__) from last_error


def unpack_layer(compressed, item, diff_id, cache, maximum):
    expected = digest_hex(diff_id)
    directory = cache/'layers'/digest_hex(item['digest'])
    if cache not in directory.resolve().parents:
        raise ValueError('layer_directory_symlink_outside_owned_cache')
    directory.mkdir(parents=True, exist_ok=True)
    target = directory/'layer.tar'
    if target.exists():
        if target.stat().st_size > maximum or sha256(target) != expected:
            raise ValueError('existing_unpacked_layer_integrity_failed')
        return target
    media = item.get('mediaType','')
    if media.endswith(('+gzip','.gzip')):
        source = gzip.open(compressed,'rb')
    elif media.endswith(('.tar','rootfs.diff.tar')):
        source = compressed.open('rb')
    else:
        raise ValueError('unsupported_registry_layer_compression:'+media)
    temporary = target.with_name('layer.tar.partial-'+uuid.uuid4().hex)
    count, digest = 0, hashlib.sha256()
    try:
        with source, temporary.open('xb') as output:
            for chunk in iter(lambda: source.read(1024*1024), b''):
                count += len(chunk)
                if count > maximum:
                    raise ValueError('unpacked_layer_size_limit')
                output.write(chunk); digest.update(chunk)
        if digest.hexdigest() != expected:
            raise ValueError('uncompressed_layer_diff_id_mismatch')
        if target.exists():
            raise FileExistsError('concurrent_layer_cache_writer')
        temporary.replace(target)
        return target
    finally:
        temporary.unlink(missing_ok=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', required=True, type=Path)
    parser.add_argument('--cache', required=True, type=Path)
    parser.add_argument('--output', required=True, type=Path)
    parser.add_argument('--tag',default='humble-ros-base-jammy')
    parser.add_argument('--workers',type=int,default=3)
    parser.add_argument('--max-compressed-bytes',type=int,default=2*1024*1024*1024)
    parser.add_argument('--max-unpacked-bytes',type=int,default=6*1024*1024*1024)
    choice = parser.add_mutually_exclusive_group()
    choice.add_argument('--load',dest='load',action='store_true')
    choice.add_argument('--no-load',dest='load',action='store_false')
    parser.set_defaults(load=False)
    args = parser.parse_args()
    if not 1 <= args.workers <= 4:
        raise ValueError('workers_must_be_1_to_4')
    root, cache, output = args.root.resolve(),args.cache.resolve(),args.output.resolve()
    if not root.is_dir() or root not in cache.parents or root not in output.parents:
        raise ValueError('cache_and_proof_must_be_inside_explicit_root')
    if output.exists():
        raise FileExistsError('proof_output_already_exists')
    output.parent.mkdir(parents=True,exist_ok=True)
    cache.mkdir(parents=True,exist_ok=True)
    owner = cache/'registry-cache-owner.json'
    identity = {'tool':'smart-hospital-public-ros-archive-v1','repository':'docker.io/'+REPOSITORY,
                'tag':args.tag,'platform':PLATFORM,'local_tag':LOCAL_TAG}
    if owner.exists():
        if cache not in owner.resolve().parents:
            raise ValueError('owner_marker_symlink_outside_owned_cache')
        if json.loads(owner.read_text(encoding='utf-8')) != identity:
            raise ValueError('cache_owner_or_requested_source_mismatch')
    elif any(cache.iterdir()):
        raise ValueError('existing_cache_has_no_project_owner_marker')
    else:
        write_new_json(owner,identity)
    proof = dict(identity,status='fetching',started_at=time.time(),public_pull_token_persisted=False,
                 proxy_configuration='standard HTTP_PROXY/HTTPS_PROXY/NO_PROXY environment; no fixed proxy',
                 scope='Official ROS image bytes and Docker archive provenance. No project service or GPU execution.')
    try:
        registry = PublicRegistry()
        lock_path = cache/'registry-lock.json'
        if lock_path.exists():
            if cache not in lock_path.resolve().parents:
                raise ValueError('registry_lock_symlink_outside_owned_cache')
            source = json.loads(lock_path.read_text(encoding='utf-8'))
            if source.get('tag') != args.tag or source.get('platform') != PLATFORM:
                raise ValueError('locked_registry_source_mismatch')
            manifest,raw,manifest_digest = registry.manifest(source['platform_manifest_digest'])
            if manifest_digest != source['platform_manifest_digest']:
                raise RuntimeError('locked_platform_manifest_mismatch')
        else:
            index,index_raw,index_digest = registry.manifest(args.tag)
            (cache/'tag-manifest.json').write_bytes(index_raw)
            if 'manifests' in index:
                candidates = [item for item in index['manifests'] if all(item.get('platform',{}).get(k)==v for k,v in PLATFORM.items())]
                if len(candidates) != 1:
                    raise RuntimeError('linux_amd64_manifest_missing_or_ambiguous')
                manifest,raw,manifest_digest = registry.manifest(candidates[0]['digest'])
            else:
                manifest,raw,manifest_digest = index,index_raw,index_digest
            source = {'tag':args.tag,'tag_or_index_manifest_digest':index_digest,
                      'platform_manifest_digest':manifest_digest,'platform':PLATFORM}
            write_new_json(lock_path,source)
        (cache/'platform-manifest.json').write_bytes(raw)
        items = [manifest['config'],*manifest['layers']]
        compressed_total = sum(item['size'] for item in items)
        if compressed_total > args.max_compressed_bytes:
            raise ValueError('compressed_image_size_limit')
        proof.update(source,compressed_bytes=compressed_total,config_digest=manifest['config']['digest'],
                     layer_count=len(manifest['layers']),registry_manifest=manifest,
                     expected_loaded_image_id=manifest['config']['digest'])
        print('SOURCE_LOCKED '+manifest_digest+' '+str(compressed_total),flush=True)
        with concurrent.futures.ThreadPoolExecutor(max_workers=args.workers) as pool:
            blobs = list(pool.map(lambda item:registry.blob(item,cache),items))
        config_raw = blobs[0].read_bytes()
        config = json.loads(config_raw)
        if any(config.get(k)!=v for k,v in PLATFORM.items()):
            raise ValueError('downloaded_config_platform_mismatch')
        diff_ids = config.get('rootfs',{}).get('diff_ids',[])
        if len(diff_ids) != len(manifest['layers']):
            raise ValueError('config_layer_count_mismatch')
        config_path = cache/(digest_hex(manifest['config']['digest'])+'.json')
        config_path.write_bytes(config_raw)
        layers,unpacked = [],0
        for blob,item,diff_id in zip(blobs[1:],manifest['layers'],diff_ids):
            path = unpack_layer(blob,item,diff_id,cache,args.max_unpacked_bytes-unpacked)
            unpacked += path.stat().st_size
            layers.append(path)
        proof['unpacked_bytes'] = unpacked
        proof['uncompressed_layer_diff_ids_verified'] = diff_ids
        manifest_path = cache/'manifest.json'
        manifest_path.write_text(json.dumps([{'Config':config_path.name,'RepoTags':[LOCAL_TAG],
                                            'Layers':[path.relative_to(cache).as_posix() for path in layers]}])+'\n',encoding='utf-8')
        archive = cache/'official-ros-image.tar'
        temporary = cache/('archive.partial-'+uuid.uuid4().hex+'.tar')
        try:
            with tarfile.open(temporary,'w') as tar:
                for path in [config_path,manifest_path,*layers]:
                    tar.add(path,arcname=path.relative_to(cache).as_posix(),recursive=False)
            temporary.replace(archive)
        finally:
            temporary.unlink(missing_ok=True)
        proof.update(status='archive_verified',archive_path=str(archive),archive_bytes=archive.stat().st_size,
                     archive_sha256=sha256(archive),repository_digest_after_docker_load='NA: Docker archive load does not preserve a public RepoDigest')
        if args.load:
            subprocess.run(['docker','load','-i',str(archive)],check=True)
            image = json.loads(subprocess.run(['docker','image','inspect',LOCAL_TAG],check=True,capture_output=True,text=True).stdout)[0]
            if image['Id'] != proof['expected_loaded_image_id']:
                raise ValueError('loaded_image_id_does_not_match_official_config')
            proof.update(status='loaded_and_verified',loaded_image_id=image['Id'],actual_repository_digests=image.get('RepoDigests',[]))
    except Exception as error:
        proof.update(status='failed',error=type(error).__name__+':'+str(error))
        raise
    finally:
        proof['ended_at'] = time.time()
        write_new_json(output,proof)
    print(json.dumps({key:value for key,value in proof.items() if key != 'registry_manifest'},indent=2))


if __name__ == '__main__':
    main()
