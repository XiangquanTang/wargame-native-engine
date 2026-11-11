#!/usr/bin/env python3
"""Run the existing native match host with a local strategic/Jev plugin."""
from __future__ import annotations
import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--max-steps', type=int, default=20)
    p.add_argument('--seed', type=int, default=42)
    p.add_argument('--input', type=Path, default=Path('local_data/maps/inbox/map_221.zip'))
    p.add_argument('--scenario-input', type=Path, default=Path('local_data/maps/inbox/2201010101.json'))
    p.add_argument('--red-checkpoint', type=Path, default=Path('local_data/rl/capture-v3-221-20261101-152137-42733/final.pt'))
    p.add_argument('--output', type=Path)
    p.add_argument('--qwen-url', default='http://127.0.0.1:8766/api/plan')
    p.add_argument('--jev-url', default='http://127.0.0.1:8765/api/evaluate')
    p.add_argument('--http-timeout-seconds', type=float, default=60.)
    p.add_argument('--plan-interval-ms', type=int, default=30000)
    args = p.parse_args(argv)
    if args.max_steps <= 0:
        p.error('--max-steps must be positive')
    for name in ('input', 'scenario_input', 'red_checkpoint'):
        if not getattr(args, name).is_file():
            p.error(name + ' file does not exist')
    stamp = datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S-%f')
    output = args.output or Path('local_data/matches') / ('ppo-vs-llm-jev-' + stamp + '-' + str(os.getpid()))
    if output.exists():
        p.error('--output must not exist')
    config_path = output.with_name(output.name + '-controller.json')
    log_path = output.with_name(output.name + '-controller.jsonl')
    config = {'qwen_url': args.qwen_url, 'jev_url': args.jev_url,
              'http_timeout_seconds': args.http_timeout_seconds,
              'plan_interval_ms': args.plan_interval_ms, 'log_path': str(log_path.resolve())}
    from wargame_native_engine.matches.strategy_jev import LocalStrategyJev
    LocalStrategyJev(config)  # validate before creating any files or loading PPO
    if config_path.exists() or log_path.exists():
        p.error('controller artifacts already exist')
    config_path.parent.mkdir(parents=True, exist_ok=True)
    with config_path.open('x', encoding='utf-8') as stream:
        json.dump(config, stream, ensure_ascii=False, indent=2)
        stream.write('\n')
    # Each turn can make two sequential HTTP calls. Outer worker deadline also
    # covers serialization/validation. A timeout never cancels the model server.
    deadline = 2 * args.http_timeout_seconds + 30
    print('MATCH OUTPUT:', output, flush=True)
    print('CONTROLLER LOG:', log_path, flush=True)
    from scripts.run_match import main as run_match_main
    return run_match_main([
        '--input', str(args.input), '--scenario-input', str(args.scenario_input),
        '--red', 'ppo', '--red-checkpoint', str(args.red_checkpoint), '--red-device', 'cpu',
        '--blue', 'llm', '--blue-plugin', 'wargame_native_engine.matches.strategy_jev:create',
        '--blue-config', str(config_path), '--candidate-limit', '255', '--failure-policy', 'abort',
        '--decision-timeout-seconds', str(deadline), '--max-steps', str(args.max_steps),
        '--seed', str(args.seed), '--output', str(output)])


if __name__ == '__main__':
    raise SystemExit(main())
