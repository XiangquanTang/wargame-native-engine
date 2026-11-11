"""Local Qwen strategic intent + NanoJev legal-choice plugin.

Factory for PluginController, not the legacy two-factory HybridController.
Only detached seat observations are consumed. Model output never builds a command.
"""
from __future__ import annotations

from copy import deepcopy
from .decision_records import candidates, readable, action_description
import json
import math
from pathlib import Path
import time
import urllib.error
import urllib.parse
import urllib.request

SCHEMA = 'native-qwen-jev-controller/1.0'
PLAN_SCHEMA = 'native-strategy/1.0'
COORDINATE_LEGEND = 'Positions are [column,row]; objective x=column,y=row. Path hex IDs encode row*100+column. '
OWN_FIELDS = ('operator_id', 'operator_type', 'position', 'alive', 'health', 'max_health',
              'blood', 'max_blood', 'state', 'stopped', 'deployed', 'has_remaining_movement_task',
              'move_to_stop_remaining_ms', 'weapon_cooldown_remaining_ms', 'suppression_remaining_ms')
ENEMY_FIELDS = ('operator_id', 'operator_type', 'position', 'alive', 'motion_state')
OBJECTIVE_FIELDS = ('objective_id', 'x', 'y', 'radius', 'value', 'controlling_side')
SYSTEM_PROMPT = '''You are the strategic planner for a simplified turn-window wargame.
Use ONLY the supplied side-visible facts. Hidden enemies are unknown. No supply,
reinforcement, reconnaissance command or invented ability exists. Movement, fire,
capture and support actions are chosen separately from the engine's legal catalog.
Return exactly one JSON object, no markdown or commentary, with these exact keys:
{"schema":"native-strategy/1.0","stance":"capture","objective_priorities":[],"assignments":[]}
stance must be capture, hold, or attack. objective_priorities is a unique ordered
list of existing objective IDs. assignments is a list of at most 16 objects, each
exactly {"unit_ids":[own living unit IDs],"task":"capture|hold|attack","target_id":ID}.
Use actual task strings capture, hold, or attack. Capture/hold targets must be
existing objective IDs; attack targets must be currently visible enemy IDs.
Each unit may appear in at most one assignment. Empty assignments are allowed.
Keep the plan compact: prefer at most four grouped assignments.
Prefer meaningful capture and defense of valuable objectives; visible threats may
change priorities. Intent guides the tactical selector, not engine legality.'''


class StrategyJevError(RuntimeError):
    """Stable error code; provider text is never put in exception messages."""


def _json(value):
    return json.dumps(value, ensure_ascii=False, separators=(',', ':'), allow_nan=False)


def _reject_constant(value):
    raise ValueError('nonfinite JSON')


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('duplicate JSON key')
        result[key] = value
    return result


def _decode(value):
    return json.loads(value, parse_constant=_reject_constant, object_pairs_hook=_unique_object)


def _exact_ids(values, allowed, name, max_count=255):
    if not isinstance(values, list) or len(values) > max_count:
        raise StrategyJevError('invalid_' + name)
    if any(type(v) is not int or v not in allowed for v in values) or len(values) != len(set(values)):
        raise StrategyJevError('invalid_' + name)
    return values


def _validate_plan(plan, state):
    if not isinstance(plan, dict) or set(plan) != {'schema', 'stance', 'objective_priorities', 'assignments'}:
        raise StrategyJevError('invalid_plan_schema')
    if plan['schema'] != PLAN_SCHEMA or plan['stance'] not in ('capture', 'hold', 'attack'):
        raise StrategyJevError('invalid_plan_schema')
    own = {v['operator_id'] for v in state['own'] if v.get('alive') is True}
    enemy = {v['operator_id'] for v in state['visible_enemies'] if v.get('alive') is not False}
    objectives = {v['objective_id'] for v in state['objectives']}
    _exact_ids(plan['objective_priorities'], objectives, 'objective_priorities')
    assignments = plan['assignments']
    if not isinstance(assignments, list) or len(assignments) > 16:
        raise StrategyJevError('invalid_assignments')
    assigned = set()
    for assignment in assignments:
        if not isinstance(assignment, dict) or set(assignment) != {'unit_ids', 'task', 'target_id'}:
            raise StrategyJevError('invalid_assignment')
        units = _exact_ids(assignment['unit_ids'], own, 'assignment_units')
        if not units or assigned.intersection(units):
            raise StrategyJevError('invalid_assignment_units')
        assigned.update(units)
        task, target = assignment['task'], assignment['target_id']
        if task not in ('capture', 'hold', 'attack') or type(target) is not int:
            raise StrategyJevError('invalid_assignment_target')
        if target not in (enemy if task == 'attack' else objectives):
            raise StrategyJevError('invalid_assignment_target')
    return plan


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise StrategyJevError('redirect_rejected')


def _endpoint(value, path):
    if not isinstance(value, str):
        raise ValueError('endpoint must be text')
    parsed = urllib.parse.urlsplit(value)
    if (parsed.scheme != 'http' or parsed.hostname != '127.0.0.1' or parsed.username or parsed.password
            or parsed.path != path or parsed.query or parsed.fragment or not parsed.port):
        raise ValueError('endpoint must be explicit HTTP IPv4 loopback with expected API path')
    return value


class LocalStrategyJev:
    def __init__(self, config):
        allowed = {'qwen_url', 'jev_url', 'http_timeout_seconds', 'plan_interval_ms',
                   'minimum_event_interval_ms', 'max_plan_tokens', 'max_state_characters', 'log_path'}
        if not isinstance(config, dict) or set(config) - allowed:
            raise ValueError('unknown strategy/Jev config field')
        self.qwen_url = _endpoint(config.get('qwen_url', 'http://127.0.0.1:8766/api/plan'), '/api/plan')
        self.jev_url = _endpoint(config.get('jev_url', 'http://127.0.0.1:8765/api/evaluate'), '/api/evaluate')
        self.timeout = config.get('http_timeout_seconds', 60.)
        if type(self.timeout) not in (float, int) or not math.isfinite(self.timeout) or not 0 < self.timeout <= 600:
            raise ValueError('invalid HTTP timeout')
        self.interval = config.get('plan_interval_ms', 30000)
        self.min_event_interval = config.get('minimum_event_interval_ms', 5000)
        self.max_plan_tokens = config.get('max_plan_tokens', 512)
        self.max_state_characters = config.get('max_state_characters', 12000)
        for value, low, high in ((self.interval, 1, 3600000), (self.min_event_interval, 0, self.interval),
                                 (self.max_plan_tokens, 1, 1024), (self.max_state_characters, 100, 24000)):
            if type(value) is not int or not low <= value <= high:
                raise ValueError('invalid integer config')
        self.log_path = config.get('log_path')
        if self.log_path is not None and (not isinstance(self.log_path, str) or not self.log_path):
            raise ValueError('invalid log path')
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect())
        self.log = None
        self.side = None
        self.plan = None
        self.plan_version = 0
        self.last_plan_time = None
        self.last_time = -1
        self.last_window = -1
        self.event_key = None
        self._last_details = None

    def reset(self, context):
        if self.side is not None:
            raise StrategyJevError('plugin_is_single_use')
        if context.side not in ('red', 'blue'):
            raise StrategyJevError('invalid_side')
        self.side = context.side
        if self.log_path:
            self.log = Path(self.log_path).open('x', encoding='utf-8')
        self._write({'event': 'reset', 'schema': SCHEMA, 'side': self.side,
                     'plan_interval_ms': self.interval, 'candidate_limit': 255,
                     'qwen_plan_schema': PLAN_SCHEMA, 'external_call_policy': 'loopback-no-retry'})

    def _write(self, record):
        if self.log:
            self.log.write(_json(readable(record)) + '\n')
            self.log.flush()

    def _post(self, url, payload):
        data = _json(payload).encode('utf-8')
        if len(data) > 131072:
            raise StrategyJevError('request_too_large')
        request = urllib.request.Request(url, data=data, headers={'Content-Type': 'application/json'}, method='POST')
        try:
            with self.opener.open(request, timeout=self.timeout) as response:
                if response.status != 200:
                    raise StrategyJevError('provider_http_status')
                raw = response.read(262145)
                if len(raw) > 262144:
                    raise StrategyJevError('response_too_large')
                return _decode(raw.decode('utf-8'))
        except StrategyJevError:
            raise
        except (urllib.error.URLError, TimeoutError, OSError):
            raise StrategyJevError('provider_transport_error') from None
        except (ValueError, UnicodeError):
            raise StrategyJevError('provider_invalid_json') from None

    def _state(self, context):
        if context.side != self.side or context.observation.get('side') != self.side:
            raise StrategyJevError('seat_mismatch')
        obs = context.observation
        result = {'side': self.side, 'sim_time_ms': context.binding.action_space.sim_time_ms,
                  'stage': obs.get('stage'), 'own': [], 'visible_enemies': [], 'objectives': []}
        for source, destination, fields, identifier in (
                ('friendly_operators', 'own', OWN_FIELDS, 'operator_id'),
                ('visible_enemies', 'visible_enemies', ENEMY_FIELDS, 'operator_id'),
                ('objectives', 'objectives', OBJECTIVE_FIELDS, 'objective_id')):
            rows = obs.get(source)
            if not isinstance(rows, (list, tuple)):
                raise StrategyJevError('missing_visible_state')
            ids = set()
            for row in rows:
                if not isinstance(row, dict) or type(row.get(identifier)) is not int or row[identifier] in ids:
                    raise StrategyJevError('invalid_visible_state')
                if source == 'friendly_operators' and row.get('side') != self.side:
                    raise StrategyJevError('seat_mismatch')
                ids.add(row[identifier])
                result[destination].append({k: row[k] for k in fields if k in row})
        if len(_json(result)) > self.max_state_characters:
            raise StrategyJevError('state_budget_exceeded')
        return result

    def _refresh_plan(self, state):
        now = state['sim_time_ms']
        key = _json({'own_alive': sorted(v['operator_id'] for v in state['own'] if v.get('alive') is True),
                     'visible_enemies': sorted(v['operator_id'] for v in state['visible_enemies']),
                     'ownership': sorted((v['objective_id'], v.get('controlling_side')) for v in state['objectives'])})
        invalid = False
        if self.plan is not None:
            try:
                _validate_plan(self.plan, state)
            except StrategyJevError:
                invalid = True
        due = (self.plan is None or invalid or now - self.last_plan_time >= self.interval or
               (key != self.event_key and now - self.last_plan_time >= self.min_event_interval))
        if not due:
            return 0.
        started = time.monotonic()
        answer = self._post(self.qwen_url, {'messages': [
            {'role': 'system', 'content': SYSTEM_PROMPT + COORDINATE_LEGEND},
            {'role': 'user', 'content': _json({'state': state, 'previous_plan': self.plan})}],
            'max_new_tokens': self.max_plan_tokens})
        required = {'text', 'input_tokens', 'output_tokens', 'finish_reason', 'generation_seconds'}
        if not isinstance(answer, dict) or set(answer) != required or answer['finish_reason'] != 'stop':
            raise StrategyJevError('invalid_qwen_envelope_or_truncated_plan')
        if not isinstance(answer['text'], str):
            raise StrategyJevError('invalid_qwen_envelope')
        for k in ('input_tokens', 'output_tokens'):
            if type(answer[k]) is not int or answer[k] < 0:
                raise StrategyJevError('invalid_qwen_envelope')
        if type(answer['generation_seconds']) not in (int, float) or not math.isfinite(answer['generation_seconds']) or answer['generation_seconds'] < 0:
            raise StrategyJevError('invalid_qwen_envelope')
        try:
            plan = _decode(answer['text'])
        except (ValueError, TypeError):
            raise StrategyJevError('invalid_plan_json') from None
        self.plan = _validate_plan(plan, state)
        self.plan_version += 1
        self.last_plan_time, self.event_key = now, key
        elapsed = time.monotonic() - started
        self._write({'event': 'plan', 'sim_time_ms': now, 'version': self.plan_version,
                     'plan': self.plan, 'elapsed_seconds': elapsed, 'input_tokens': answer['input_tokens'],
                     'output_tokens': answer['output_tokens']})
        return elapsed

    def decision_details(self):
        return deepcopy(self._last_details)

    def decide(self, context):
        self._last_details = None
        space = context.binding.action_space
        if self.side is None or context.side != self.side or space.side.value != self.side:
            raise StrategyJevError('seat_mismatch')
        now, window = space.sim_time_ms, space.decision_window_index
        if now < self.last_time or window <= self.last_window:
            raise StrategyJevError('stale_or_repeated_snapshot')
        self.last_time, self.last_window = now, window
        ids = [v for v, ok in zip(space.action_ids, space.mask) if ok]
        if len(ids) > 255 or len(set(ids)) != len(ids):
            raise StrategyJevError('invalid_candidate_capacity')
        state = self._state(context)
        legal, local_mapping = candidates(context.binding)
        started = time.monotonic()
        try:
            if len(ids) <= 1:
                chosen = ids[0] if ids else None
                self._last_details = {'strategic_plan': deepcopy(self.plan), 'model_calls': 0,
                    'plan_version': self.plan_version, 'visible_state': state, 'prompt_schema': PLAN_SCHEMA}
                self._write({'event': 'decision', 'side': self.side, 'window': window, 'sim_time_ms': now,
                    'strategic_plan': self.plan, 'legal_candidates': legal,
                    'selection': legal[0] if legal else None, 'model_calls': 0})
                return chosen
            qwen_seconds = self._refresh_plan(state)
            mapping = {'c' + str(i): value for i, value in enumerate(ids)}
            action_rows = {v['action_id']: v['action'] for v in space.actions}
            if set(ids) - set(action_rows):
                raise StrategyJevError('candidate_record_mismatch')
            # Use the same semantic candidate payload shown in decision samples.
            # Native authorization fingerprints stay in the binding for execution.
            criteria = {item['id']: _json(item['action']) for item in legal}
            snapshot_id = self.side + '-window-' + str(window)
            jev_started = time.monotonic()
            answer = self._post(self.jev_url, {'states': [{'id': snapshot_id,
                'state': _json({'visible_state': state, 'strategic_intent': self.plan}),
                'questions': {'action': {'type': 'choice',
                    'instructions': COORDINATE_LEGEND + 'Choose exactly one currently legal action that best advances the strategic intent. '
                    'Candidate coordinates use engine parameters. Do not invent commands. Avoid pointless repeated waiting.',
                    'criteria': criteria}}}]})
            chosen_key = self._validate_jev(answer, snapshot_id, mapping)
            chosen = mapping[chosen_key]
            probabilities = answer['states'][0]['answers']['action']['probabilities']
            self._last_details = {'strategic_plan': deepcopy(self.plan),
                'plan_version': self.plan_version, 'visible_state': state, 'prompt_schema': PLAN_SCHEMA,
                'probabilities': deepcopy(probabilities), 'qwen_seconds': qwen_seconds,
                'jev_seconds': time.monotonic() - jev_started, 'model_calls': 1 + int(qwen_seconds > 0)}
            self._write({'event': 'decision', 'side': self.side, 'window': window, 'sim_time_ms': now,
                'strategic_plan': self.plan, 'visible_state': state,
                'legal_candidates': legal, 'selection': next(v for v in legal if v['id'] == chosen_key),
                'probabilities': probabilities, 'qwen_seconds': qwen_seconds,
                'jev_seconds': self._last_details['jev_seconds'], 'elapsed_seconds': time.monotonic() - started})
            return chosen
        except Exception as exc:
            self._write({'event': 'failure', 'side': self.side, 'window': window, 'sim_time_ms': now,
                         'error': str(exc) if isinstance(exc, StrategyJevError) else type(exc).__name__})
            raise

    @staticmethod
    def _validate_jev(answer, snapshot_id, mapping):
        if not isinstance(answer, dict) or answer.get('schema_version') != 'openjev-toy-inference-v1':
            raise StrategyJevError('invalid_jev_schema')
        states = answer.get('states')
        if not isinstance(states, list) or len(states) != 1 or not isinstance(states[0], dict) or states[0].get('id') != snapshot_id:
            raise StrategyJevError('jev_snapshot_mismatch')
        answers = states[0].get('answers')
        if not isinstance(answers, dict) or set(answers) != {'action'}:
            raise StrategyJevError('invalid_jev_answers')
        result = answers['action']
        if not isinstance(result, dict) or result.get('type') != 'choice':
            raise StrategyJevError('invalid_jev_choice')
        choice, probabilities = result.get('choice'), result.get('probabilities')
        if not isinstance(choice, str) or choice not in mapping or result.get('value') != choice:
            raise StrategyJevError('invalid_jev_choice')
        if not isinstance(probabilities, dict) or set(probabilities) != set(mapping):
            raise StrategyJevError('invalid_jev_probabilities')
        if any(type(v) not in (int, float) or not math.isfinite(v) or not 0 <= v <= 1 for v in probabilities.values()):
            raise StrategyJevError('invalid_jev_probabilities')
        if abs(sum(probabilities.values()) - 1) > 1e-4 or probabilities[choice] < max(probabilities.values()) - 1e-7:
            raise StrategyJevError('invalid_jev_probabilities')
        return choice

    def close(self):
        if self.log:
            self.log.close()
            self.log = None


def create(config):
    return LocalStrategyJev(config)
