"""Weapon-lock capability additions invalidate old tensor/checkpoint identities."""
from copy import deepcopy
import json
from pathlib import Path
import unittest

from wargame_native_engine.training.decision_encoder import (
    DecisionObservationEncoder, DecisionObservationEncoderV2,
)
from wargame_native_engine.training.learner_preprocessor import (
    build_learner_observation_preprocessor_spec, LearnerPreprocessorContractError,
)
from wargame_native_engine.training.ppo_config import load_ppo_algorithm_config, build_ppo_algorithm_config
from wargame_native_engine.training.ppo_runner_config import load_ppo_runner_config
from wargame_native_engine.training.ppo_vector_runner_config import load_ppo_vector_runner_config
from wargame_native_engine.training.ppo_parallel_runner_config import load_ppo_parallel_runner_config
from wargame_native_engine.training.objective_task import load_objective_task_config

ROOT = Path(__file__).resolve().parents[1] / 'config/training'


class V19TrainingContractTests(unittest.TestCase):
    def test_all_updated_authorities_load(self):
        for version, encoder in ((1, DecisionObservationEncoder()), (2, DecisionObservationEncoderV2())):
            data = json.loads((ROOT / f'native_ppo_observation_preprocessor_v{version}.json').read_text())
            spec = build_learner_observation_preprocessor_spec(data)
            self.assertEqual(spec.input_shape, encoder.observation_space.flat_shape)
            self.assertEqual(spec.input_encoder_hash, encoder.contract_hash)
            ppo = load_ppo_algorithm_config(ROOT / f'native_masked_ppo_v{version}.json')
            runner = load_ppo_runner_config(ROOT / f'native_ppo_runner_v{version}.json', ppo=ppo)
            self.assertEqual(runner.ppo_config.identity_hash, ppo.content_hash)
        vector = load_ppo_vector_runner_config()
        parallel = load_ppo_parallel_runner_config()
        self.assertEqual(parallel.base_vector_runner.identity_hash, vector.content_hash)

    def test_objective_task_uses_current_encoder(self):
        load_objective_task_config(
            ROOT / "public_objective_capture_task_v1.json",
            reward_policy_path=ROOT / "native_objective_first_control_reward_v1.json",
        )
        raw = json.loads((ROOT / "public_objective_capture_task_v1.json").read_text())
        self.assertEqual(raw["environment"]["encoder"]["hash"], DecisionObservationEncoderV2().contract_hash)

    def test_old_encoder_schema_hash_and_shape_rejected(self):
        base = json.loads((ROOT / 'native_ppo_observation_preprocessor_v1.json').read_text())
        mutations = (
            ('schema_version', 'native-decision-observation-encoder/1.0'),
            ('contract_hash', 'a477d7953c4f3e0a52f13aa2bc12ea08d1327fff7a8bdfe8d91c41a9e51ed750'),
            ('shape', [8302]),
        )
        for field, old in mutations:
            candidate = deepcopy(base)
            candidate['input_encoder'][field] = old
            with self.subTest(field=field), self.assertRaises((ValueError, LearnerPreprocessorContractError)):
                build_learner_observation_preprocessor_spec(candidate)

    def test_old_action_identity_rejected(self):
        base = json.loads((ROOT / 'native_masked_ppo_v1.json').read_text())
        for field, old in (
            ('schema_version', 'native-decision-action-encoder/1.0'),
            ('hash', 'aa08ad27d84634f76d607d7890abe953a911b57b4043b311fdc85dd2813b83dc'),
        ):
            candidate = deepcopy(base)
            candidate['action_encoder_ref'][field] = old
            with self.subTest(field=field), self.assertRaises(ValueError):
                build_ppo_algorithm_config(candidate)


if __name__ == '__main__':
    unittest.main()
