"""Measure validated replay startup and public projection identity.

Run this same file against baseline/current PYTHONPATH values. This does not
skip archive validation or claim a synthetic replay is a real game trajectory.
"""
from __future__ import annotations
import argparse
import gzip
import hashlib
import json
from pathlib import Path
import resource
from time import perf_counter

import wargame_native_engine
from wargame_native_engine.version import ENGINE_VERSION
from wargame_native_engine.viewer.replay_view import load_replay_view


def encoded(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
        separators=(',', ':'), allow_nan=False).encode()


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--replay-dir', type=Path, required=True)
    parser.add_argument('--native-scenario-map', type=Path)
    parser.add_argument('--repeat', type=int, default=1)
    args=parser.parse_args()
    if args.repeat < 1:parser.error('--repeat must be positive')
    elapsed=[]
    for _ in range(args.repeat):
        started=perf_counter()
        view=load_replay_view(args.replay_dir, native_scenario_map=args.native_scenario_map)
        elapsed.append(perf_counter()-started)
    records={'replay': view.replay_record(), 'timeline':view.timeline_record(), 'map':view.map_record()}
    for index in sorted({0, view.frame_count//2, view.frame_count-1}):
        records[f'frame-{index}']=view.frame_record(index)
        records[f'analysis-{index}']=view.analysis_record(index)
    source_root = Path(wargame_native_engine.__file__).resolve().parent
    source_digest = hashlib.sha256()
    for file in sorted(source_root.rglob('*.py')):
        source_digest.update(file.relative_to(source_root).as_posix().encode() + b'\0')
        source_digest.update(file.read_bytes() + b'\0')
    print(json.dumps({
        'engine_version': ENGINE_VERSION,
        'python_source_sha256': source_digest.hexdigest(),
        'frame_count' :view.frame_count,
        'load_seconds':elapsed,
        'peak_rss_kib_linux':resource.getrusage(resource.RUSAGE_SELF).ru_maxrss,
        'records': {name:{'sha256':hashlib.sha256(encoded(value)).hexdigest(),
            'raw_bytes':len(encoded(value)),
            'gzip_bytes':len(gzip.compress(encoded(value),compresslevel=3,mtime=0))}
            for name,value in records.items()},
    },indent=2))


if __name__=='__main__':main()
