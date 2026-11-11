"""Deterministic public controller reads, separate from tick or HTTP latency."""

from __future__ import annotations

import argparse
import cProfile
import hashlib
import json
from pathlib import Path
import tempfile

from benchmark_runtime import samples, source_identity
from wargame_native_engine.io.serialization import canonical_hash
from wargame_native_engine.operations import ProductOperationService, ScenarioMatchFactory


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--scenario', type=Path, default=Path('examples/map123/scenario.json'))
    parser.add_argument('--map-runtime-data', type=Path)
    parser.add_argument('--repeats', type=int, default=30)
    parser.add_argument('--profile', type=Path)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    if args.repeats < 1:
        parser.error('repeats must be positive')
    result = {**source_identity(), 'seed': 42,
              'scenario_sha256': hashlib.sha256(args.scenario.read_bytes()).hexdigest(),
              'runner_sha256': hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
              'workload': 'fixed initial state: public observe, state, unit menu, capabilities; no tick or HTTP'}
    with tempfile.TemporaryDirectory(prefix='public-read-bench-') as storage:
        match = ScenarioMatchFactory.from_path(
            args.scenario, map_runtime_data=args.map_runtime_data,
            storage_root=storage, red_controller_id='red', blue_controller_id='blue',
            decision_interval_ms=1000, frame_interval_ms=1000,
        )(42, 1)
        controller = match.bind_controller('red', controller_id='red')
        service = ProductOperationService(controller)
        try:
            view = controller.observe()
            actors = tuple(item['operator_id'] for item in view.observation['friendly_operators'])[:3]
            calls = {
                'observe': controller.observe,
                'state': controller.state,
                'unit_menu': lambda: controller.get_unit_commands(actors[0]),
                'capabilities': lambda: service.capabilities(actors),
            }
            result['actors'] = actors
            result['reads'] = {}
            for name, call in calls.items():
                expected = canonical_hash(call())  # warmup, outside timing
                profiler = cProfile.Profile() if args.profile else None
                if profiler:
                    profiler.enable()
                measured = samples(call, args.repeats)
                if profiler:
                    profiler.disable()
                    profiler.dump_stats(str(args.profile) + '.' + name + '.prof')
                actual = canonical_hash(call())
                if actual != expected:
                    raise AssertionError(f'{name} changed its returned state')
                result['reads'][name] = {**measured, 'result_hash': actual}
        finally:
            service.close()
            match.close()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2) + '\n')


if __name__ == '__main__':
    main()
