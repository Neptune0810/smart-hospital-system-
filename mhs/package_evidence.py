"""Allowlisted project evidence, excludes account configuration and credentials."""
import hashlib,json,time,zipfile
from .manage import ROOT

def main():
    base=ROOT/'runs/mhs-v2'; target=base/('evidence-'+time.strftime('%Y%m%d-%H%M%S')+'.zip')
    files=[]
    for collection in ['acceptance','qualification','diagnostics','demos']:
        if not (base/collection).exists():continue
        for directory in (base/collection).iterdir():
            if directory.is_file():
                if directory.suffix in {'.json','.jsonl','.log','.jpg'}:files.append(directory)
                continue
            files.extend(p for p in directory.rglob('*') if p.is_file() and p.suffix in {'.json','.jsonl','.log','.jpg','.html'})
    files.extend(base.glob('summary-*.json'))
    for directory in (base/'protocol').iterdir():
        if directory.is_dir():files.extend(p for p in directory.iterdir() if p.name in {'registration.json','messages.jsonl','image-index.jsonl','wire.jsonl'})
    for name in ['mcp-calls.jsonl','policy-denials.jsonl','session-bindings.json']:
        if (base/name).is_file():files.append(base/name)
    files.extend(p for p in (ROOT/'mhs').iterdir() if p.name in {'README.md','requirements.lock.txt','build-manifest.json','runtime-provenance.json'})
    files.extend(p for p in (ROOT/'mhs').rglob('*') if p.is_file() and '__pycache__' not in p.parts and (p.suffix in {'.py','.js','.sh','.md','.yaml','.mjs','.html'} or p.name=='package.json'))
    files.extend(p for p in (ROOT/'logs/mhs').glob('preview-*.json') if p.is_file())
    files.extend(ROOT/name for name in ['configs/nav2.yaml','configs/stations.json','configs/hospital-v2.json','configs/navigation-qualification-v2.json','configs/fastdds.xml','maps/carter_hospital_navigation.yaml','maps/carter_hospital_navigation.png','mhs/camera.json'] if (ROOT/name).is_file())
    for name in ['Connect-HospitalMHS.ps1','Manage-HospitalMHS.ps1','院递-使用指南.md','院递V2-交付报告.md','院递V2-会话与演示索引.md','build_v2_delivery_report.py','build_v2_replay.py']:
        if (ROOT/name).is_file():files.append(ROOT/name)
    for name in ['Dockerfile','launch/navigation.launch.py','sim/hospital.py','scripts/manage.py','scripts/control.py','scripts/nav_bootstrap.py','scripts/nav_bootstrap_start.sh']:
        if (ROOT/name).is_file():files.append(ROOT/name)
    files=list(dict.fromkeys(files));manifest=[]
    if any(file.is_symlink() or ROOT not in file.resolve().parents for file in files):raise RuntimeError('evidence_symlink_or_outside_project_refused')
    with zipfile.ZipFile(target,'x',compression=zipfile.ZIP_DEFLATED) as archive:
        for file in sorted(files):
            raw=file.read_bytes(); relative=str(file.relative_to(ROOT))
            archive.writestr(relative,raw)
            manifest.append({'path':relative,'bytes':len(raw),'sha256':hashlib.sha256(raw).hexdigest()})
        archive.writestr('evidence-manifest.json',json.dumps(manifest,indent=2))
        archive.writestr('archive-scope.json',json.dumps({'videos':'Separate simulation.mp4 files; not bundled','binary_frames':'Full recorded .bin frames retained on server under runs/mhs-v2/protocol; model-selected original JPEGs are included','credentials':'Account configuration and credentials excluded','old_attempts':'Original failures and missing items retained; no reruns overwrite them'},indent=2))
    print(json.dumps({'archive':str(target),'files':len(files),'bytes':target.stat().st_size,'sha256':hashlib.sha256(target.read_bytes()).hexdigest()}))
if __name__=='__main__':main()
