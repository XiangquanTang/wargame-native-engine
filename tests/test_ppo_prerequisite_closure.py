from __future__ import annotations

import ast
import json
import math
from pathlib import Path
import random
import subprocess
import sys
from types import SimpleNamespace

import pytest

from wargame_native_engine.core.rng import NativeRNG
from wargame_native_engine.io.serialization import canonical_hash
from wargame_native_engine.projection.decision_semantic import DecisionSide
from wargame_native_engine.training import (
    CUSTOM_PYTORCH_BACKEND_KIND,
    DECISION_OBSERVATION_ENCODER_SCHEMA_VERSION,
    LEARNER_OBSERVATION_PREPROCESSOR_SCHEMA_VERSION,
    MAX_TRAINING_SEED,
    TRAINING_SEED_STREAM_ORDER,
    DecisionActionEncoder,
    DecisionObservationEncoder,
    EncodedDecisionObservation,
    LearnerDependencyError,
    LearnerObservationPreprocessor,
    LearnerPreprocessorContractError,
    TrainingSeedContractError,
    TrainingSeedStream,
    build_learner_observation_preprocessor_spec,
    build_training_seed_config,
    load_eight_factor_shaping_policy,
    load_learner_backend_authority,
    load_learner_observation_preprocessor_spec,
    load_reward_policy,
    load_shared_training_discount_config,
    load_training_seed_config,
    python_random_for_stream,
    seed_torch_cpu,
)
from wargame_native_engine.training.decision_encoder import (
    CAPABILITY_FEATURES,
    OBJECTIVE_FEATURES,
    OBSERVED_ENEMY_FEATURES,
    OWN_OPERATOR_FEATURES,
    SNAPSHOT_FEATURES,
)
import wargame_native_engine.training.learner_backend as backend_module
import wargame_native_engine.training.learner_preprocessor as preprocessor_module
import wargame_native_engine.training.training_seed as seed_module


ROOT = Path(__file__).resolve().parents[1]
PREPROCESSOR_CONFIG = (
    ROOT / "config/training/native_ppo_observation_preprocessor_v1.json"
)
OBSERVATION_HASH = (
    "a477d7953c4f3e0a52f13aa2bc12ea08d1327fff7a8bdfe8d91c41a9e51ed750"
)
ACTION_HASH = (
    "aa08ad27d84634f76d607d7890abe953a911b57b4043b311fdc85dd2813b83dc"
)
DISCOUNT_HASH = (
    "f35a748f9d0232506135b7e2438fa64f5bcc601a7aeb1fc631b182f396e4f06e"
)
BASE_HASH = (
    "5653b7653eb46d3f091d5d0c5c04ab7dd5ce380236a4d2455214ab735a39b0cf"
)
SHAPING_HASH = (
    "bc21e92be00f9f9243b436741321f7d326f51a8bb734386a6de880dad37d7e81"
)


def _row(names: tuple[str, ...], values: dict[str, float]) -> tuple[float, ...]:
    return tuple(float(values.get(name, 0.0)) for name in names)


def _encoded_fixture() -> EncodedDecisionObservation:
    snapshot = _row(SNAPSHOT_FEATURES, {
        "side": 1,
        "stage": 2,
        "sim_time_ms": 20_000,
        "objective_count": 1,
        "own_operator_count": 1,
        "observed_enemy_count": 1,
        "action_count": 48,
        "max_ticks_present": 1,
        "max_ticks": 3_600,
    })
    objective = _row(OBJECTIVE_FEATURES, {
        "objective_reference": 1,
        "x": 10,
        "y": 8,
        "radius": 2,
        "value": 1,
    })
    own = _row(OWN_OPERATOR_FEATURES, {
        "operator_reference": 7,
        "alive": 1,
        "health": 100,
        "max_health": 100,
        "health_ratio": 1,
        "current_hex_present": 1,
        "current_hex": 115,
        "segment_progress_present": 1,
        "segment_progress_millionths": 500_000,
        "segment_remaining_present": 1,
        "segment_remaining_ms": 75_000,
        "stop_transition_remaining_ms": 5_000,
        "transition_active": 1,
        "transition_remaining_ms": 75_000,
        "fatigue_recovery_remaining_present": 1,
        "fatigue_recovery_remaining_ms": 150_000,
        "suppression_active": 1,
        "suppression_remaining_ms": 150_000,
        "boarding_transition_remaining_present": 1,
        "boarding_transition_remaining_ms": 75_000,
        "weapon_cooldown_active": 1,
        "weapon_cooldown_remaining_ms": 30_000,
        "guided_remaining_present": 1,
        "guided_remaining_ms": 10_000,
        "indirect_remaining_present": 1,
        "indirect_remaining_ms": 20_000,
        "limited_enabled": 1,
        "limited_remaining_present": 1,
        "limited_remaining_ms": 1_200_000,
    })
    enemy = _row(OBSERVED_ENEMY_FEATURES, {
        "operator_reference": 30,
        "domain": 3,
        "canonical_status": 1,
        "x": 15,
        "y": 4,
        "altitude": 2,
        "alive": 1,
    })
    capability = _row(CAPABILITY_FEATURES, {
        "operator_reference": 7,
        "WAIT.status": 4,
        "WAIT.instance_count_present": 1,
        "WAIT.instance_count": 1,
        "DIRECT_FIRE.status": 4,
        "DIRECT_FIRE.instance_count_present": 1,
        "DIRECT_FIRE.instance_count": 48,
    })
    return EncodedDecisionObservation(
        side=DecisionSide.RED,
        decision_window_index=2,
        frame_index=20,
        sim_time_ms=20_000,
        decision_semantic_hash="1" * 64,
        encoder_schema_version=DECISION_OBSERVATION_ENCODER_SCHEMA_VERSION,
        encoder_contract_hash=OBSERVATION_HASH,
        snapshot_features=snapshot,
        objective_features=(objective,) + ((0.0,) * len(OBJECTIVE_FEATURES),) * 31,
        objective_mask=(True,) + (False,) * 31,
        own_operator_features=(own,) + ((0.0,) * len(OWN_OPERATOR_FEATURES),) * 63,
        own_operator_mask=(True,) + (False,) * 63,
        observed_enemy_features=(enemy,) + (
            ((0.0,) * len(OBSERVED_ENEMY_FEATURES),) * 63
        ),
        observed_enemy_mask=(True,) + (False,) * 63,
        capability_features=(capability,) + (
            ((0.0,) * len(CAPABILITY_FEATURES),) * 63
        ),
        capability_mask=(True,) + (False,) * 63,
    )


def _feature_value(processed, spec, group: str, name: str, occurrence: int = 0):
    record = next(
        item
        for item in spec.feature_transforms
        if item.group == group and item.feature_name == name
    )
    return processed.values[record.indices[occurrence]]


def test_backend_authority_optional_dependency_and_engine_import_isolation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    authority = load_learner_backend_authority()
    assert authority.backend_kind == CUSTOM_PYTORCH_BACKEND_KIND
    assert authority.backend_id == "custom-pytorch-ppo"
    assert authority.backend_version == "1.0.0"
    assert authority.framework_compatibility == ">=2.2,<3"
    assert authority.semantic_hash == canonical_hash(authority.semantic_payload())
    backend_schema = json.loads((
        ROOT / "config/schemas/native_learner_backend_authority.schema.json"
    ).read_text(encoding="utf-8"))
    assert backend_schema["additionalProperties"] is False
    assert set(backend_schema["required"]) == set(authority.semantic_payload())
    pyproject = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    assert 'learner = ["torch>=2.2,<3"]' in pyproject
    assert "stable-baselines3" not in pyproject
    assert "sb3-contrib" not in pyproject
    assert "rllib" not in pyproject
    assert "gymnasium" not in pyproject

    source = Path(backend_module.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)
    assert not any(
        isinstance(node, (ast.Import, ast.ImportFrom))
        and (
            any(alias.name == "torch" for alias in node.names)
            if isinstance(node, ast.Import)
            else node.module == "torch"
        )
        for node in ast.walk(tree)
    )
    probe = subprocess.run(
        [
            sys.executable,
            "-c",
            "import sys; import wargame_native_engine; "
            "assert 'torch' not in sys.modules",
        ],
        cwd=ROOT,
        env={"PYTHONPATH": str(ROOT / "src"), "PYTHONDONTWRITEBYTECODE": "1"},
        check=False,
        capture_output=True,
        text=True,
    )
    assert probe.returncode == 0, probe.stderr

    def missing(name: str):
        assert name == "torch"
        raise ModuleNotFoundError("torch")

    monkeypatch.setattr(backend_module.importlib, "import_module", missing)
    with pytest.raises(LearnerDependencyError, match=r"\.\[learner\]"):
        backend_module.require_pytorch(authority)


def test_backend_runtime_patch_is_provenance_not_semantic_identity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    authority = load_learner_backend_authority()
    versions = iter(("2.2.0+cpu", "2.2.1+cpu"))
    monkeypatch.setattr(
        backend_module.importlib,
        "import_module",
        lambda name: SimpleNamespace(__version__=next(versions)),
    )
    first = backend_module.learner_backend_runtime_provenance(authority)
    second = backend_module.learner_backend_runtime_provenance(authority)
    assert first.framework_version != second.framework_version
    assert first.backend_semantic_hash == second.backend_semantic_hash
    assert first.backend_semantic_hash == authority.semantic_hash


def test_pp1_pp3_same_input_shape_and_explicit_float32_output() -> None:
    encoded = _encoded_fixture()
    preprocessor = LearnerObservationPreprocessor()
    one = preprocessor.preprocess(encoded)
    two = preprocessor.preprocess(encoded)
    assert one == two
    assert one.values == two.values
    assert one.shape == (8302,)
    assert len(one.values) == 8302
    assert one.dtype == "float32"
    assert one.learner_tensor_hash == two.learner_tensor_hash
    assert all(math.isfinite(item) for item in one.values)


def test_pp4_pp6_encoder_mismatch_nan_and_inf_fail_closed() -> None:
    encoded = _encoded_fixture()
    preprocessor = LearnerObservationPreprocessor()
    values = list(encoded.tensor)
    with pytest.raises(LearnerPreprocessorContractError, match="identity mismatch"):
        preprocessor.preprocess_values(
            values,
            encoder_schema=encoded.encoder_schema_version,
            encoder_hash="0" * 64,
        )
    for invalid in (float("nan"), float("inf"), float("-inf")):
        broken = list(values)
        broken[0] = invalid
        with pytest.raises(LearnerPreprocessorContractError, match="finite"):
            preprocessor.preprocess_values(
                broken,
                encoder_schema=encoded.encoder_schema_version,
                encoder_hash=encoded.encoder_contract_hash,
            )


def test_pp7_pp9_masks_padding_and_fixed_timer_transforms() -> None:
    encoded = _encoded_fixture()
    preprocessor = LearnerObservationPreprocessor()
    spec = preprocessor.spec
    result = preprocessor.preprocess(encoded)
    assert _feature_value(result, spec, "snapshot", "sim_time_ms") == 20.0
    assert _feature_value(
        result, spec, "own", "segment_progress_millionths"
    ) == 0.5
    assert _feature_value(result, spec, "own", "segment_remaining_ms") == 75.0
    assert _feature_value(result, spec, "own", "suppression_remaining_ms") == 150.0
    assert _feature_value(result, spec, "own", "limited_remaining_ms") == 1.0
    assert _feature_value(result, spec, "snapshot", "objective_count") == 1 / 32
    assert _feature_value(result, spec, "snapshot", "own_operator_count") == 1 / 64
    assert _feature_value(result, spec, "snapshot", "action_count") == 48 / 256

    for group in (
        "objective_mask", "own_operator_mask", "observed_enemy_mask",
        "capability_mask",
    ):
        record = next(item for item in spec.feature_transforms if item.group == group)
        assert result.values[record.indices[0]] == 1.0
        assert set(result.values[index] for index in record.indices) <= {0.0, 1.0}
    own_ref = next(
        item for item in spec.feature_transforms
        if item.group == "own" and item.feature_name == "operator_reference"
    )
    assert result.values[own_ref.indices[1]] == 0.0


def test_pp10_pp14_no_statistics_state_and_hash_order_contract() -> None:
    spec = load_learner_observation_preprocessor_spec()
    assert spec.schema_version == LEARNER_OBSERVATION_PREPROCESSOR_SCHEMA_VERSION
    assert spec.source_config["batch_statistics"] == "NONE"
    assert spec.source_config["running_state"] == "NONE"
    assert spec.source_config["clipping"] == "NONE"
    assert not hasattr(LearnerObservationPreprocessor(), "running_mean")
    with pytest.raises(TypeError):
        spec.source_config["clipping"] = "CLIP"  # type: ignore[index]
    assert load_learner_observation_preprocessor_spec() is spec
    assert spec.semantic_hash == canonical_hash(spec.semantic_payload())

    timer = next(
        item for item in spec.feature_transforms
        if item.group == "snapshot" and item.feature_name == "sim_time_ms"
    )
    changed = spec.semantic_payload()
    changed_timer = next(
        item
        for item in changed["feature_transforms"]
        if item["group"] == "snapshot" and item["feature_name"] == "sim_time_ms"
    )
    changed_timer["scale"] = timer.scale * 2
    assert canonical_hash(changed) != spec.semantic_hash

    document = json.loads(PREPROCESSOR_CONFIG.read_text(encoding="utf-8"))
    reordered = {key: document[key] for key in reversed(tuple(document))}
    assert build_learner_observation_preprocessor_spec(reordered).semantic_hash == (
        spec.semantic_hash
    )

    schema = json.loads((
        ROOT / "config/schemas/native_ppo_observation_preprocessor.schema.json"
    ).read_text(encoding="utf-8"))
    assert schema["additionalProperties"] is False
    assert schema["properties"]["source_authorities"][
        "additionalProperties"
    ] is False
    assert schema["properties"]["policies"]["additionalProperties"] is False


def test_pp15_pp16_existing_encoder_hash_and_policy_isolation() -> None:
    assert DecisionObservationEncoder().contract_hash == OBSERVATION_HASH
    assert DecisionActionEncoder().contract_hash == ACTION_HASH
    spec = load_learner_observation_preprocessor_spec()
    record = LearnerObservationPreprocessor().preprocess(
        _encoded_fixture()
    ).to_record(include_values=False)
    serialized = json.dumps(
        {"spec": spec.to_dict(), "record": record}, sort_keys=True
    ).lower()
    assert "green" not in serialized
    assert "eightfactor" not in serialized
    assert "reward" not in record
    assert "winner" not in record


def test_torch_tensor_boundary_is_lazy_cpu_and_explicit_float32(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = []
    fake = SimpleNamespace(
        float32=object(),
        as_tensor=lambda values, *, dtype, device: calls.append(
            (tuple(values), dtype, device)
        ) or "tensor",
    )
    monkeypatch.setattr(preprocessor_module, "require_pytorch", lambda: fake)
    result = LearnerObservationPreprocessor().preprocess(_encoded_fixture())
    assert result.to_torch_cpu() == "tensor"
    assert calls[0][1] is fake.float32
    assert calls[0][2] == "cpu"


def test_sd1_sd6_seed_hash_and_coordinate_derivation() -> None:
    config = load_training_seed_config()
    assert config.content_hash == canonical_hash(config.semantic_payload())
    assert load_training_seed_config() is config
    changed = build_training_seed_config(root_seed=43)
    assert changed.content_hash != config.content_hash
    sampling = config.derive_seed(TrainingSeedStream.POLICY_SAMPLING)
    assert sampling == config.derive_seed("POLICY_SAMPLING")
    assert sampling != config.derive_seed(TrainingSeedStream.EVALUATION)
    assert sampling != config.derive_seed(
        TrainingSeedStream.POLICY_SAMPLING, worker_index=1
    )
    assert sampling != config.derive_seed(
        TrainingSeedStream.POLICY_SAMPLING, env_index=1
    )
    assert len({config.derive_seed(item) for item in TRAINING_SEED_STREAM_ORDER}) == 8
    seed_schema = json.loads((
        ROOT / "config/schemas/native_training_seed_config.schema.json"
    ).read_text(encoding="utf-8"))
    assert seed_schema["additionalProperties"] is False
    assert set(seed_schema["required"]) == set(config.semantic_payload())


def test_sd7_sd9_no_python_hash_engine_rng_isolation_and_eval_split() -> None:
    source = Path(seed_module.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)
    assert not any(
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "hash"
        for node in ast.walk(tree)
    )
    engine_rng = NativeRNG(42)
    before = engine_rng.snapshot()
    config = load_training_seed_config()
    config.derive_seed(TrainingSeedStream.POLICY_SAMPLING)
    python_random_for_stream(config, TrainingSeedStream.MINIBATCH_SHUFFLE).random()
    assert engine_rng.snapshot() == before
    assert config.derive_seed(TrainingSeedStream.EVALUATION) != (
        config.derive_seed(TrainingSeedStream.POLICY_SAMPLING)
    )


def test_sd10_torch_cpu_seed_helper_is_explicit_and_reproducible(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakeGenerator:
        def __init__(self) -> None:
            self.value = random.Random(0)

        def manual_seed(self, seed: int) -> None:
            self.value = random.Random(seed)

        def draw(self) -> float:
            return self.value.random()

    generator = FakeGenerator()
    fake = SimpleNamespace(random=SimpleNamespace(default_generator=generator))
    monkeypatch.setattr(seed_module, "require_pytorch", lambda: fake)
    config = load_training_seed_config()
    first_seed = seed_torch_cpu(config, worker_index=2, env_index=3)
    first = generator.draw()
    second_seed = seed_torch_cpu(config, worker_index=2, env_index=3)
    second = generator.draw()
    assert first_seed == second_seed
    assert first == second


@pytest.mark.parametrize(
    "value",
    [True, -1, MAX_TRAINING_SEED + 1],
)
def test_sd11_invalid_root_seed_rejected(value) -> None:
    with pytest.raises((TypeError, TrainingSeedContractError)):
        build_training_seed_config(root_seed=value)


def test_sd12_canonical_seed_order_and_isolated_python_streams() -> None:
    config = load_training_seed_config()
    assert config.stream_namespaces == TRAINING_SEED_STREAM_ORDER
    reversed_payload = {
        key: config.semantic_payload()[key]
        for key in reversed(tuple(config.semantic_payload()))
    }
    assert canonical_hash(reversed_payload) == config.content_hash
    first = python_random_for_stream(config, TrainingSeedStream.POLICY_SAMPLING)
    second = python_random_for_stream(config, TrainingSeedStream.POLICY_SAMPLING)
    different = python_random_for_stream(config, TrainingSeedStream.EVALUATION)
    assert [first.random() for _ in range(4)] == [
        second.random() for _ in range(4)
    ]
    assert different.random() != python_random_for_stream(
        config, TrainingSeedStream.POLICY_SAMPLING
    ).random()


def test_existing_discount_reward_and_shaping_authorities_are_unchanged() -> None:
    discount = load_shared_training_discount_config()
    assert discount.content_hash == DISCOUNT_HASH
    assert load_reward_policy().content_hash == BASE_HASH
    assert load_eight_factor_shaping_policy(discount=discount).content_hash == (
        SHAPING_HASH
    )
