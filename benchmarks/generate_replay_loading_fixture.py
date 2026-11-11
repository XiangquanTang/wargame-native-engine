"""Build a clearly synthetic repeated-frame loading fixture, not an engine run.

The source must be a valid published replay with its analysis sidecar. Only its
first frame is repeated; indices and byte hashes are rebound. No native strict
replay manifest is claimed, and the resulting viewer correctly reports strict
evidence unavailable. Never use this fixture as agent evaluation or training.
"""
from __future__ import annotations
import argparse
import hashlib
import json
from pathlib import Path
from zipfile import ZipFile, ZIP_DEFLATED


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('source', type=Path)
    parser.add_argument('output', type=Path)
    parser.add_argument('--frames', type=int, default=2547)
    args = parser.parse_args()
    if args.frames < 1:
        parser.error('--frames must be positive')
    args.output.mkdir(parents=True, exist_ok=False)
    with ZipFile(args.source / 'replay.zip') as archive:
        frame = json.loads(archive.read('0.json'))
    with (args.source / 'frame_analysis.native.jsonl').open() as source:
        analysis = json.loads(source.readline())
    with ZipFile(args.output / 'replay.zip', 'w', ZIP_DEFLATED) as archive, (
        args.output / 'frame_analysis.native.jsonl'
    ).open('w') as sidecar:
        for index in range(args.frames):
            frame['time']['cur_step'] = index
            payload = json.dumps(frame, sort_keys=True, separators=(',', ':')).encode()
            archive.writestr(f'{index}.json', payload)
            analysis['provenance']['frame_index'] = index
            analysis['provenance']['frame_hash'] = hashlib.sha256(payload).hexdigest()
            analysis['events'] = []
            sidecar.write(json.dumps(analysis, sort_keys=True, separators=(',', ':')) + '\n')
    (args.output / 'SYNTHETIC.txt').write_text(
        'Repeated first-frame load fixture, not a simulated trajectory.\n'
        'No native strict-replay evidence, evaluation or training claims.\n'
    )


if __name__ == '__main__':
    main()
