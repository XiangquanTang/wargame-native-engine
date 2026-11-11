"""Self-contained, side-visible decision samples. Hashes stay in engine plumbing.

These are decision/learning records, not privileged world snapshots capable of
restarting the simulator. All future feedback is separated from policy_input.
"""
from __future__ import annotations
from copy import deepcopy
import json

SCHEMA = 'native-decision-sample/1.0'
_NAMES = {'MOVE': '移动', 'STOP_MOVE': '停止移动', 'CAPTURE': '夺控', 'WAIT': '等待',
          'SHOOT': '射击', 'ATTACK': '攻击', 'FIRE': '开火', 'BOARD': '登车', 'UNLOAD': '下车',
          'DEPLOY': '部署', 'END_DEPLOYMENT': '结束部署', 'CHANGE_STATE': '改变状态',
          'WEAPON_LOCK': '武器锁定', 'WEAPON_UNLOCK': '武器解锁', 'WEAPON_UNFOLD': '武器展开',
          'ENTER_CONCEALMENT': '隐蔽'}


def readable(value):
    """Drop technical integrity/link fields, never invent missing game facts."""
    if isinstance(value, dict):
        return {str(k): readable(v) for k, v in value.items()
                if 'hash' not in str(k).lower() and str(k) not in
                {'action_id', 'action_ids', 'legal_action_ids', 'catalog_id', 'session_id', 'binding_scope', 'binding_scope_id'}}
    if isinstance(value, (list, tuple)):
        return [readable(v) for v in value]
    return deepcopy(value)


# These native authorization fingerprints identify rule documents, not tactical
# choices. Keep them in the engine binding/replay; never use them as model input.
_ACTION_AUTHORITY_KEYS = {
    'BOARD': frozenset({'authority_hash'}),
    'UNLOAD': frozenset({'authority_hash'}),
    'FIRE': frozenset({'indirect_fire_authority_hash', 'execution_policy_hash',
                       'ammo_mapping_hash', 'observation_policy_hash', 'damage_policy_hash'}),
}


def semantic_action(action):
    """Copy tactical fields, excluding only known native authorization metadata.

    This is not an executable EngineAction: execute the original bound candidate.
    Actor/target/weapon/profile/version/path and all other game parameters remain.
    Unknown hash/link fields are NOT silently deleted by this projection.
    """
    result = deepcopy(action)
    parameters = result.get('parameters')
    if isinstance(parameters, dict):
        for key in _ACTION_AUTHORITY_KEYS.get(result.get('action_type'), ()):
            parameters.pop(key, None)
    return result


def technical_field_paths(value, path='$'):
    """Key paths only, never field values; useful for safe validation errors."""
    found = []
    if isinstance(value, dict):
        for key, child in value.items():
            location = path + '[' + json.dumps(str(key), ensure_ascii=False) + ']'
            if readable({key: None}) != {key: None}:
                found.append(location)
            else:
                found.extend(technical_field_paths(child, location))
    elif isinstance(value, (list, tuple)):
        for index, child in enumerate(value):
            found.extend(technical_field_paths(child, path + '[' + str(index) + ']'))
    return found


def load_record(record):
    """Read current and initial 1.0 samples without modifying the source record.

    The initial writer accidentally copied authorization hashes into current
    candidate actions. Normalize only those exact fields/locations. Old hash-only
    logs, unknown technical fields and incomplete samples remain errors.
    """
    if not isinstance(record, dict) or record.get('schema') != SCHEMA:
        return validate_record(record)
    result = deepcopy(record)
    data = result.get('policy_input')
    legal = data.get('legal_candidates') if isinstance(data, dict) else None
    if isinstance(legal, list):
        selection = result.get('selection')
        if selection is not None and selection not in legal:
            raise ValueError('selection is not a complete current candidate')
        for item in legal:
            if isinstance(item, dict) and isinstance(item.get('action'), dict):
                item['action'] = semantic_action(item['action'])
        if isinstance(selection, dict) and isinstance(selection.get('action'), dict):
            selection['action'] = semantic_action(selection['action'])
    return validate_record(result)


def compact_public_map(public_map):
    """Losslessly encode the public valid-cell list as inclusive integer runs."""
    metadata = readable({k: v for k, v in public_map.items() if k != 'valid_hex_ids'})
    runs = []
    for value in public_map.get('valid_hex_ids', ()):
        if type(value) is not int:
            raise ValueError('public map hex IDs must be integers')
        if runs and value == runs[-1][1] + 1:
            runs[-1][1] = value
        else:
            runs.append([value, value])
    return {'encoding': 'inclusive-hex-ranges/1.0', 'metadata': metadata, 'valid_hex_ranges': runs}


def expand_public_map(encoded):
    if encoded.get('encoding') != 'inclusive-hex-ranges/1.0':
        raise ValueError('unknown public map encoding')
    result = deepcopy(encoded['metadata'])
    result['valid_hex_ids'] = [v for lo, hi in encoded['valid_hex_ranges'] for v in range(lo, hi + 1)]
    return result


def action_description(action):
    if action is None:
        return '不下达指令'
    side = {'red': '红方', 'blue': '蓝方'}.get(action.get('side'), action.get('side', ''))
    actor = action.get('actor_id')
    text = str(side) + (f'{actor}号' if actor is not None else '') + '：' + _NAMES.get(action.get('action_type'), str(action.get('action_type')))
    if action.get('target_id') is not None:
        text += '，目标' + str(action['target_id'])
    if action.get('target_position') is not None:
        text += '，位置' + str(action['target_position'])
    params = action.get('parameters', {})
    if params.get('path'):
        text += '，路径' + '→'.join(str(v) for v in params['path'])
    if params.get('weapon_id') is not None:
        text += '，武器' + str(params['weapon_id'])
    return text


def candidates(binding):
    result, internal = [], {}
    rows = {r['action_id']: r['action'] for r in binding.action_space.actions}
    for action_id, ok in zip(binding.action_space.action_ids, binding.action_space.mask):
        if not ok:
            continue
        local_id = 'c' + str(len(result))
        action = semantic_action(rows[action_id])
        result.append({'id': local_id, 'action': action, 'description': action_description(action)})
        internal[action_id] = local_id
    return result, internal


def make_record(context, binding, *, match_id, controller, action_id, status, elapsed, details=None, history=()):
    legal, mapping = candidates(binding)
    details = readable(details) if isinstance(details, dict) else {}
    # Plugin diagnostics are copied before execution. A None plan explicitly
    # means this controller did not report an LLM plan, not an invented plan.
    selected_id = mapping.get(action_id)
    chosen = next((deepcopy(v) for v in legal if v['id'] == selected_id), None)
    record = {
        'schema': SCHEMA, 'match_id': match_id, 'side': context.side,
        'decision': context.decision_index,
        'window': binding.action_space.decision_window_index,
        'sim_time_ms': binding.action_space.sim_time_ms,
        'controller': controller,
        'policy_input': {
            'observation': readable(context.observation),
            'public_map': compact_public_map(context.public_map),
            'own_weapon_catalog': readable(context.weapon_catalog),
            'own_weapon_inventory': readable(binding.weapon_inventory),
            'strategic_plan': deepcopy(details.get('strategic_plan')),
            'legal_candidates': legal,
        },
        'input_scope': 'detached side-visible facts and legal choices; not a simulator checkpoint or serialized policy tensor',
        'history': readable(list(history)),
        'history_scope': 'up to four previous same-side decision outcomes; not extra model input',
        'selection': chosen,
        'decision_status': status, 'elapsed_seconds': elapsed,
        'model_details': details,
        'execution': {'status': 'not_submitted'},
        'feedback': None,
    }
    # Reject non-JSON/NaN diagnostics instead of silently publishing bad samples.
    json.dumps(record, ensure_ascii=False, allow_nan=False)
    return record


def finish_record(record, *, results, selected_action_id, post_observation, after_time_ms):
    matches = [r for r in results if r.get('action_id') == selected_action_id] if selected_action_id else []
    if selected_action_id is None:
        execution = {'status': 'no_controller_command', 'native_results': readable(results)}
    elif len(matches) == 1:
        receipt = matches[0]
        execution = {'status': 'accepted' if receipt.get('accepted') is True else 'rejected',
                     'receipt': readable(receipt)}
    else:
        execution = {'status': 'receipt_unavailable', 'native_results': readable(results)}
    record['execution'] = execution
    record['feedback'] = {'sim_time_ms': after_time_ms,
                          'observation': readable(post_observation),
                          'scope': 'same-side observation after one window; acceptance is not task completion'}
    return record


def history_item(record):
    return {key: deepcopy(record[key]) for key in
            ('window', 'sim_time_ms', 'selection', 'execution')}


def validate_record(record):
    """Fail clearly for old hash-only logs; there is no fabricated migration."""
    if not isinstance(record, dict) or record.get('schema') != SCHEMA:
        raise ValueError('requires native-decision-sample/1.0; old hash-only logs cannot supply missing observations/plans')
    for key in ('match_id', 'side', 'decision', 'window', 'sim_time_ms', 'policy_input', 'selection', 'execution', 'history'):
        if key not in record:
            raise ValueError('incomplete decision sample: ' + key)
    if not isinstance(record['match_id'], str) or not record['match_id'] or record['side'] not in ('red', 'blue'):
        raise ValueError('invalid decision identity')
    if any(type(record[k]) is not int or record[k] < 0 for k in ('decision', 'window', 'sim_time_ms')):
        raise ValueError('invalid decision time/index')
    if not isinstance(record['history'], list) or not isinstance(record['execution'], dict):
        raise ValueError('invalid history or execution')
    data = record['policy_input']
    if not isinstance(data, dict) or any(k not in data for k in ('observation', 'strategic_plan', 'legal_candidates')):
        raise ValueError('incomplete policy input')
    if not isinstance(data['observation'], dict) or data['observation'].get('side') != record['side']:
        raise ValueError('invalid side observation')
    if data['strategic_plan'] is not None and not isinstance(data['strategic_plan'], dict):
        raise ValueError('invalid strategic plan')
    legal = data['legal_candidates']
    if not isinstance(legal, list) or len(legal) > 255:
        raise ValueError('invalid legal candidate list')
    ids = set()
    for index, item in enumerate(legal):
        if not isinstance(item, dict) or set(item) != {'id', 'action', 'description'} or not isinstance(item['action'], dict):
            raise ValueError('incomplete candidate')
        if item['id'] != 'c' + str(index) or item['action'].get('side') != record['side'] or not isinstance(item['description'], str):
            raise ValueError('invalid local candidate identity')
        if item['id'] in ids:
            raise ValueError('duplicate candidate ID')
        ids.add(item['id'])
    if record['selection'] is not None and record['selection'] not in legal:
        raise ValueError('selection is not a complete current candidate')
    if readable(record) != record:
        raise ValueError('business decision record contains technical hash/link fields at ' + ', '.join(technical_field_paths(record)[:12]))
    json.dumps(record, allow_nan=False)
    return record
