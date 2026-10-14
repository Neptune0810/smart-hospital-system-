"""Build the Nav2 image from public ROS infrastructure; no GPU or service start.

The public base tag is resolved before building. The selected repository digest
and every resolved apt/pip package are written to the caller's new proof folder.
Direct apt versions fail closed if a repository no longer provides them.
"""
import argparse
import hashlib
import json
import os
import subprocess
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent


def run(arguments, **kwargs):
    return subprocess.run(arguments, check=True, text=True, **kwargs)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--image', default='smart-hospital:humble-repro-v1')
    parser.add_argument('--base', default='ros:humble-ros-base-jammy')
    parser.add_argument('--archive-proof', type=Path, help='Verified public-registry archive proof, after docker load on this machine')
    parser.add_argument('--output', required=True, type=Path)
    arguments = parser.parse_args()
    if not arguments.base.startswith(('ros:', 'ros@sha256:', 'docker.io/library/ros:','docker.io/library/ros@sha256:')):
        raise ValueError('base_must_be_official_ros_repository')
    output = arguments.output.resolve()
    output.mkdir(parents=True, exist_ok=False)
    proof = {'status': 'building', 'wall_start': time.time(), 'architecture': 'linux/amd64',
             'base_requested': arguments.base, 'image_tag': arguments.image,
             'scope': 'Public ROS base and dependency installation; not a navigation or Agent acceptance run',
             'source_sha256': {p.name: hashlib.sha256(p.read_bytes()).hexdigest()
                               for p in (HERE/'Dockerfile', HERE/'apt-packages.lock', HERE/'requirements-nav.lock.txt')}}
    target = output/'build.json'
    target.write_text(json.dumps(proof, indent=2)+'\n', encoding='utf-8')
    try:
        if arguments.archive_proof:
            archive = json.loads(arguments.archive_proof.read_text(encoding='utf-8'))
            if archive.get('repository') != 'docker.io/library/ros' or archive.get('status') not in ('archive_verified','loaded_and_verified'):
                raise ValueError('base_archive_proof_is_not_verified_public_ros')
            local_tag = archive['local_tag']
            if local_tag != 'smart-hospital-upstream/ros:humble-base':
                raise ValueError('base_archive_local_tag_is_not_project_owned_ros_import')
            image = json.loads(run(['docker','image','inspect',local_tag],capture_output=True).stdout)[0]
            if image['Id'] != archive['expected_loaded_image_id'] or image['Architecture'] != 'amd64' or image['Os'] != 'linux':
                raise ValueError('loaded_public_base_does_not_match_config_image_id')
            # A bare sha256 config ID in FROM is interpreted as a registry
            # repository by BuildKit. This local-only tag is tied to the exact
            # public config digest by independent checks before and after build.
            digest = local_tag
            proof.update(base_source='verified_public_registry_archive',
                         base_registry_manifest_digest=archive['platform_manifest_digest'],
                         base_tag_or_index_manifest_digest=archive['tag_or_index_manifest_digest'],
                         base_config_digest=archive['config_digest'],
                         base_build_reference=local_tag,
                         base_loaded_image_id_before_build=image['Id'],
                         archive_builder='classic Docker builder selected only in this build subprocess',
                         base_resolved_digest='NA: Docker load does not preserve a public RepoDigest',
                         base_actual_repository_digests=image.get('RepoDigests',[]),
                         base_archive_sha256=archive['archive_sha256'],
                         base_archive_proof_sha256=hashlib.sha256(arguments.archive_proof.read_bytes()).hexdigest())
        else:
            run(['docker', 'pull', '--platform', 'linux/amd64', arguments.base])
            image = json.loads(run(['docker', 'image', 'inspect', arguments.base], capture_output=True).stdout)[0]
            repo_digests = image.get('RepoDigests', [])
            digest = next((item for item in repo_digests if item.startswith(('ros@sha256:', 'docker.io/library/ros@sha256:'))), None)
            if not digest:
                raise RuntimeError('official_base_repository_digest_missing')
            proof['base_resolved_digest'] = digest
        proof['base_image_id'] = image['Id']
        target.write_text(json.dumps(proof, indent=2)+'\n', encoding='utf-8')
        run(['docker', 'build', '--platform', 'linux/amd64', '--pull=false',
             '--build-arg', 'ROS_BASE_IMAGE='+digest, '-t', arguments.image, str(HERE)],
            **({'env': dict(os.environ, DOCKER_BUILDKIT='0')} if arguments.archive_proof else {}))
        if arguments.archive_proof:
            base_after = json.loads(run(['docker','image','inspect',local_tag],capture_output=True).stdout)[0]
            if base_after['Id'] != archive['expected_loaded_image_id']:
                raise ValueError('verified_public_base_tag_changed_during_build')
            proof['base_loaded_image_id_after_build'] = base_after['Id']
        runtime = json.loads(run(['docker','image','inspect',arguments.image],capture_output=True).stdout)[0]
        proof['image_id'] = runtime['Id']
        proof['image_repository_digests'] = runtime.get('RepoDigests', [])
        # /opt dependency records are baked into this new image. No host project
        # files, daemon state, GPU, ROS network or application services are used.
        for filename in ('apt-resolved.txt', 'pip-resolved.txt'):
            data = run(['docker', 'run', '--rm', '--network', 'none', '--entrypoint', 'cat',
                        arguments.image, '/opt/smart-hospital-dependencies/'+filename], capture_output=True).stdout
            (output/filename).write_text(data, encoding='utf-8')
            proof[filename+'_sha256'] = hashlib.sha256((output/filename).read_bytes()).hexdigest()
        proof['status'] = 'built'
    except Exception as error:
        proof['status'] = 'failed'
        proof['error'] = str(error)
        raise
    finally:
        proof['wall_end'] = time.time()
        target.write_text(json.dumps(proof, indent=2)+'\n', encoding='utf-8')
    print(json.dumps(proof, indent=2))


if __name__ == '__main__':
    main()
