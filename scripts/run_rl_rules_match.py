#!/usr/bin/env python3
"""Run the existing native host with frozen PPO red and rules blue."""
from __future__ import annotations
import argparse
from datetime import datetime, timezone
import os
from pathlib import Path


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--max-steps', type=int, default=20)
    parser.add_argument('--seed', type=int, default=42)
    parser.add_argument('--input', type=Path, default=Path('local_data/maps/inbox/map_221.zip'))
    parser.add_argument('--scenario-input', type=Path, default=Path('local_data/maps/inbox/2201010101.json'))
    parser.add_argument('--red-checkpoint', type=Path, default=Path('local_data/rl/capture-v3-221-20261101-152137-42733/final.pt'))
    parser.add_argument('--red-device', default='cpu')
    parser.add_argument('--output', type=Path)
    args = parser.parse_args(argv)
    if args.max_steps <= 0:
        parser.error('--max-steps must be positive')
    for name in ('input', 'scenario_input', 'red_checkpoint'):
        if not getattr(args, name).is_file():
            parser.error(name + ' file does not exist')
    stamp = datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S-%f')
    output = args.output or Path('local_data/matches') / ('ppo-vs-rules-' + stamp + '-' + str(os.getpid()))
    if output.exists():
        parser.error('--output must not exist')
    print('MATCH OUTPUT:', output, flush=True)
    from scripts.run_match import main as run_match_main
    return run_match_main([
        '--input', str(args.input), '--scenario-input', str(args.scenario_input),
        '--red', 'ppo', '--red-checkpoint', str(args.red_checkpoint), '--red-device', args.red_device,
        '--blue', 'rules', '--candidate-limit', '255', '--failure-policy', 'abort',
        '--max-steps', str(args.max_steps), '--seed', str(args.seed), '--output', str(output)])


if __name__ == '__main__':
    raise SystemExit(main())
