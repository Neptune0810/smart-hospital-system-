#!/usr/bin/env python3
"""Export a small allowlisted delivery bundle; never archive AGH home or secrets."""
import json,tarfile,hashlib,re,io
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
def main():
    files=[]
    for name in ['scripts','service','sim','launch','maps','configs','agent/hospital-policy']:
        files.extend(f for f in (ROOT/name).rglob('*') if f.is_file() and '__pycache__' not in f.parts and f.suffix!='.pyc')
    files.extend(ROOT/n for n in ['README.md','DELIVERY.md','Dockerfile','.gitignore','.dockerignore','agent/profile.template.json','agent/instructions.md'])
    files.extend(f for f in (ROOT/'evidence').iterdir() if f.is_file() and f.suffix in ['.json','.txt','.tsv'])
    for name in ['agent-mcp-authorized-v7','agent-mcp-readonly','once-plan-smoke']:
        directory=ROOT/'evidence'/name
        if directory.exists():files.extend(f for f in directory.rglob('*') if f.is_file() and f.suffix in ['.json','.jsonl','.txt','.log'])
    for name in ['experiment-v6','experiment-v5','experiment-v4','experiment-v3-preflight','acceptance-agent','experiment-v1','experiment-v2']:
        directory=ROOT/'runs'/name
        if directory.exists():
            files.extend(f for f in directory.rglob('*') if f.is_file() and (f.suffix in ['.json','.jsonl','.csv','.md','.mp4','.gz'] or f.name.startswith('turn-') and f.suffix=='.log') and 'frames' not in f.parts)
    proof=ROOT/'evidence/continuous-agent.json'
    if proof.exists():
        run_id=json.loads(proof.read_text())['run_id']
        if not re.fullmatch(r'[0-9a-f]{32}',run_id):raise RuntimeError('Invalid proof run id')
        files.extend(f for f in (ROOT/'runs'/run_id).rglob('*') if f.is_file() and f.suffix in ['.json','.jsonl','.log'])
    files.extend(f for pattern in ['qualification-*.log','agent-normal*.log','acceptance.log'] for f in (ROOT/'logs').glob(pattern) if f.is_file())
    files=sorted(set(f for f in files if f.exists()))
    sensitive=re.compile(rb'(?i)(?:sk-[A-Za-z0-9_-]{20,}|Bearer\s+[A-Za-z0-9._-]{20,}|-----BEGIN (?:RSA |OPENSSH |EC )?PRIVATE KEY-----)')
    hits=[str(f.relative_to(ROOT)) for f in files if f.suffix!='.mp4' and sensitive.search(f.read_bytes())]
    if hits:raise RuntimeError('Potential credential content; inspect without printing values: '+str(hits))
    output=ROOT/'hospital-delivery-agent-delivery.tar.gz'
    manifest={};temporary=output.with_suffix('.tmp')
    with tarfile.open(temporary,'w:gz') as archive:
        for f in files:
            if f.is_symlink():raise RuntimeError('Refuse symlink export: '+str(f))
            payload=f.read_bytes()
            if f.suffix!='.mp4' and sensitive.search(payload):raise RuntimeError('Potential credential content: '+str(f.relative_to(ROOT)))
            manifest[str(f.relative_to(ROOT))]=hashlib.sha256(payload).hexdigest()
            info=archive.gettarinfo(str(f),arcname=str(f.relative_to(ROOT)));info.size=len(payload)
            archive.addfile(info,io.BytesIO(payload))
        payload=json.dumps(manifest,indent=2).encode();info=tarfile.TarInfo('delivery-manifest.json');info.size=len(payload);archive.addfile(info,io.BytesIO(payload))
        info=tarfile.TarInfo('vendor/apt-cache/.gitkeep');info.size=0;archive.addfile(info,io.BytesIO())
    temporary.replace(output)
    print(json.dumps({'path':str(output),'bytes':output.stat().st_size,'sha256':hashlib.sha256(output.read_bytes()).hexdigest(),'files':len(files),'credential_pattern_hits':0}))
if __name__=='__main__':main()
