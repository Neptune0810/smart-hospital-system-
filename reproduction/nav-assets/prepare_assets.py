"""Acquire the frozen official 194-file IRA motion library without Isaac imports.

Default is verification only. --fetch downloads missing objects from the exact
official URLs. Existing wrong files fail instead of being silently overwritten.
This tool does not start Isaac, a model, a robot, or any project service.
"""
import argparse
import concurrent.futures
import hashlib
import json
import os
import time
import urllib.parse
import urllib.request
import uuid
from pathlib import Path, PurePosixPath

HERE = Path(__file__).resolve().parent
ALLOWED_HOSTS = {'omniverse-content-production.s3-us-west-2.amazonaws.com',
                 'omniverse-content-staging.s3.us-west-2.amazonaws.com'}
MAX_FILE_BYTES = 256 * 1024 * 1024


def sha256(path):
    digest = hashlib.sha256()
    with path.open('rb') as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b''):
            digest.update(chunk)
    return digest.hexdigest()


def validate_lock(lock):
    rows = lock.get('files', [])
    if len(rows) != 194 or not lock.get('complete') or lock.get('errors'):
        raise ValueError('frozen_asset_lock_incomplete')
    urls, paths = set(), set()
    for row in rows:
        url = urllib.parse.urlsplit(row['url'])
        relative = PurePosixPath(row['local'])
        expected = PurePosixPath('assets/people-v2') / url.netloc / urllib.parse.unquote(url.path).lstrip('/')
        if (url.scheme != 'https' or url.netloc not in ALLOWED_HOSTS
                or not url.path.startswith('/Assets/Isaac/6.0/') or url.query or url.fragment
                or relative.is_absolute() or '..' in relative.parts or relative != expected):
            raise ValueError('unsafe_frozen_asset_reference')
        if row['url'] in urls or row['local'] in paths:
            raise ValueError('duplicate_frozen_asset_reference')
        if not 0 < row['bytes'] <= MAX_FILE_BYTES or len(row['sha256']) != 64:
            raise ValueError('invalid_frozen_asset_digest')
        urls.add(row['url']); paths.add(row['local'])
    return rows


def acquire(root, row, fetch):
    cache = (root/'assets/people-v2').resolve()
    if root not in cache.parents:
        raise ValueError('asset_cache_symlink_outside_project')
    target = (root/row['local']).resolve()
    if cache not in target.parents:
        raise ValueError('asset_cache_path_escape')
    if target.exists():
        if not target.is_file() or target.stat().st_size != row['bytes'] or sha256(target) != row['sha256']:
            raise ValueError('existing_asset_does_not_match_lock:'+row['local'])
        return {'local': row['local'], 'status': 'verified', 'sha256': row['sha256'], 'bytes': row['bytes']}
    if not fetch:
        raise FileNotFoundError('asset_missing:'+row['local'])
    target.parent.mkdir(parents=True, exist_ok=True)
    if cache not in target.resolve().parents:
        raise ValueError('asset_cache_path_escape')
    temporary = target.with_name(target.name+'.download-'+uuid.uuid4().hex)
    count = 0
    digest = hashlib.sha256()
    try:
        request = urllib.request.Request(row['url'], headers={'User-Agent': 'SmartHospitalReproduction/1'})
        with urllib.request.urlopen(request, timeout=60) as response, temporary.open('xb') as output:
            final_url = urllib.parse.urlsplit(response.geturl())
            if final_url.scheme != 'https' or final_url.netloc not in ALLOWED_HOSTS:
                raise ValueError('asset_redirect_origin_not_allowed')
            for chunk in iter(lambda: response.read(1024*1024), b''):
                count += len(chunk)
                if count > row['bytes']:
                    raise ValueError('asset_larger_than_frozen_lock')
                output.write(chunk); digest.update(chunk)
        if count != row['bytes'] or digest.hexdigest() != row['sha256']:
            raise ValueError('downloaded_asset_digest_mismatch:'+row['local'])
        # The unique temporary path belongs to this command only. Never replace
        # an independently created target or a corrupt existing cache entry.
        with target.open('xb') as output, temporary.open('rb') as source:
            for chunk in iter(lambda: source.read(1024*1024), b''):
                output.write(chunk)
        return {'local': row['local'], 'status': 'downloaded_and_verified', 'sha256': row['sha256'], 'bytes': count}
    finally:
        temporary.unlink(missing_ok=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', required=True, type=Path)
    parser.add_argument('--fetch', action='store_true')
    parser.add_argument('--workers', type=int, default=4)
    parser.add_argument('--output', required=True, type=Path)
    parser.add_argument('--limit', type=int, help='Only for acquisition probes; this never produces a complete cache manifest')
    args = parser.parse_args()
    if not 1 <= args.workers <= 8:
        raise ValueError('workers_must_be_1_to_8')
    root = args.root.resolve()
    if not (root/'mhs').is_dir() or not (root/'maps').is_dir():
        raise ValueError('root_must_be_project_checkout')
    cache_directory = (root/'assets/people-v2').resolve()
    if root not in cache_directory.parents:
        raise ValueError('asset_cache_symlink_outside_project')
    lock_path = HERE/'people-assets.lock.json'
    lock = json.loads(lock_path.read_text(encoding='utf-8'))
    rows = validate_lock(lock)
    if args.limit is not None:
        if not 1 <= args.limit <= 194:
            raise ValueError('invalid_probe_limit')
        rows = rows[:args.limit]
    output = args.output.resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    if output.exists():
        raise FileExistsError('proof_output_already_exists')
    results, errors = [], []
    cache_manifest = root/'assets/people-v2/manifest.json'
    if cache_manifest.exists():
        try:
            if cache_directory not in cache_manifest.resolve().parents:
                raise ValueError('asset_manifest_symlink_outside_cache')
            existing = json.loads(cache_manifest.read_text(encoding='utf-8'))
            validate_lock(existing)
            if sorted(existing['files'], key=lambda row: row['local']) != sorted(lock['files'], key=lambda row: row['local']):
                raise ValueError('existing_complete_manifest_does_not_match_frozen_lock')
        except Exception as error:
            errors.append({'local': 'assets/people-v2/manifest.json', 'error': str(error)})
    proof = {'started_at': time.time(), 'root': str(root), 'fetch': args.fetch,
             'frozen_lock_sha256': sha256(lock_path), 'expected_count': 194, 'checked_count': len(rows),
             'scope': 'Official motion-library asset acquisition and checksum; no scene or navigation acceptance'}
    with concurrent.futures.ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = {pool.submit(acquire, root, row, args.fetch): row for row in rows}
        for future in concurrent.futures.as_completed(futures):
            row = futures[future]
            try:
                results.append(future.result())
            except Exception as error:
                errors.append({'local': row['local'], 'error': str(error)})
    complete = len(rows) == 194 and not errors
    proof.update(status='passed' if complete else 'probe_passed' if not errors else 'failed',
                 ended_at=time.time(), files=sorted(results,key=lambda row:row['local']), errors=errors,
                 complete=complete, total_verified_bytes=sum(row['bytes'] for row in results))
    output.write_text(json.dumps(proof, ensure_ascii=False, indent=2)+'\n', encoding='utf-8')
    if complete:
        cache = root/'assets/people-v2'
        cache.mkdir(parents=True, exist_ok=True)
        manifest = {'roots': lock['roots'], 'files': lock['files'], 'errors': [], 'pending': 0,
                    'complete': True, 'wall_time': time.time(), 'frozen_lock_sha256': sha256(lock_path)}
        target = cache/'manifest.json'
        # Retain an already valid project manifest. It may contain an earlier
        # acquisition timestamp; validation above independently checked all data.
        if not target.exists():
            with target.open('x', encoding='utf-8') as handle:
                handle.write(json.dumps(manifest, indent=2)+'\n')
    print(json.dumps({key: value for key,value in proof.items() if key != 'files'}, ensure_ascii=False, indent=2))
    if errors:
        raise SystemExit(1)


if __name__ == '__main__':
    main()
