"""Deterministic shared-window matches; observer artifacts never enter actors."""
from __future__ import annotations
from wargame_native_engine.training.candidate_limits import MAX_NEW_CANDIDATES, validate_new_candidate_limit
from copy import deepcopy
import json
import math
from pathlib import Path
import time

from .controllers import ControllerContext, RulesController
from .runtime import MatchRuntime
from .decision_records import make_record, finish_record, history_item


def _write(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False)+'\n', encoding='utf-8')


def context_for(seat, seed):
    view = seat._controlled_view()
    return ControllerContext(seat.controlled_side, seed, view.step_index,
        deepcopy(dict(view.observation)), deepcopy(seat.current_decision_binding),
        deepcopy(dict(seat._manager.public_static_map(seat._require_session_id()))),
        deepcopy(dict(seat._manager.get_weapon_catalog(seat._require_session_id()))))


def run_match(bundle, red, blue, output, *, seed=42, max_steps=1500,
              decision_ms=1000, candidate_limit=MAX_NEW_CANDIDATES, failure_policy='noop',
              decision_timeout_seconds=30., progress=None):
    """Run once, never overwrite. Invalid/error/overdue commands become no-op or abort.

    Both seats receive the same engine-time budget and at most one command per
    window. Built-in inference is trusted synchronous code: its deadline is
    measured on return, not preemptive. Plugin RPC has a hard worker deadline.
    Wall latency does not advance engine time; this is not realtime benchmarking.
    """
    validate_new_candidate_limit(candidate_limit)
    if failure_policy not in {'noop', 'abort'}:
        raise ValueError('failure_policy must be noop or abort')
    if not math.isfinite(decision_timeout_seconds) or decision_timeout_seconds <= 0:
        raise ValueError('decision timeout must be finite and positive')
    output = Path(output)
    output.mkdir(parents=True, exist_ok=False)
    controllers = {'red': red, 'blue': blue}
    runtime = None
    report = {'schema': 'native-controller-match/1.0', 'seed': seed,
              'decision_ms': decision_ms, 'candidate_limit': candidate_limit,
              'max_steps': max_steps, 'failure_policy': failure_policy,
              'decision_timeout_seconds': decision_timeout_seconds,
              'clock_mode': 'synchronous-equal-one-command-windows',
              'artifact_visibility': 'observer-only; replay/analysis may contain both sides',
              'controllers': {s: c.provenance() for s, c in controllers.items()},
              'status': 'running', 'failures': [], 'cleanup_errors': []}
    for side, controller in controllers.items():
        metadata = getattr(controller, 'training_metadata', None)
        if metadata is not None:
            actual = {'side': side, 'opponent': controllers['blue' if side == 'red' else 'red'].kind,
                      'decision_ms': decision_ms, 'candidate_limit': candidate_limit,
                      'map': bundle.scenario.map.map_id, 'scenario': bundle.scenario.scenario_id}
            report['controllers'][side]['match_transfer'] = {
                key: {'training': metadata.get(key), 'match': value,
                      'changed': metadata.get(key) != value if metadata.get(key) is not None else None}
                for key, value in actual.items()}
    disabled = set()
    histories = {'red': [], 'blue': []}
    report['decision_data'] = {'schema': 'native-decision-sample/1.0',
        'path': str(output/'decisions.jsonl'), 'visibility': 'same-side input; post-window feedback separated'}
    try:
        runtime = MatchRuntime(bundle, seed=seed, decision_ms=decision_ms,
                               candidate_limit=candidate_limit, max_steps=max_steps)
        runtime.reset()
        for side, controller in controllers.items():
            try:
                controller.reset(context_for(runtime.seat(side), seed))
            except Exception as exc:
                report['failures'].append({'side': side, 'phase': 'reset', 'error_type': type(exc).__name__})
                if failure_policy == 'abort':
                    raise
                disabled.add(side)
        with (output/'decisions.jsonl').open('w', encoding='utf-8') as log:
            while not runtime.ended:
                selections, records, chosen_ids = {}, {}, {}
                completed_window = False
                try:
                    for side, controller in controllers.items():
                        seat = runtime.seat(side)
                        context = context_for(seat, seed)
                        started = time.monotonic()
                        action_id, status = None, 'disabled' if side in disabled else 'ok'
                        error = None
                        details = None
                        try:
                            if side not in disabled:
                                if isinstance(controller, RulesController):
                                    action_id = controller.select_seat(seat)
                                else:
                                    action_id = controller.decide(context)
                                if time.monotonic()-started > decision_timeout_seconds:
                                    raise TimeoutError('decision budget exceeded')
                                getter = getattr(controller, 'decision_details', None)
                                details = getter() if callable(getter) else None
                            # Rules teachers may create native MOVE candidates while
                            # planning. Capture that final pre-execution catalogue.
                            binding = seat.current_decision_binding
                            allowed = {v: i for i, (v, ok) in enumerate(zip(
                                binding.action_space.action_ids, binding.action_space.mask)) if ok}
                            if action_id is not None and (not isinstance(action_id, str) or action_id not in allowed):
                                raise ValueError('controller must return a current legal action ID or None')
                            selections[side] = None if action_id is None else allowed[action_id]
                        except Exception as exc:
                            error = exc
                            status = type(exc).__name__
                            report['failures'].append({'side': side, 'phase': 'decide',
                                                      'decision': runtime.steps, 'error_type': status})
                            action_id, selections[side] = None, None
                            if not isinstance(exc, ValueError):
                                disabled.add(side)
                        binding = seat.current_decision_binding
                        chosen_ids[side] = action_id
                        records[side] = make_record(context, binding, match_id=output.name,
                            controller=controller.kind, action_id=action_id, status=status,
                            elapsed=time.monotonic()-started, details=details, history=histories[side])
                        if error is not None and failure_policy == 'abort':
                            raise error
                    runtime.step(selections['red'], selections['blue'])
                    completed_window = True
                    for side, record in records.items():
                        view = runtime.seat(side)._controlled_view()
                        finish_record(record, results=runtime.last_execution.get(side, ()),
                            selected_action_id=chosen_ids[side], post_observation=dict(view.observation),
                            after_time_ms=view.sim_time_ms)
                        histories[side].append(history_item(record))
                        histories[side] = histories[side][-4:]
                finally:
                    for record in records.values():
                        if not completed_window:
                            record['execution'] = {'status': 'window_not_completed',
                                'note': 'no execution result asserted; inspect match failure'}
                        log.write(json.dumps(record, ensure_ascii=False, allow_nan=False)+'\n')
                    log.flush()
                if progress is not None:
                    progress(runtime.outcome())
        report.update(runtime.outcome())
        report['status'] = 'completed'
    except Exception as exc:
        report['status'] = 'failed'
        report['error_type'] = type(exc).__name__
        raise
    finally:
        if runtime is not None:
            try:
                publication = runtime.publish(output/'replay')
                report['replay'] = str(publication.replay_path)
                report['analysis'] = str(publication.analysis_path) if publication.analysis_path else None
                report['frames'] = publication.frame_count
            except Exception as exc:
                report['publication_error'] = type(exc).__name__
                report['status'] = 'failed'
            finally:
                try:
                    runtime.close()
                except Exception as exc:
                    report['cleanup_errors'].append({'side': 'host', 'error_type': type(exc).__name__})
                    report['status'] = 'failed'
        for side, controller in controllers.items():
            try:
                controller.close()
            except Exception as exc:
                report['cleanup_errors'].append({'side': side, 'error_type': type(exc).__name__})
        _write(output/'match.json', report)
    if report['status'] != 'completed':
        raise RuntimeError('match publication failed; see match.json')
    return report
