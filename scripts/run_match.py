#!/usr/bin/env python3
"""Run independent red/blue controllers without training or overwriting inputs."""
from __future__ import annotations
from wargame_native_engine.training.candidate_limits import MAX_NEW_CANDIDATES, validate_new_candidate_limit
import argparse
import json
from pathlib import Path
from wargame_native_engine.data import ScenarioBundle
from wargame_native_engine.matches.controllers import (
    NoOpController, RandomController, RulesController, PPOController, PluginController, HybridController,
)
from wargame_native_engine.matches.runner import run_match


def parser():
    p = argparse.ArgumentParser(description=__doc__)
    source = p.add_mutually_exclusive_group(required=True)
    source.add_argument('--input', type=Path, help='source map ZIP; requires --scenario-input')
    source.add_argument('--scenario-bundle', type=Path)
    p.add_argument('--scenario-input', type=Path)
    p.add_argument('--map-runtime-data', type=Path)
    p.add_argument('--output', required=True, type=Path, help='new isolated directory, must not exist')
    p.add_argument('--seed', type=int, default=42)
    p.add_argument('--max-steps', type=int, default=1500)
    p.add_argument('--decision-ms', type=int, default=1000)
    p.add_argument('--candidate-limit', type=int, default=MAX_NEW_CANDIDATES)
    p.add_argument('--decision-timeout-seconds', type=float, default=30.)
    p.add_argument('--failure-policy', choices=('noop','abort'), default='noop')
    for side in ('red', 'blue'):
        p.add_argument('--'+side, choices=('rules','ppo','llm','llm-jev','random-legal','no-op'), default='rules')
        p.add_argument('--'+side+'-checkpoint', type=Path)
        p.add_argument('--'+side+'-device', default='cpu')
        p.add_argument('--'+side+'-plugin', help='explicit trusted local module:factory; hybrid planner')
        p.add_argument('--'+side+'-evaluator', help='hybrid JEV evaluator module:factory')
        p.add_argument('--'+side+'-config', type=Path, help='plugin JSON config (never copied to provenance)')
    return p


def _controller(args, side):
    kind = getattr(args, side)
    checkpoint = getattr(args, side+'_checkpoint')
    plugin = getattr(args, side+'_plugin')
    evaluator = getattr(args, side+'_evaluator')
    config_path = getattr(args, side+'_config')
    if kind == 'ppo':
        if plugin or evaluator or config_path:
            raise ValueError('ppo does not accept plugin/evaluator/config options')
        if checkpoint is None:
            raise ValueError('--'+side+' ppo requires --'+side+'-checkpoint')
        return PPOController(checkpoint, getattr(args, side+'_device'))
    if kind in ('llm', 'llm-jev'):
        if checkpoint or (kind == 'llm' and evaluator):
            raise ValueError('incompatible checkpoint/evaluator for selected plugin kind')
        config = json.loads(config_path.read_text(encoding='utf-8')) if config_path else {}
        if not isinstance(config, dict):
            raise ValueError('plugin config must be a JSON object')
        if kind == 'llm-jev':
            return HybridController(plugin, evaluator, config, args.decision_timeout_seconds)
        return PluginController(plugin, config, args.decision_timeout_seconds)
    if checkpoint or plugin or evaluator or config_path:
        raise ValueError('controller-specific options supplied for incompatible controller')
    return {'rules': RulesController, 'random-legal': RandomController, 'no-op': NoOpController}[kind]()


def main(argv=None):
    p = parser()
    args = p.parse_args(argv)
    validate_new_candidate_limit(args.candidate_limit)
    if args.output.exists():
        p.error('--output must not exist; maps/checkpoints/results are never overwritten')
    if args.input and not args.scenario_input:
        p.error('--input requires --scenario-input')
    if args.scenario_input and not args.input:
        p.error('--scenario-input requires --input')
    if args.input and args.map_runtime_data:
        p.error('--map-runtime-data is for --scenario-bundle only')
    # Fail unconfigured adapters before importing a map or creating output.
    red, blue = _controller(args, 'red'), _controller(args, 'blue')
    if args.input:
        from wargame_native_engine.data.scenario_workspace import prepare_source_game
        # Import into a sibling staging directory unique to this output. Never
        # reuse the user's map workspace or alter source ZIP/scenario/checkpoint.
        workspace = args.output.with_name(args.output.name+'-inputs')
        workspace.mkdir(parents=True, exist_ok=False)
        prepared = prepare_source_game(args.input, args.scenario_input, workspace)
        bundle = ScenarioBundle.from_path(prepared['scenario_path']).with_map_runtime_data(prepared['runtime']['path'])
    else:
        bundle = ScenarioBundle.from_path(args.scenario_bundle)
        if args.map_runtime_data:
            bundle = bundle.with_map_runtime_data(args.map_runtime_data)
    print(f'MATCH: red={args.red} blue={args.blue} seed={args.seed} max_steps={args.max_steps}', flush=True)
    def progress(outcome):
        if outcome['steps'] == 1 or outcome['steps'] % 100 == 0 or outcome['ended']:
            print(f"MATCH: decisions={outcome['steps']}/{args.max_steps} sim_time_ms={outcome['sim_time_ms']} ended={outcome['ended']}", flush=True)
    result = run_match(bundle, red, blue, args.output, seed=args.seed,
        max_steps=args.max_steps, decision_ms=args.decision_ms, candidate_limit=args.candidate_limit,
        failure_policy=args.failure_policy, decision_timeout_seconds=args.decision_timeout_seconds, progress=progress)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
