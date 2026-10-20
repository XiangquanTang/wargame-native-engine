"""Pure-Python frozen demonstration records; no optimizer or tensor backend."""
from dataclasses import asdict
import json
import math
from pathlib import Path

from wargame_native_engine.io.serialization import canonical_hash, canonical_json, file_sha256
from wargame_native_engine.projection.decision_semantic import DecisionSide, DecisionCapabilityFamily
from wargame_native_engine.projection.decision_capability import ActionCatalogEntry, ActionCatalogSnapshot
from wargame_native_engine.training.decision_encoder import (
    EncodedDecisionObservation, EncodedDecisionObservationV2, DecisionActionEncoder,
)

SCHEMA = 'native-bound-demonstration/1.0'


def validate_input_label(values, mask, index):
    """Shared by BC samples and streamed engineering demonstrations."""
    if not values or any(type(v) not in (int, float) or not math.isfinite(v) for v in values):
        raise ValueError('sample observation must be finite and non-empty')
    if not mask or any(type(v) is not bool for v in mask):
        raise ValueError('sample action mask must contain exact bools')
    if type(index) is not int or not 0 <= index < len(mask):
        raise ValueError('sample action index is out of range')
    if not mask[index]:
        raise ValueError('expert label is masked')


def restore_observation(value):
    source = dict(value)
    base = dict(source.pop('base'))
    base['side'] = DecisionSide(base['side'])
    for k, v in base.items():
        if isinstance(v, list):
            base[k] = tuple(tuple(row) if isinstance(row, list) else row for row in v)
    for k, v in source.items():
        if isinstance(v, list):
            source[k] = tuple(tuple(row) if isinstance(row, list) else row for row in v)
    return EncodedDecisionObservationV2(base=EncodedDecisionObservation(**base), **source)


def restore_catalog(value):
    entries = []
    for raw in value['entries']:
        entry = dict(raw)
        expected = entry.pop('semantic_hash')
        entry['capability_family'] = DecisionCapabilityFamily(entry['capability_family'])
        entry['canonical_parameters_json'] = canonical_json(entry.pop('parameters'))
        if entry['target_position'] is not None:
            entry['target_position'] = tuple(entry['target_position'])
        item = ActionCatalogEntry(**entry)
        if item.semantic_hash != expected:
            raise ValueError('catalog entry hash mismatch')
        entries.append(item)
    catalog = ActionCatalogSnapshot(schema_version=value['schema_version'], side=DecisionSide(value['side']),
        decision_window_index=value['decision_window_index'], frame_index=value['frame_index'],
        sim_time_ms=value['sim_time_ms'], entries=tuple(entries))
    if catalog.catalog_hash != value['catalog_hash'] or len(entries) != value['action_count']:
        raise ValueError('catalog hash/count mismatch')
    return catalog


def prepare_sample(env, binding, index, preprocessor, identity):
    processed = preprocessor.preprocess(binding.observation)
    validate_input_label(processed.values, binding.action_space.mask, index)
    return dict(schema_version=SCHEMA, identity=identity,
        window=binding.observation.decision_window_index, frame=binding.observation.frame_index,
        sim_time_ms=binding.observation.sim_time_ms,
        binding_scope=binding.binding_scope_id,
        provider_hash=binding.spatial_provider_config_hash,
        inputs=dict(visible=env.current_side_observation, encoded=asdict(binding.observation),
                    processed=processed.to_record(include_values=True),
                    catalog=env.current_catalog_record, inventory=list(binding.weapon_inventory),
                    action_space=binding.action_space.to_record()),
        supervision=dict(expert_index=index, expert_action=binding.action_space.actions[index]))


def validate_sample(record, preprocessor, identity):
    if record.get('schema_version') != SCHEMA or record.get('identity') != identity:
        raise ValueError('sample schema/identity mismatch')
    if record.get('provider_hash') != identity['provider']['config_hash']:
        raise ValueError('sample provider identity mismatch')
    inputs = record['inputs']
    observation = restore_observation(inputs['encoded'])
    processed = preprocessor.preprocess(observation).to_record(include_values=True)
    if processed != inputs['processed']:
        raise ValueError('processed input mismatch')
    catalog = restore_catalog(inputs['catalog'])
    space = DecisionActionEncoder(256).encode(catalog)
    if (space.to_record() != inputs['action_space'] or observation.catalog_hash != catalog.catalog_hash
        or observation.action_candidate_mask != space.mask):
        raise ValueError('frozen catalog/encoding mismatch')
    from wargame_native_engine.training.decision_encoder import _encode_weapon_inventory, _encode_action_candidate, DecisionEncoderV2Config
    units, weapons, pools, refs = _encode_weapon_inventory(inputs['inventory'], DecisionEncoderV2Config())
    if observation.weapon_inventory_hash != canonical_hash(inputs['inventory']):
        raise ValueError('inventory hash mismatch')
    if tuple(_encode_action_candidate(i, e, refs) for i, e in enumerate(catalog.entries)) != observation.action_candidate_features[:len(catalog.entries)]:
        raise ValueError('candidate feature semantics mismatch')
    for actual, expected_rows in ((observation.inventory_unit_features, units),
                                  (observation.weapon_inventory_features, weapons),
                                  (observation.ammunition_pool_features, pools)):
        if actual[:len(expected_rows)] != expected_rows:
            raise ValueError('inventory feature semantics mismatch')
    index = record['supervision']['expert_index']
    validate_input_label(processed['values'], space.mask, index)
    if index >= len(space.actions) or space.actions[index] != record['supervision']['expert_action']:
        raise ValueError('index/action semantic mismatch')
    if (record['window'], record['frame'], record['sim_time_ms']) != (
            catalog.decision_window_index, catalog.frame_index, catalog.sim_time_ms):
        raise ValueError('window binding mismatch')
    if (observation.decision_window_index, observation.frame_index, observation.sim_time_ms) != (
            catalog.decision_window_index, catalog.frame_index, catalog.sim_time_ms):
        raise ValueError('observation binding mismatch')
    next_observation = restore_observation(record['audit']['next_encoded'])
    preprocessor.preprocess(next_observation)
    if next_observation.frame_index <= observation.frame_index:
        raise ValueError('next observation did not advance')
    return canonical_hash([processed['values'], list(space.mask)]), canonical_hash(record['supervision']['expert_action']['action'])


def validate_dataset(path, preprocessor, identity, *, maximum_samples=96, task_context_complete=True):
    seen, conflicts, count = {}, [], 0
    with Path(path).open() as stream:
        for line in stream:
            count += 1
            if count > maximum_samples:
                raise ValueError('dataset sample budget exceeded')
            record = json.loads(line)
            signature, label = validate_sample(record, preprocessor, identity)
            if signature in seen and seen[signature] != label:
                conflicts.append(count - 1)
            seen[signature] = label
    blockers = ([] if task_context_complete else [
        'Ordered multi-action itinerary is held by the scripted driver; no ordered public task-goal input contract was established.'])
    return dict(valid_samples=count, unique_inputs=len(seen), same_input_label_conflicts=conflicts,
                structural_validation='PASS' if count else 'EMPTY', task_context_blockers=blockers,
                status='TRAINING_READY' if count and not conflicts and not blockers else 'BLOCKED',
                dataset_sha256=file_sha256(path), classification='SCRIPTED_ENGINEERING_DEMONSTRATIONS',
                split='NONE; repeat is determinism evidence; future splits must use complete episodes/layouts')
