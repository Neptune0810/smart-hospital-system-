"""Capture project source/runtime hashes and dependency versions, no credentials."""
import hashlib,json,subprocess,time
from .manage import ROOT,RUNTIME,HOME,process,container

def command(args,cwd=None):
    return subprocess.check_output(args,cwd=cwd or ROOT,text=True).strip()

def main():
    directory=ROOT/'runs/mhs-v2/acceptance'/('provenance-'+time.strftime('%Y%m%d-%H%M%S'))
    directory.mkdir(parents=True)
    paths=[*sorted((ROOT/'mhs').rglob('*.py')),*sorted((ROOT/'mhs').rglob('*.sh')),*sorted((ROOT/'mhs/policy').glob('*.js')),
           ROOT/'mhs/policy/dist/index.mjs',ROOT/'mhs/policy/instructions.md',ROOT/'mhs/policy/hospital-mhs.yaml',
           ROOT/'mhs/policy/package.json',ROOT/'mhs/dashboard.html',ROOT/'mhs/dashboard.js',ROOT/'mhs/dashboard-v2.js',ROOT/'mhs/workbench.js',ROOT/'mhs/dist/workbench.mjs',ROOT/'mhs/workbench-build.json',ROOT/'mhs/requirements.lock.txt',
           ROOT/'mhs/build-manifest.json',ROOT/'mhs/runtime-provenance.json',ROOT/'configs/stations.json',
           ROOT/'maps/carter_hospital_navigation.yaml',ROOT/'maps/carter_hospital_navigation.png',ROOT/'configs/nav2.yaml',ROOT/'configs/fastdds.xml',ROOT/'configs/hospital-v2.json',ROOT/'assets/people-v2/manifest.json',
           HOME/'profiles/local-dev/agnes-lock.json',*sorted(RUNTIME.glob('*.mjs'))]
    hashes={str(p.relative_to(ROOT)):hashlib.sha256(p.read_bytes()).hexdigest() for p in paths if p.is_file()}
    source=ROOT/'agent/agh-source-mhs'
    c=container()
    image=json.loads(command(['docker','image','inspect','hospital-delivery:humble-v1']))[0]
    evidence={'wall_time':time.time(),'agh_commit':command(['git','rev-parse','HEAD'],source),
              'agh_source_changes':command(['git','diff','--name-only'],source),
              'node':command(['node','--version']),'python':command([str(ROOT/'agent/mhs-venv/bin/python'),'--version']),
              'python_freeze':command([str(ROOT/'agent/mhs-venv/bin/pip'),'freeze']),
              'legacy_status':json.loads(command(['python3',str(ROOT/'scripts/control.py'),'status'])),
              'runtime_sha256':hashes,'processes':{name:process(name) for name in ['sim','web','bridge','daemon','workbench']},
              'docker':{'image_id':image['Id'],'repo_digests':image.get('RepoDigests',[]),
                        'container_image_id':c['Image'] if c else None,'network':'host','privileged':c['HostConfig']['Privileged'] if c else None,
                        'pid_mode':c['HostConfig']['PidMode'] if c else None,'mounts':c['Mounts'] if c else []},
              'note':'Hashes captured for this cold-started project runtime. Public AGH and credential files excluded.'}
    if evidence['agh_commit']!='57d0c34b33527294042b7ab8a60d7c7c934e932e':raise RuntimeError('official_commit_mismatch')
    (directory/'report.json').write_text(json.dumps(evidence,ensure_ascii=False,indent=2))
    print(json.dumps({'directory':str(directory),'agh_commit':evidence['agh_commit'],'file_count':len(hashes),'image_id':image['Id']}))
if __name__=='__main__':main()
