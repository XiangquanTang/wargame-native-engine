"""Seat-only controller contracts and opt-in local plugin workers.

Plugins are trusted local Python code, not a security sandbox. Only detached
seat observations and public static data cross the worker boundary. No provider,
network client, credentials, world handle or opponent observation is supplied.
"""
from __future__ import annotations
from copy import deepcopy
from dataclasses import dataclass
import hashlib
import importlib
import multiprocessing as mp
from pathlib import Path
import random
from typing import Any


@dataclass(frozen=True)
class ControllerContext:
    side: str
    seed: int
    decision_index: int
    observation: dict
    binding: Any
    public_map: dict
    weapon_catalog: dict


class Controller:
    kind = 'base'
    def reset(self, context):
        pass
    def decide(self, context):
        raise NotImplementedError
    def close(self):
        pass
    def decision_details(self):
        return None
    def provenance(self):
        return {'kind': self.kind}


class NoOpController(Controller):
    kind = 'no-op'
    def decide(self, context):
        return None


class RandomController(Controller):
    kind = 'random-legal'
    def reset(self, context):
        self.rng = random.Random(context.seed ^ (0x11 if context.side == 'red' else 0x22))
    def decide(self, context):
        space = context.binding.action_space
        ids = [v for v, allowed in zip(space.action_ids, space.mask) if allowed]
        return self.rng.choice(ids) if ids else None


class RulesController(Controller):
    """Trusted adapter: one SimpleCaptureTeacher command per shared window."""
    kind = 'rules'
    def __init__(self):
        from wargame_native_engine.training.minimal_teacher import SimpleCaptureTeacher
        self.teacher = SimpleCaptureTeacher()
    def select_seat(self, seat):
        selection = self.teacher.select(seat)
        if selection.action_index is None:
            return None
        return selection.binding.action_space.action_ids[selection.action_index]
    def close(self):
        self.teacher.close()
    def provenance(self):
        return {'kind': self.kind, 'adapter': self.teacher.teacher_id, 'commands_per_window': 1}


class PPOController(Controller):
    kind = 'ppo'
    def __init__(self, checkpoint, device='cpu'):
        from wargame_native_engine.training.minimal_ppo import MinimalPPO, PPOConfig, SCHEMA, ENCODER_SCHEMAS, torch
        path = Path(checkpoint)
        self.checkpoint_hash = hashlib.sha256(path.read_bytes()).hexdigest()
        payload = torch.load(path, map_location=device, weights_only=True)
        state = payload['learner']
        if state.get('schema') != SCHEMA:
            raise ValueError('incompatible minimal PPO checkpoint schema')
        self.encoder_schema = state.get('encoder_schema', SCHEMA)
        if self.encoder_schema not in ENCODER_SCHEMAS:
            raise ValueError('incompatible compact encoder schema')
        if payload.get('metadata', {}).get('encoder_schema', self.encoder_schema) != self.encoder_schema:
            raise ValueError('checkpoint encoder metadata mismatch')
        self.learner = MinimalPPO(PPOConfig(**state['config']), seed=0, device=device, encoder_schema=self.encoder_schema)
        # Match inference intentionally loads ONLY weights, not training optimizer,
        # opponent constraints, RNG state or update authority. No training occurs.
        self.learner.model.load_state_dict(state['model'], strict=True)
        self.learner.model.eval()
        for parameter in self.learner.model.parameters():
            parameter.requires_grad_(False)
        self.training_metadata = {k: payload.get('metadata', {}).get(k)
                                  for k in ('side', 'opponent', 'schema', 'decision_ms', 'candidate_limit', 'map', 'scenario', 'parameters')}
        self.device = str(self.learner.device)
    def reset(self, context):
        from wargame_native_engine.training.minimal_ppo import CompactEncoder
        cells = [{'hex_id': h} for h in context.public_map['valid_hex_ids']]
        positions = {int(cell['hex_id']): (float(int(cell['hex_id']) % 100),
                                         float(int(cell['hex_id']) // 100)) for cell in cells}
        scale = max(1., max((max(p) for p in positions.values()), default=1.))
        profiles = {int(v['weapon_id']): v for v in context.weapon_catalog['weapons']}
        self.encoder = CompactEncoder(positions, scale, profiles, schema=self.encoder_schema)
    def decide(self, context):
        # Current own profiles can change on delayed reinforcement arrival.
        self.encoder.weapon_profiles = {int(v['weapon_id']): deepcopy(v)
                                        for v in context.weapon_catalog['weapons']}
        action = self.learner.act(self.encoder.encode(context.binding), deterministic=True)
        return context.binding.action_space.action_ids[action.action_index]
    def provenance(self):
        return {'kind': self.kind, 'checkpoint_sha256': self.checkpoint_hash,
                'device': self.device, 'deterministic_argmax': True, 'frozen': True,
                'training_metadata': self.training_metadata, 'metadata_policy': 'standalone-match-not-training-resume'}


def _factory(spec, config):
    module, separator, name = spec.partition(':')
    if not separator or not module or not name:
        raise ValueError('plugin must be module:factory')
    return getattr(importlib.import_module(module), name)(deepcopy(config))


def _plugin_worker(pipe, planner, evaluator, config):
    objects = []
    try:
        objects.append(_factory(planner, config))
        if evaluator:
            objects.append(_factory(evaluator, config))
        pipe.send(('ok', None))
        while True:
            method, context = pipe.recv()
            if method == 'close':
                for obj in objects:
                    obj.close()
                pipe.send(('ok', None))
                return
            if method == 'reset':
                for obj in objects:
                    obj.reset(context)
                result = None
            elif evaluator:
                proposals = objects[0].propose(deepcopy(context))
                legal = {v for v, ok in zip(context.binding.action_space.action_ids,
                                           context.binding.action_space.mask) if ok}
                if not isinstance(proposals, (list, tuple)) or not proposals or any(
                        not isinstance(v, str) or v not in legal for v in proposals):
                    raise ValueError('hybrid proposals must be nonempty legal action IDs')
                result = objects[1].evaluate(deepcopy(context), tuple(proposals))
                if result is not None and result not in proposals:
                    raise ValueError('hybrid evaluation must select a proposed action')
            else:
                result = objects[0].decide(context)
            if method == 'decide':
                getter = getattr(objects[-1], 'decision_details', None)
                details = getter() if callable(getter) else None
                result = {'selection': result, 'details': details}
            pipe.send(('ok', result))
    except BaseException as exc:
        # Avoid leaking plugin prompt, credentials or provider response via errors.
        try:
            pipe.send(('error', type(exc).__name__))
        except (BrokenPipeError, EOFError, OSError):
            pass
    finally:
        pipe.close()


class PluginController(Controller):
    kind = 'llm'
    def __init__(self, plugin=None, config=None, timeout_seconds=5., *, evaluator=None):
        import math
        if not plugin:
            raise ValueError('LLM controller is unconfigured: supply an explicit local module:factory plugin')
        if not math.isfinite(timeout_seconds) or timeout_seconds <= 0:
            raise ValueError('timeout_seconds must be finite and positive')
        self.plugin, self.evaluator = plugin, evaluator
        self.config = deepcopy(config or {})
        self.timeout_seconds = timeout_seconds
        self.process = self.pipe = None
        self._decision_details = None
    def _receive(self):
        if not self.pipe.poll(self.timeout_seconds):
            self._stop()
            raise TimeoutError('controller deadline exceeded; worker terminated')
        status, value = self.pipe.recv()
        if status != 'ok':
            self._stop()
            raise RuntimeError('plugin failed: ' + str(value))
        return value
    def reset(self, context):
        self._stop()
        ctx = mp.get_context('spawn')
        parent, child = ctx.Pipe()
        self.pipe = parent
        self.process = ctx.Process(target=_plugin_worker,
                                   args=(child, self.plugin, self.evaluator, self.config), daemon=True)
        self.process.start()
        child.close()
        self._receive()
        self._rpc('reset', context)
    def _rpc(self, method, context):
        if self.process is None or not self.process.is_alive():
            raise RuntimeError('plugin worker unavailable')
        self.pipe.send((method, context))
        return self._receive()
    def decide(self, context):
        self._decision_details = None
        result = self._rpc('decide', context)
        self._decision_details = deepcopy(result['details'])
        return result['selection']
    def decision_details(self):
        return deepcopy(self._decision_details)
    def _stop(self):
        if self.process is not None:
            if self.process.is_alive():
                self.process.terminate()
            self.process.join(timeout=2.)
            if self.process.is_alive():
                self.process.kill()
                self.process.join(timeout=2.)
            self.process.close()
            self.process = None
        if self.pipe is not None:
            self.pipe.close()
            self.pipe = None
    def close(self):
        try:
            if self.process is not None and self.process.is_alive():
                self._rpc('close', None)
        finally:
            self._stop()
    def provenance(self):
        return {'kind': self.kind, 'plugin': self.plugin, 'evaluator': self.evaluator,
                'timeout_seconds': self.timeout_seconds, 'external_calls': 'only-if-explicit-plugin-implements-them'}


class HybridController(PluginController):
    kind = 'llm-jev'
    def __init__(self, planner=None, evaluator=None, config=None, timeout_seconds=5.):
        if not evaluator:
            raise ValueError('LLM+JEV is unconfigured: explicit evaluator plugin required')
        super().__init__(planner, config, timeout_seconds, evaluator=evaluator)
