"""Fail closed before starting the character animation runtime."""
import hashlib,json

def verify_people_assets(root):
    cache=root/'assets/people-v2'
    manifest=json.loads((cache/'manifest.json').read_text())
    rows=manifest.get('files',[])
    if not manifest.get('complete') or manifest.get('errors') or len(rows)<194:
        raise RuntimeError('people_asset_cache_incomplete_run_mhs_cache_people_assets')
    for row in rows:
        path=(root/row['local']).resolve()
        if cache.resolve() not in path.parents or not path.is_file():
            raise RuntimeError('people_asset_cache_missing_or_outside_project')
        if path.stat().st_size!=row['bytes'] or hashlib.sha256(path.read_bytes()).hexdigest()!=row['sha256']:
            raise RuntimeError('people_asset_cache_integrity_failed:'+row['local'])
    return {'files':len(rows),'bytes':sum(row['bytes'] for row in rows)}
