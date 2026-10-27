"""Versioned deterministic fixed scaling for Decision-v1 learner tensors."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from functools import lru_cache
import json
import math
from pathlib import Path
import struct
from types import MappingProxyType
from typing import Any, Mapping, Sequence

from wargame_native_engine.io.serialization import canonical_hash, json_clone
from wargame_native_engine.training.decision_encoder import (
    ACTION_CANDIDATE_FEATURES,
    AMMUNITION_POOL_FEATURES,
    CAPABILITY_FEATURES,
    DECISION_OBSERVATION_ENCODER_SCHEMA_VERSION,
    DECISION_OBSERVATION_ENCODER_V2_SCHEMA_VERSION,
    INVENTORY_UNIT_FEATURES,
    OBJECTIVE_FEATURES,
    OBSERVED_ENEMY_FEATURES,
    OWN_OPERATOR_FEATURES,
    SNAPSHOT_FEATURES,
    DecisionEncoderConfig,
    DecisionEncoderV2Config,
    DecisionObservationEncoder,
    DecisionObservationEncoderV2,
    EncodedDecisionObservation,
    EncodedDecisionObservationV2,
    WEAPON_INVENTORY_FEATURES,
)
from wargame_native_engine.training.learner_backend import require_pytorch
from wargame_native_engine.rules.operator_lifecycle import (
    load_operator_lifecycle_authority,
)


LEARNER_OBSERVATION_PREPROCESSOR_SCHEMA_VERSION = (
    "native-ppo-observation-preprocessor/1.0"
)
LEARNER_OBSERVATION_PREPROCESSOR_ID = "native-decision-fixed-scaling"
LEARNER_OBSERVATION_PREPROCESSOR_VERSION = "1.0.0"
LEARNER_OBSERVATION_PREPROCESSOR_V2_SCHEMA_VERSION = (
    "native-ppo-observation-preprocessor/2.0"
)
LEARNER_OBSERVATION_PREPROCESSOR_V2_ID = "native-decision-weapon-aware-fixed-scaling"
LEARNER_OBSERVATION_PREPROCESSOR_V2_VERSION = "2.0.0"


class LearnerPreprocessorContractError(ValueError):
    """The learner input or fixed scaling specification is incompatible."""


class FeatureTransformType(str, Enum):
    IDENTITY = "IDENTITY"
    DIVIDE_BY_CONSTANT = "DIVIDE_BY_CONSTANT"
    LINEAR_SCALE = "LINEAR_SCALE"


@dataclass(frozen=True, slots=True)
class FeatureTransformRecord:
    group: str
    feature_name: str
    start: int
    end_exclusive: int
    stride: int
    count: int
    transform_type: FeatureTransformType
    scale: float
    offset: float
    source_authority: str
    expected_input_min: float | None = None
    expected_input_max: float | None = None

    def __post_init__(self) -> None:
        for name in ("group", "feature_name", "source_authority"):
            _nonempty(getattr(self, name), name)
        for name in ("start", "end_exclusive", "stride", "count"):
            value = getattr(self, name)
            if type(value) is not int or value < (0 if name == "start" else 1):
                raise LearnerPreprocessorContractError(
                    f"{name} has an invalid transform range"
                )
        if self.end_exclusive != self.start + (self.count - 1) * self.stride + 1:
            raise LearnerPreprocessorContractError(
                "feature transform end does not match stride/count"
            )
        if not isinstance(self.transform_type, FeatureTransformType):
            raise TypeError("transform_type must be FeatureTransformType")
        _finite(self.scale, "feature scale")
        _finite(self.offset, "feature offset")
        if self.scale <= 0.0:
            raise LearnerPreprocessorContractError("feature scale must be positive")
        if self.transform_type is FeatureTransformType.IDENTITY and (
            self.scale != 1.0 or self.offset != 0.0
        ):
            raise LearnerPreprocessorContractError(
                "IDENTITY transform requires scale=1 and offset=0"
            )
        if self.transform_type is FeatureTransformType.DIVIDE_BY_CONSTANT and (
            self.offset != 0.0
        ):
            raise LearnerPreprocessorContractError(
                "DIVIDE_BY_CONSTANT requires offset=0"
            )
        if self.expected_input_min is not None:
            _finite(self.expected_input_min, "expected input minimum")
        if self.expected_input_max is not None:
            _finite(self.expected_input_max, "expected input maximum")
        if (
            self.expected_input_min is not None
            and self.expected_input_max is not None
            and self.expected_input_max < self.expected_input_min
        ):
            raise LearnerPreprocessorContractError(
                "expected input maximum is below minimum"
            )

    @property
    def indices(self) -> tuple[int, ...]:
        return tuple(self.start + index * self.stride for index in range(self.count))

    def apply(self, value: float) -> float:
        input_value = _finite(value, f"{self.group}.{self.feature_name}")
        if self.expected_input_min is not None and (
            input_value < self.expected_input_min
        ):
            raise LearnerPreprocessorContractError(
                f"{self.group}.{self.feature_name} is below its formal bound"
            )
        if self.expected_input_max is not None and (
            input_value > self.expected_input_max
        ):
            raise LearnerPreprocessorContractError(
                f"{self.group}.{self.feature_name} exceeds its formal bound"
            )
        if self.transform_type is FeatureTransformType.IDENTITY:
            result = input_value
        elif self.transform_type is FeatureTransformType.DIVIDE_BY_CONSTANT:
            result = input_value / self.scale
        else:
            result = input_value * self.scale + self.offset
        return _float32(result, f"{self.group}.{self.feature_name} output")

    def to_dict(self) -> dict[str, object]:
        return {
            "group": self.group,
            "feature_name": self.feature_name,
            "start": self.start,
            "end_exclusive": self.end_exclusive,
            "stride": self.stride,
            "count": self.count,
            "transform_type": self.transform_type.value,
            "scale": self.scale,
            "offset": self.offset,
            "source_authority": self.source_authority,
            "expected_input_bound": (
                None
                if self.expected_input_min is None
                and self.expected_input_max is None
                else [self.expected_input_min, self.expected_input_max]
            ),
        }


@dataclass(frozen=True, slots=True)
class LearnerObservationPreprocessorSpec:
    schema_version: str
    preprocessor_id: str
    preprocessor_version: str
    strategy: str
    input_encoder_schema: str
    input_encoder_hash: str
    input_shape: tuple[int, ...]
    input_dtype: str
    output_shape: tuple[int, ...]
    output_dtype: str
    default_device: str
    feature_transforms: tuple[FeatureTransformRecord, ...]
    source_config: Mapping[str, object]
    semantic_hash: str

    def __post_init__(self) -> None:
        identity = (
            self.schema_version, self.preprocessor_id, self.preprocessor_version
        )
        if identity not in {
            (
                LEARNER_OBSERVATION_PREPROCESSOR_SCHEMA_VERSION,
                LEARNER_OBSERVATION_PREPROCESSOR_ID,
                LEARNER_OBSERVATION_PREPROCESSOR_VERSION,
            ),
            (
                LEARNER_OBSERVATION_PREPROCESSOR_V2_SCHEMA_VERSION,
                LEARNER_OBSERVATION_PREPROCESSOR_V2_ID,
                LEARNER_OBSERVATION_PREPROCESSOR_V2_VERSION,
            ),
        }:
            raise LearnerPreprocessorContractError(
                "unsupported learner observation preprocessor schema"
            )
        if self.strategy != "DETERMINISTIC_FIXED_SCALING":
            raise LearnerPreprocessorContractError(
                "preprocessor v1 requires deterministic fixed scaling"
            )
        expected_encoder_schema = (
            DECISION_OBSERVATION_ENCODER_SCHEMA_VERSION
            if identity[0] == LEARNER_OBSERVATION_PREPROCESSOR_SCHEMA_VERSION
            else DECISION_OBSERVATION_ENCODER_V2_SCHEMA_VERSION
        )
        if self.input_encoder_schema != expected_encoder_schema:
            raise LearnerPreprocessorContractError("input encoder schema mismatch")
        _sha256(self.input_encoder_hash, "input encoder hash")
        if (
            len(self.input_shape) != 1
            or self.input_shape[0] <= 0
            or self.output_shape != self.input_shape
            or (
                identity[0] == LEARNER_OBSERVATION_PREPROCESSOR_SCHEMA_VERSION
                and self.input_shape != DecisionObservationEncoder().observation_space.flat_shape
            )
        ):
            raise LearnerPreprocessorContractError(
                "preprocessor requires an unchanged supported flat shape"
            )
        if self.input_dtype != "float64" or self.output_dtype != "float32":
            raise LearnerPreprocessorContractError(
                "preprocessor dtype boundary must be float64 to float32"
            )
        if self.default_device != "cpu":
            raise LearnerPreprocessorContractError(
                "preprocessor output default device must be cpu"
            )
        if tuple(sorted(
            self.feature_transforms,
            key=lambda item: (item.start, item.stride, item.feature_name),
        )) != self.feature_transforms:
            raise LearnerPreprocessorContractError(
                "feature transforms must have canonical index order"
            )
        covered = [index for item in self.feature_transforms for index in item.indices]
        if sorted(covered) != list(range(self.input_shape[0])) or (
            len(covered) != len(set(covered))
        ):
            raise LearnerPreprocessorContractError(
                "feature transforms must cover each input index exactly once"
            )
        object.__setattr__(
            self,
            "source_config",
            _deep_freeze(json_clone(self.source_config)),
        )
        expected = canonical_hash(self.semantic_payload())
        if self.semantic_hash != expected:
            raise LearnerPreprocessorContractError(
                "preprocessor semantic hash mismatch"
            )

    def semantic_payload(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "preprocessor_id": self.preprocessor_id,
            "preprocessor_version": self.preprocessor_version,
            "strategy": self.strategy,
            "input_encoder": {
                "schema_version": self.input_encoder_schema,
                "contract_hash": self.input_encoder_hash,
                "shape": list(self.input_shape),
                "dtype": self.input_dtype,
            },
            "output": {
                "shape": list(self.output_shape),
                "dtype": self.output_dtype,
                "default_device": self.default_device,
            },
            "feature_transforms": [
                item.to_dict() for item in self.feature_transforms
            ],
            "source_config": json_clone(self.source_config),
        }

    def to_dict(self) -> dict[str, object]:
        result = self.semantic_payload()
        result["semantic_hash"] = self.semantic_hash
        return result


@dataclass(frozen=True, slots=True)
class ProcessedLearnerObservation:
    side: str
    decision_window_index: int
    frame_index: int
    sim_time_ms: int
    input_encoder_hash: str
    preprocessor_hash: str
    shape: tuple[int, ...]
    dtype: str
    values: tuple[float, ...]
    learner_tensor_hash: str

    def __post_init__(self) -> None:
        if self.side not in {"red", "blue"}:
            raise LearnerPreprocessorContractError("processed side is invalid")
        for name in ("decision_window_index", "frame_index", "sim_time_ms"):
            if type(getattr(self, name)) is not int or getattr(self, name) < 0:
                raise LearnerPreprocessorContractError(
                    f"processed {name} must be a non-negative exact int"
                )
        _sha256(self.input_encoder_hash, "processed input encoder hash")
        _sha256(self.preprocessor_hash, "processed preprocessor hash")
        if len(self.shape) != 1 or self.shape[0] <= 0 or self.dtype != "float32":
            raise LearnerPreprocessorContractError(
                "processed observation shape/dtype mismatch"
            )
        if len(self.values) != self.shape[0] or any(
            not math.isfinite(value) or _float32(value, "processed value") != value
            for value in self.values
        ):
            raise LearnerPreprocessorContractError(
                "processed observation is not finite float32 data"
            )
        _sha256(self.learner_tensor_hash, "learner tensor hash")
        if self.learner_tensor_hash != canonical_hash(list(self.values)):
            raise LearnerPreprocessorContractError(
                "learner tensor content hash mismatch"
            )

    def to_torch_cpu(self) -> Any:
        """Cross the optional backend boundary with explicit dtype/device."""

        torch = require_pytorch()
        return torch.as_tensor(self.values, dtype=torch.float32, device="cpu")

    def to_record(self, *, include_values: bool = False) -> dict[str, object]:
        result: dict[str, object] = {
            "side": self.side,
            "decision_window_index": self.decision_window_index,
            "frame_index": self.frame_index,
            "sim_time_ms": self.sim_time_ms,
            "input_encoder_hash": self.input_encoder_hash,
            "preprocessor_hash": self.preprocessor_hash,
            "shape": list(self.shape),
            "dtype": self.dtype,
            "learner_tensor_hash": self.learner_tensor_hash,
        }
        if include_values:
            result["values"] = list(self.values)
        return result


class LearnerObservationPreprocessor:
    """Pure fixed transform from one immutable Decision-v1 encoding."""

    def __init__(
        self, spec: LearnerObservationPreprocessorSpec | None = None,
    ) -> None:
        self.spec = load_learner_observation_preprocessor_spec() if spec is None else spec
        if not isinstance(self.spec, LearnerObservationPreprocessorSpec):
            raise TypeError("spec must be LearnerObservationPreprocessorSpec")

    def preprocess(
        self, value: EncodedDecisionObservation | EncodedDecisionObservationV2,
    ) -> ProcessedLearnerObservation:
        if not isinstance(value, (
            EncodedDecisionObservation, EncodedDecisionObservationV2,
        )):
            raise TypeError("value must be a supported Decision observation")
        values = self.preprocess_values(
            value.tensor,
            encoder_schema=value.encoder_schema_version,
            encoder_hash=value.encoder_contract_hash,
        )
        return ProcessedLearnerObservation(
            side=value.side.value,
            decision_window_index=value.decision_window_index,
            frame_index=value.frame_index,
            sim_time_ms=value.sim_time_ms,
            input_encoder_hash=value.encoder_contract_hash,
            preprocessor_hash=self.spec.semantic_hash,
            shape=self.spec.output_shape,
            dtype=self.spec.output_dtype,
            values=values,
            learner_tensor_hash=canonical_hash(list(values)),
        )

    def preprocess_values(
        self,
        values: Sequence[int | float],
        *,
        encoder_schema: str,
        encoder_hash: str,
    ) -> tuple[float, ...]:
        if encoder_schema != self.spec.input_encoder_schema or (
            encoder_hash != self.spec.input_encoder_hash
        ):
            raise LearnerPreprocessorContractError(
                "Decision observation encoder identity mismatch"
            )
        if isinstance(values, (str, bytes)) or not isinstance(values, Sequence):
            raise TypeError("values must be a numerical sequence")
        if len(values) != self.spec.input_shape[0]:
            raise LearnerPreprocessorContractError(
                "Decision observation tensor shape mismatch"
            )
        source = tuple(_finite(item, "input feature") for item in values)
        result = [0.0] * len(source)
        for transform in self.spec.feature_transforms:
            for index in transform.indices:
                result[index] = transform.apply(source[index])
        if any(not math.isfinite(item) for item in result):
            raise LearnerPreprocessorContractError(
                "processed observation contains NaN or Inf"
            )
        return tuple(result)


def build_learner_observation_preprocessor_spec(
    value: Mapping[str, object],
) -> LearnerObservationPreprocessorSpec:
    document = _validate_source_config(value)
    input_encoder = _mapping(document["input_encoder"], "input_encoder")
    output = _mapping(document["output"], "output")
    common_capacities = {
        "max_own_operators": _positive_int(
            input_encoder["max_own_operators"], "max_own_operators"
        ),
        "max_observed_enemies": _positive_int(
            input_encoder["max_observed_enemies"], "max_observed_enemies"
        ),
        "max_objectives": _positive_int(
            input_encoder["max_objectives"], "max_objectives"
        ),
        "max_actions": _positive_int(input_encoder["max_actions"], "max_actions"),
    }
    if input_encoder["schema_version"] == DECISION_OBSERVATION_ENCODER_SCHEMA_VERSION:
        config: DecisionEncoderConfig | DecisionEncoderV2Config = DecisionEncoderConfig(
            **common_capacities
        )
        expected_encoder = DecisionObservationEncoder(config)
    else:
        config = DecisionEncoderV2Config(
            **common_capacities,
            max_weapon_entries=_positive_int(
                input_encoder["max_weapon_entries"], "max_weapon_entries"
            ),
            max_ammunition_pools=_positive_int(
                input_encoder["max_ammunition_pools"], "max_ammunition_pools"
            ),
        )
        expected_encoder = DecisionObservationEncoderV2(config)
    expected_shape = expected_encoder.observation_space.flat_shape
    if input_encoder["contract_hash"] != expected_encoder.contract_hash:
        raise LearnerPreprocessorContractError(
            "preprocessor config does not match runtime Decision encoder"
        )
    if tuple(input_encoder["shape"]) != expected_shape:
        raise LearnerPreprocessorContractError(
            "preprocessor config input shape does not match encoder"
        )
    transforms = _feature_transforms(document, config)
    common = {
        "schema_version": str(document["schema_version"]),
        "preprocessor_id": str(document["preprocessor_id"]),
        "preprocessor_version": str(document["preprocessor_version"]),
        "strategy": str(document["strategy"]),
        "input_encoder_schema": str(input_encoder["schema_version"]),
        "input_encoder_hash": str(input_encoder["contract_hash"]),
        "input_shape": tuple(input_encoder["shape"]),
        "input_dtype": str(input_encoder["dtype"]),
        "output_shape": tuple(output["shape"]),
        "output_dtype": str(output["dtype"]),
        "default_device": str(output["default_device"]),
        "feature_transforms": transforms,
        "source_config": document,
    }
    return LearnerObservationPreprocessorSpec(
        **common,
        semantic_hash=canonical_hash(_spec_semantic_payload(common)),
    )


@lru_cache(maxsize=4)
def load_learner_observation_preprocessor_spec(
    path: str | Path | None = None,
) -> LearnerObservationPreprocessorSpec:
    source = (
        Path(path)
        if path is not None
        else Path(__file__).resolve().parents[3]
        / "config/training/native_ppo_observation_preprocessor_v1.json"
    )
    try:
        value = json.loads(source.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise LearnerPreprocessorContractError(
            f"invalid learner preprocessor config {source}: {exc}"
        ) from exc
    return build_learner_observation_preprocessor_spec(value)


def load_learner_observation_preprocessor_spec_for_identity(
    preprocessor_id: str, preprocessor_version: str,
) -> LearnerObservationPreprocessorSpec:
    """Resolve only the two explicit checked-in preprocessing authorities."""

    if (preprocessor_id, preprocessor_version) == (
        LEARNER_OBSERVATION_PREPROCESSOR_ID,
        LEARNER_OBSERVATION_PREPROCESSOR_VERSION,
    ):
        return load_learner_observation_preprocessor_spec()
    if (preprocessor_id, preprocessor_version) == (
        LEARNER_OBSERVATION_PREPROCESSOR_V2_ID,
        LEARNER_OBSERVATION_PREPROCESSOR_V2_VERSION,
    ):
        source = (
            Path(__file__).resolve().parents[3]
            / "config/training/native_ppo_observation_preprocessor_v2.json"
        )
        return load_learner_observation_preprocessor_spec(source)
    raise LearnerPreprocessorContractError(
        "unsupported learner preprocessor identity"
    )


def _feature_transforms(
    document: Mapping[str, object],
    config: DecisionEncoderConfig | DecisionEncoderV2Config,
) -> tuple[FeatureTransformRecord, ...]:
    constants = _mapping(document["scaling_constants"], "scaling_constants")
    authorities = _mapping(document["source_authorities"], "source_authorities")
    ms = _positive_number(constants["milliseconds_per_second"], "milliseconds_per_second")
    progress = _positive_number(constants["progress_millionths"], "progress_millionths")
    limited = _positive_number(
        constants["limited_lifecycle_maximum_ms"],
        "limited_lifecycle_maximum_ms",
    )
    records: list[FeatureTransformRecord] = []
    cursor = 0

    def add_group(group: str, names: Sequence[str], count: int) -> None:
        nonlocal cursor
        width = len(names)
        for offset, name in enumerate(names):
            transform, scale, authority, lower, upper = _transform_for_feature(
                group, name, config, ms, progress, limited, authorities
            )
            start = cursor + offset
            records.append(FeatureTransformRecord(
                group=group,
                feature_name=name,
                start=start,
                end_exclusive=start + (count - 1) * width + 1,
                stride=width,
                count=count,
                transform_type=transform,
                scale=scale,
                offset=0.0,
                source_authority=authority,
                expected_input_min=lower,
                expected_input_max=upper,
            ))
        cursor += width * count

    add_group("snapshot", SNAPSHOT_FEATURES, 1)
    add_group("objectives", OBJECTIVE_FEATURES, config.max_objectives)
    add_group("own", OWN_OPERATOR_FEATURES, config.max_own_operators)
    add_group("enemy", OBSERVED_ENEMY_FEATURES, config.max_observed_enemies)
    add_group("capability", CAPABILITY_FEATURES, config.max_own_operators)
    for group, count in (
        ("objective_mask", config.max_objectives),
        ("own_operator_mask", config.max_own_operators),
        ("observed_enemy_mask", config.max_observed_enemies),
        ("capability_mask", config.max_own_operators),
    ):
        records.append(FeatureTransformRecord(
            group=group,
            feature_name="present",
            start=cursor,
            end_exclusive=cursor + count,
            stride=1,
            count=count,
            transform_type=FeatureTransformType.IDENTITY,
            scale=1.0,
            offset=0.0,
            source_authority="DECISION_ENCODER_ENTITY_MASK_CONTRACT",
            expected_input_min=0.0,
            expected_input_max=1.0,
        ))
        cursor += count
    if isinstance(config, DecisionEncoderV2Config):
        add_group("inventory_unit", INVENTORY_UNIT_FEATURES, config.max_own_operators)
        add_group("weapon_inventory", WEAPON_INVENTORY_FEATURES, config.max_weapon_entries)
        add_group("ammunition_pool", AMMUNITION_POOL_FEATURES, config.max_ammunition_pools)
        add_group("action_candidate", ACTION_CANDIDATE_FEATURES, config.max_actions)
        for group, count in (
            ("inventory_unit_mask", config.max_own_operators),
            ("weapon_inventory_mask", config.max_weapon_entries),
            ("ammunition_pool_mask", config.max_ammunition_pools),
            ("action_candidate_mask", config.max_actions),
        ):
            records.append(FeatureTransformRecord(
                group=group, feature_name="present", start=cursor,
                end_exclusive=cursor + count, stride=1, count=count,
                transform_type=FeatureTransformType.IDENTITY,
                scale=1.0, offset=0.0,
                source_authority="DECISION_ENCODER_ENTITY_MASK_CONTRACT",
                expected_input_min=0.0, expected_input_max=1.0,
            ))
            cursor += count
    return tuple(sorted(records, key=lambda item: (
        item.start, item.stride, item.feature_name
    )))


def _transform_for_feature(
    group: str,
    name: str,
    config: DecisionEncoderConfig | DecisionEncoderV2Config,
    ms: float,
    progress: float,
    limited: float,
    authorities: Mapping[str, object],
) -> tuple[FeatureTransformType, float, str, float | None, float | None]:
    if group == "snapshot" and name in {
        "objective_count", "own_operator_count", "observed_enemy_count",
        "action_count",
    }:
        capacities = {
            "objective_count": config.max_objectives,
            "own_operator_count": config.max_own_operators,
            "observed_enemy_count": config.max_observed_enemies,
            "action_count": config.max_actions,
        }
        scale = float(capacities[name])
        return (
            FeatureTransformType.DIVIDE_BY_CONSTANT,
            scale,
            "DECISION_ENCODER_CONFIG_CAPACITY",
            0.0,
            scale,
        )
    if group == "own" and name == "segment_progress_millionths":
        return (
            FeatureTransformType.DIVIDE_BY_CONSTANT,
            progress,
            str(authorities["progress_contract"]),
            0.0,
            progress,
        )
    if group == "own" and name == "limited_remaining_ms":
        authority = (
            f"{authorities['operator_lifecycle_authority_version']}:"
            f"{authorities['operator_lifecycle_authority_hash']}"
        )
        return (
            FeatureTransformType.DIVIDE_BY_CONSTANT,
            limited,
            authority,
            0.0,
            limited,
        )
    if name.endswith("_ms"):
        return (
            FeatureTransformType.DIVIDE_BY_CONSTANT,
            ms,
            str(authorities["unit_conversion"]),
            0.0,
            None,
        )
    if group == "capability" and name.endswith(".instance_count"):
        return (
            FeatureTransformType.DIVIDE_BY_CONSTANT,
            float(config.max_actions),
            "DECISION_ACTION_ENCODER_CAPACITY_UPPER_BOUND",
            0.0,
            float(config.max_actions),
        )
    return (
        FeatureTransformType.IDENTITY,
        1.0,
        "DECISION_OBSERVATION_ENCODER_CONTRACT",
        None,
        None,
    )


def _validate_source_config(value: Mapping[str, object]) -> dict[str, object]:
    if not isinstance(value, Mapping):
        raise TypeError("preprocessor config must be a Mapping")
    expected = {
        "schema_version", "preprocessor_id", "preprocessor_version", "strategy",
        "input_encoder", "output", "scaling_constants", "source_authorities",
        "policies", "clipping", "batch_statistics", "running_state",
    }
    if set(value) != expected:
        raise LearnerPreprocessorContractError(
            "preprocessor config fields are missing or unknown"
        )
    document = json_clone(value)
    schema = document["schema_version"]
    if schema not in {
        LEARNER_OBSERVATION_PREPROCESSOR_SCHEMA_VERSION,
        LEARNER_OBSERVATION_PREPROCESSOR_V2_SCHEMA_VERSION,
    }:
        raise LearnerPreprocessorContractError("unsupported preprocessor schema")
    expected_identity = (
        (LEARNER_OBSERVATION_PREPROCESSOR_ID,
         LEARNER_OBSERVATION_PREPROCESSOR_VERSION)
        if schema == LEARNER_OBSERVATION_PREPROCESSOR_SCHEMA_VERSION
        else (LEARNER_OBSERVATION_PREPROCESSOR_V2_ID,
              LEARNER_OBSERVATION_PREPROCESSOR_V2_VERSION)
    )
    if (document["preprocessor_id"], document["preprocessor_version"]) != expected_identity:
        raise LearnerPreprocessorContractError("unsupported preprocessor identity")
    if document["strategy"] != "DETERMINISTIC_FIXED_SCALING":
        raise LearnerPreprocessorContractError("unsupported scaling strategy")
    if document["clipping"] != "NONE" or document["batch_statistics"] != "NONE" or (
        document["running_state"] != "NONE"
    ):
        raise LearnerPreprocessorContractError(
            "preprocessor forbids clipping, batch statistics and running state"
        )
    input_encoder = _mapping(document["input_encoder"], "input_encoder")
    expected_input_fields = {
        "schema_version", "contract_hash", "shape", "dtype",
        "max_objectives", "max_own_operators", "max_observed_enemies",
        "max_actions",
    }
    if schema == LEARNER_OBSERVATION_PREPROCESSOR_V2_SCHEMA_VERSION:
        expected_input_fields |= {"max_weapon_entries", "max_ammunition_pools"}
    if set(input_encoder) != expected_input_fields:
        raise LearnerPreprocessorContractError(
            "input_encoder fields are missing or unknown"
        )
    expected_encoder_schema = (
        DECISION_OBSERVATION_ENCODER_SCHEMA_VERSION
        if schema == LEARNER_OBSERVATION_PREPROCESSOR_SCHEMA_VERSION
        else DECISION_OBSERVATION_ENCODER_V2_SCHEMA_VERSION
    )
    if input_encoder["schema_version"] != expected_encoder_schema:
        raise LearnerPreprocessorContractError("preprocessor encoder schema mismatch")
    output = _mapping(document["output"], "output")
    if set(output) != {"shape", "dtype", "default_device"}:
        raise LearnerPreprocessorContractError(
            "preprocessor output fields are missing or unknown"
        )
    constants = _mapping(document["scaling_constants"], "scaling_constants")
    if set(constants) != {
        "milliseconds_per_second", "progress_millionths",
        "limited_lifecycle_maximum_ms",
    }:
        raise LearnerPreprocessorContractError(
            "scaling constants are missing or unknown"
        )
    authorities = _mapping(document["source_authorities"], "source_authorities")
    if set(authorities) != {
        "operator_lifecycle_authority_version",
        "operator_lifecycle_authority_hash",
        "unit_conversion",
        "progress_contract",
    }:
        raise LearnerPreprocessorContractError(
            "source authority fields are missing or unknown"
        )
    policies = _mapping(document["policies"], "policies")
    if set(policies) != {
        "bounded_ratios", "enum_status_codes", "milliseconds",
        "limited_lifecycle", "progress_millionths", "formal_counts",
        "positions", "absolute_health", "entity_masks", "padding",
    }:
        raise LearnerPreprocessorContractError(
            "preprocessor policies are missing or unknown"
        )
    expected_policies = {
        "bounded_ratios": "IDENTITY",
        "enum_status_codes": "IDENTITY",
        "milliseconds": "DIVIDE_BY_1000_EXCEPT_LIMITED_LIFECYCLE",
        "limited_lifecycle": "DIVIDE_BY_AUTHORITY_MAXIMUM",
        "progress_millionths": "DIVIDE_BY_1000000",
        "formal_counts": "DIVIDE_BY_ENCODER_CAPACITY",
        "positions": "IDENTITY_NO_GLOBAL_BOUND_AUTHORITY",
        "absolute_health": "IDENTITY",
        "entity_masks": "IDENTITY_EXACT_ZERO_ONE",
        "padding": "PRESERVE_ZERO",
    }
    if dict(policies) != expected_policies:
        raise LearnerPreprocessorContractError(
            "preprocessor policy semantics mismatch"
        )
    if dict(constants) != {
        "milliseconds_per_second": 1000.0,
        "progress_millionths": 1000000.0,
        "limited_lifecycle_maximum_ms": 1200000.0,
    }:
        raise LearnerPreprocessorContractError(
            "preprocessor scaling constants mismatch"
        )
    if authorities.get("unit_conversion") != "SI_MILLISECONDS_TO_SECONDS_EXACT" or (
        authorities.get("progress_contract") != "NATIVE_PROGRESS_MILLIONTHS/1.0"
    ):
        raise LearnerPreprocessorContractError(
            "preprocessor unit/progress authority mismatch"
        )
    lifecycle_hash = authorities.get("operator_lifecycle_authority_hash")
    _sha256(lifecycle_hash, "operator lifecycle authority hash")
    lifecycle = load_operator_lifecycle_authority()
    if authorities["operator_lifecycle_authority_version"] != (
        lifecycle.authority_version
    ) or lifecycle_hash != lifecycle.content_hash:
        raise LearnerPreprocessorContractError(
            "operator lifecycle scaling authority mismatch"
        )
    maxima = {item.maximum_lifetime_ms for item in lifecycle.profiles}
    if maxima != {int(constants["limited_lifecycle_maximum_ms"])}:
        raise LearnerPreprocessorContractError(
            "limited lifecycle scale does not match its authority"
        )
    return document


def _spec_semantic_payload(common: Mapping[str, object]) -> dict[str, object]:
    return {
        "schema_version": common["schema_version"],
        "preprocessor_id": common["preprocessor_id"],
        "preprocessor_version": common["preprocessor_version"],
        "strategy": common["strategy"],
        "input_encoder": {
            "schema_version": common["input_encoder_schema"],
            "contract_hash": common["input_encoder_hash"],
            "shape": list(common["input_shape"]),
            "dtype": common["input_dtype"],
        },
        "output": {
            "shape": list(common["output_shape"]),
            "dtype": common["output_dtype"],
            "default_device": common["default_device"],
        },
        "feature_transforms": [
            item.to_dict() for item in common["feature_transforms"]
        ],
        "source_config": json_clone(common["source_config"]),
    }


def _float32(value: object, name: str) -> float:
    result = _finite(value, name)
    try:
        packed = struct.pack(">f", result)
    except (OverflowError, struct.error) as exc:
        raise LearnerPreprocessorContractError(f"{name} overflows float32") from exc
    converted = struct.unpack(">f", packed)[0]
    if not math.isfinite(converted):
        raise LearnerPreprocessorContractError(f"{name} is not finite float32")
    return converted


def _finite(value: object, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TypeError(f"{name} must be a finite number (not bool)")
    result = float(value)
    if not math.isfinite(result):
        raise LearnerPreprocessorContractError(f"{name} must be finite")
    return result


def _mapping(value: object, name: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise LearnerPreprocessorContractError(f"{name} must be an object")
    return value


def _deep_freeze(value: Any) -> Any:
    if isinstance(value, Mapping):
        return MappingProxyType({
            str(key): _deep_freeze(item) for key, item in value.items()
        })
    if isinstance(value, (tuple, list)):
        return tuple(_deep_freeze(item) for item in value)
    return value


def _positive_int(value: object, name: str) -> int:
    if type(value) is not int or value <= 0:
        raise LearnerPreprocessorContractError(
            f"{name} must be a positive exact int"
        )
    return value


def _positive_number(value: object, name: str) -> float:
    result = _finite(value, name)
    if result <= 0.0:
        raise LearnerPreprocessorContractError(f"{name} must be positive")
    return result


def _nonempty(value: object, name: str) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise LearnerPreprocessorContractError(
            f"{name} must be non-empty trimmed text"
        )
    return value


def _sha256(value: object, name: str) -> str:
    if not isinstance(value, str) or len(value) != 64 or any(
        item not in "0123456789abcdef" for item in value
    ):
        raise LearnerPreprocessorContractError(f"{name} must be lowercase SHA-256")
    return value


__all__ = [
    "LEARNER_OBSERVATION_PREPROCESSOR_ID",
    "LEARNER_OBSERVATION_PREPROCESSOR_SCHEMA_VERSION",
    "LEARNER_OBSERVATION_PREPROCESSOR_VERSION",
    "LEARNER_OBSERVATION_PREPROCESSOR_V2_ID",
    "LEARNER_OBSERVATION_PREPROCESSOR_V2_SCHEMA_VERSION",
    "LEARNER_OBSERVATION_PREPROCESSOR_V2_VERSION",
    "FeatureTransformRecord",
    "FeatureTransformType",
    "LearnerObservationPreprocessor",
    "LearnerObservationPreprocessorSpec",
    "LearnerPreprocessorContractError",
    "ProcessedLearnerObservation",
    "build_learner_observation_preprocessor_spec",
    "load_learner_observation_preprocessor_spec",
    "load_learner_observation_preprocessor_spec_for_identity",
]
