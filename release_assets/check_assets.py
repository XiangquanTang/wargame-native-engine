#!/usr/bin/env python3
"""Verify release inputs without loading model weights or serialized map objects."""
from __future__ import annotations
import argparse
import hashlib
import json
from pathlib import Path
import zipfile


def verify(root, required_only=False):
    root = Path(root).resolve()
    manifest = json.loads((root / 'release_assets/ASSETS.json').read_text(encoding='utf-8'))
    results = []
    for asset in manifest['assets']:
        if required_only and not asset['required_for_221_workflows']:
            continue
        path = root / asset['path']
        status = 'ok'
        if not path.is_file():
            status = 'missing'
        elif asset['sha256'] and hashlib.sha256(path.read_bytes()).hexdigest() != asset['sha256']:
            status = 'hash_mismatch'
        elif asset['kind'] == 'source_scenario':
            try:
                if not isinstance(json.loads(path.read_text(encoding='utf-8')), dict):
                    status = 'scenario_must_be_object'
            except (ValueError, UnicodeError):
                status = 'invalid_scenario_json'
        elif asset['kind'] == 'map_archive':
            try:
                with zipfile.ZipFile(path) as archive:
                    if archive.testzip() is not None:
                        status = 'invalid_archive'
            except (OSError, zipfile.BadZipFile):
                status = 'invalid_archive'
        results.append({'path': asset['path'], 'status': status})
    return results


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--required-only', action='store_true')
    args = parser.parse_args(argv)
    results = verify(Path(__file__).resolve().parents[1], args.required_only)
    for row in results:
        print(row['status'].upper() + ': ' + row['path'])
    return 0 if all(row['status'] == 'ok' for row in results) else 1


if __name__ == '__main__':
    raise SystemExit(main())
