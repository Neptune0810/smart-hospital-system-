"""Propose exact current versions from official Ubuntu/ROS apt repositories.

Only apt metadata is refreshed in an isolated short-lived, project-labeled
container. Nothing is installed; the shipped lock is never edited. Inspect the
new proposal and rerun build with a consciously selected lock. Preserve old
build failures and qualify actual navigation after any dependency version change.
"""
import argparse
import hashlib
import json
import os
import re
import subprocess
import time
import uuid
import urllib.parse
from pathlib import Path

HERE = Path(__file__).resolve().parent
PACKAGES = ['ros-humble-navigation2','ros-humble-nav2-bringup',
            'ros-humble-nav2-simple-commander','ros-humble-pointcloud-to-laserscan',
            'python3-pip','python3-yaml','python3-pil','python3-scipy','ffmpeg']
LABEL = 'smart-hospital-public-nav-lock-resolution-v1'
ALLOWED_APT_HOSTS = {'archive.ubuntu.com','security.ubuntu.com','ports.ubuntu.com',
                     'packages.ros.org','repo.ros2.org'}


def run(arguments, **kwargs):
    return subprocess.run(arguments, check=True, text=True, **kwargs)


def select_candidate(policy):
    match = re.search(r'^\s*Candidate:\s*(\S+)\s*$', policy, re.M)
    if not match or match.group(1) == '(none)':
        raise ValueError('official_repository_candidate_missing')
    candidate = match.group(1)
    if not re.fullmatch(r'[A-Za-z0-9.+:~_-]+',candidate):
        raise ValueError('invalid_repository_package_version')
    return candidate


def parse_active_apt_sources(files):
    """Only effective .list deb/deb-src URIs and Deb822 URIs, not comments.

Deb822 Signed-By multiline blocks are parsed as their own field and cannot
accidentally become URIs. Disabled stanzas are retained in source evidence but
do not constitute active apt repositories.
"""
    rows = []
    for filename, text in files.items():
        if filename.endswith('.list') or filename.endswith('/sources.list'):
            for number,line in enumerate(text.splitlines(),1):
                line = line.split('#',1)[0].strip()
                if not line:
                    continue
                match = re.match(r'^(deb|deb-src)\s+(?:\[[^\]]*\]\s+)?(\S+)\s+\S+',line)
                if match:
                    rows.append({'file':filename,'line':number,'type':match.group(1),'uri':match.group(2)})
                elif re.match(r'^deb(?:-src)?(?:\s|$)',line):
                    raise ValueError('malformed_active_apt_source_line')
        elif filename.endswith('.sources'):
            fields,current,start = {},None,1
            def finish():
                if not fields or fields.get('enabled','yes').lower() == 'no':
                    return
                types = fields.get('types','').split()
                if any(value in {'deb','deb-src'} for value in types):
                    uris = fields.get('uris','').split()
                    if not uris:
                        raise ValueError('active_deb822_source_has_no_uri')
                    for uri in uris:
                        rows.append({'file':filename,'line':start,'type':' '.join(types),'uri':uri})
            for number,line in enumerate(text.splitlines()+[''],1):
                if not line.strip():
                    finish(); fields,current,start = {},None,number+1
                    continue
                if line.lstrip().startswith('#'):
                    continue
                if line.startswith((' ','\t')):
                    if current:
                        fields[current] += ' '+line.strip()
                    continue
                match = re.match(r'^([A-Za-z0-9-]+):\s*(.*)$',line)
                if not match:
                    raise ValueError('malformed_deb822_source_field')
                current = match.group(1).lower()
                fields[current] = match.group(2).strip()
    if not rows:
        raise ValueError('active_official_apt_sources_missing')
    for row in rows:
        uri = urllib.parse.urlsplit(row['uri'])
        if (uri.scheme not in {'http','https'} or uri.hostname not in ALLOWED_APT_HOSTS
                or uri.username is not None or uri.password is not None
                or uri.port not in {None,80,443}):
            raise ValueError('base_apt_source_is_not_official_ubuntu_or_ros:'+row['uri'])
    return rows


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--base',default='ros:humble-ros-base-jammy')
    parser.add_argument('--archive-proof',type=Path)
    parser.add_argument('--output',required=True,type=Path)
    args = parser.parse_args()
    output = args.output.resolve()
    output.mkdir(parents=True,exist_ok=False)
    proof = {'status':'resolving','wall_start':time.time(),'packages':PACKAGES,
             'scope':'Official apt candidate proposal only. No installation, image build, GPU, model or navigation test.',
             'old_lock_sha256':hashlib.sha256((HERE/'apt-packages.lock').read_bytes()).hexdigest(),
             'source_script_sha256':hashlib.sha256(Path(__file__).read_bytes()).hexdigest()}
    container_name = 'smart-hospital-nav-lock-'+uuid.uuid4().hex[:12]
    created = False
    try:
        if args.archive_proof:
            archive = json.loads(args.archive_proof.read_text(encoding='utf-8'))
            if (archive.get('repository') != 'docker.io/library/ros'
                    or archive.get('status') not in ('archive_verified','loaded_and_verified')
                    or archive.get('local_tag') != 'smart-hospital-upstream/ros:humble-base'):
                raise ValueError('archive_is_not_verified_public_ros')
            base = archive['local_tag']
            image = json.loads(run(['docker','image','inspect',base],capture_output=True).stdout)[0]
            if image['Id'] != archive['expected_loaded_image_id']:
                raise ValueError('loaded_base_image_id_mismatch')
            proof.update(base_source='verified_public_registry_archive',
                         base_registry_manifest_digest=archive['platform_manifest_digest'],
                         base_config_digest=archive['config_digest'],
                         base_repo_digest='NA: archive load',
                         archive_proof_sha256=hashlib.sha256(args.archive_proof.read_bytes()).hexdigest())
        else:
            if not args.base.startswith(('ros:','ros@sha256:','docker.io/library/ros:','docker.io/library/ros@sha256:')):
                raise ValueError('base_requires_official_ros_repository')
            run(['docker','pull','--platform','linux/amd64',args.base])
            image = json.loads(run(['docker','image','inspect',args.base],capture_output=True).stdout)[0]
            base = next((value for value in image.get('RepoDigests',[]) if value.startswith(('ros@sha256:','docker.io/library/ros@sha256:'))),None)
            if not base:
                raise ValueError('public_base_repo_digest_missing')
            proof.update(base_source='official_repository_digest',base_repo_digest=base)
        if image['Os'] != 'linux' or image['Architecture'] != 'amd64':
            raise ValueError('base_platform_mismatch')
        proof['base_image_id'] = image['Id']
        arguments = ['docker','create','--name',container_name,'--label','project='+LABEL,
                     '--entrypoint','sleep',base,'infinity']
        created_id = run(arguments,capture_output=True).stdout.strip()
        created = True
        proof['container'] = {'id':created_id,'name':container_name,'label':LABEL,'image':image['Id']}
        inspect = json.loads(run(['docker','inspect',container_name],capture_output=True).stdout)[0]
        if (inspect['Id'] != created_id or inspect['Image'] != image['Id']
                or inspect['Config']['Labels'].get('project') != LABEL
                or inspect['Config'].get('Cmd') != ['infinity']):
            raise ValueError('new_resolution_container_identity_failed')
        proof['container'] = {'id':created_id,'name':container_name,'label':LABEL,'image':inspect['Image']}
        run(['docker','start',container_name],capture_output=True)
        # No host mounts, no GPU, no PID namespace sharing or privileged mode.
        sources_code = "from pathlib import Path; import json; files=[Path('/etc/apt/sources.list')]+sorted(Path('/etc/apt/sources.list.d').glob('*')); print(json.dumps({str(p):p.read_text() for p in files if p.is_file() and (p.name=='sources.list' or p.suffix in ('.list','.sources'))}))"
        sources = json.loads(run(['docker','exec',container_name,'python3','-c',sources_code],capture_output=True).stdout)
        (output/'apt-sources.json').write_text(json.dumps(sources,indent=2)+'\n',encoding='utf-8')
        (output/'apt-sources.txt').write_text('\n'.join(name+'\n'+value for name,value in sources.items()),encoding='utf-8')
        active_sources = parse_active_apt_sources(sources)
        proof['active_official_apt_sources'] = active_sources
        updated = subprocess.run(['docker','exec',container_name,'apt-get','update'],capture_output=True,text=True)
        (output/'apt-update.log').write_text(updated.stdout+updated.stderr,encoding='utf-8')
        updated.check_returncode()
        rows = []
        for package in PACKAGES:
            policy = run(['docker','exec',container_name,'apt-cache','policy',package],capture_output=True).stdout
            madison = run(['docker','exec',container_name,'apt-cache','madison',package],capture_output=True).stdout
            (output/(package+'.policy.txt')).write_text(policy,encoding='utf-8')
            (output/(package+'.madison.txt')).write_text(madison,encoding='utf-8')
            candidate = select_candidate(policy)
            rows.append({'package':package,'proposed_version':candidate,'policy':policy,'madison':madison})
        proposal = '\n'.join(row['package']+'='+row['proposed_version'] for row in rows)+'\n'
        (output/'apt-packages.proposed.lock').write_text(proposal,encoding='utf-8')
        base_after = json.loads(run(['docker','image','inspect',base],capture_output=True).stdout)[0]
        if base_after['Id'] != image['Id']:
            raise ValueError('base_tag_changed_during_resolution')
        proof.update(status='proposal_ready',candidates=rows,proposal_sha256=hashlib.sha256(proposal.encode()).hexdigest(),
                     base_image_id_after_resolution=base_after['Id'],selection='apt Candidate from official repositories; proposal requires intentional release-lock update',
                     lock_replaced=False,packages_installed=False)
    except Exception as error:
        proof.update(status='failed',error=type(error).__name__+':'+str(error))
        raise
    finally:
        if created:
            try:
                own = json.loads(run(['docker','inspect',container_name],capture_output=True).stdout)[0]
                if own['Config']['Labels'].get('project') != LABEL or own['Id'] != proof.get('container',{}).get('id'):
                    raise ValueError('resolution_container_cleanup_owner_mismatch')
                run(['docker','rm','-f',container_name],capture_output=True)
                proof['temporary_container_removed'] = True
            except Exception as cleanup_error:
                proof['temporary_container_removed'] = False
                proof['cleanup_error'] = str(cleanup_error)
        proof['wall_end'] = time.time()
        (output/'resolution.json').write_text(json.dumps(proof,indent=2)+'\n',encoding='utf-8')
    print(json.dumps({key:value for key,value in proof.items() if key!='candidates'},indent=2))


if __name__ == '__main__':
    main()
