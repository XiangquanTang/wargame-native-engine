#!/usr/bin/env python3
"""Portable release entrypoint; delegates to existing native match launchers."""
from __future__ import annotations
import argparse
from datetime import datetime, timezone
import os
from pathlib import Path
import sys
from check_assets import verify


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('workflow', choices=('rl-vs-rules', 'llm-jev-vs-rl'))
    parser.add_argument('--max-steps', type=int, default=20)
    parser.add_argument('--seed', type=int, default=42)
    args = parser.parse_args(argv)
    if args.max_steps <= 0:
        parser.error('--max-steps must be positive')
    root = Path(__file__).resolve().parents[1]
    failures = [row for row in verify(root, True) if row['status'] != 'ok']
    if failures:
        for row in failures:
            print(row['status'].upper() + ': ' + row['path'], file=sys.stderr)
        return 2
    sys.path[:0] = [str(root), str(root / 'src')]
    stamp = datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S-%f')
    output = root / 'local_data' / 'matches' / (args.workflow + '-' + stamp + '-' + str(os.getpid()))
    common = [
        '--input', str(root / 'release_assets/maps/221/map_221.zip'),
        '--scenario-input', str(root / 'release_assets/scenarios/221/2201010101.json'),
        '--red-checkpoint', str(root / 'release_assets/models/ppo/capture-v3-final.pt'),
        '--max-steps', str(args.max_steps), '--seed', str(args.seed), '--output', str(output)]
    if args.workflow == 'rl-vs-rules':
        from scripts.run_rl_rules_match import main as launch
    else:
        from scripts.run_llm_jev_match import main as launch
    return launch(common)


if __name__ == '__main__':
    raise SystemExit(main())
