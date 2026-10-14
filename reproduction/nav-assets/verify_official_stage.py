"""Verify only the fixed official hospital scenario root (not its full closure)."""
import argparse
import hashlib
import json
import time
import urllib.parse
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--file', type=Path, help='Locally installed matching scenario root; omitted means probe the official URL')
    parser.add_argument('--output', required=True, type=Path)
    args = parser.parse_args()
    lock = json.loads((HERE/'scene-inputs.lock.json').read_text(encoding='utf-8'))['official_stage']
    expected = lock['root_file']
    if args.file:
        actual_file = args.file.resolve()
        if actual_file.stat().st_size != expected['bytes']:
            raise ValueError('installed_official_scenario_size_mismatch')
        data = actual_file.read_bytes()
        source = str(actual_file)
    else:
        with urllib.request.urlopen(expected['url'], timeout=45) as response:
            final = urllib.parse.urlsplit(response.geturl())
            if final.scheme != 'https' or final.netloc != urllib.parse.urlsplit(expected['url']).netloc:
                raise ValueError('official_scenario_probe_redirect_not_allowed')
            data = response.read(expected['bytes']+1)
        source = expected['url']
    actual_sha = hashlib.sha256(data).hexdigest()
    passed = len(data) == expected['bytes'] and actual_sha == expected['sha256']
    proof = {'status': 'passed' if passed else 'failed', 'wall_time': time.time(), 'source': source,
             'actual_bytes': len(data), 'actual_sha256': actual_sha, 'expected': expected,
             'scope': 'Official scenario root file only. Referenced robot, hospital, material and texture assets require Isaac asset resolution; no GPU/scene qualification.'}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open('x', encoding='utf-8') as handle:
        handle.write(json.dumps(proof, ensure_ascii=False, indent=2)+'\n')
    print(json.dumps(proof, ensure_ascii=False, indent=2))
    if not passed:
        raise SystemExit(1)


if __name__ == '__main__':
    main()
