"""Frozen Phase-7 Base-vs-EightFactor PPO benchmark protocol."""

from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
import json
from pathlib import Path
from typing import Mapping, Sequence

from wargame_native_engine.io.scenario import load_scenario
from wargame_native_engine.io.serialization import canonical_hash
from wargame_native_engine.training.ppo_config import (
    PPOAuthorityReference,
    load_ppo_algorithm_config,
)
from wargame_native_engine.training.ppo_parallel_runner_config import (
    load_ppo_parallel_runner_config,
)
from wargame_native_engine.training.ppo_runner_config import PPORewardMode
from wargame_native_engine.training.training_seed import (
    TrainingSeedStream,
    load_training_seed_config,
)


PPO_TRAINING_BENCHMARK_SCHEMA_VERSION = "native-ppo-training-benchmark/1.0"
PPO_TRAINING_BENCHMARK_ID = "native-base-vs-eight-factor-ppo-pilot"
PPO_TRAINING_BENCHMARK_VERSION = "1.0.0"
PPO_TRAINING_BENCHMARK_FULL_ID = "native-base-vs-eight-factor-ppo-full"
PPO_TRAINING_BENCHMARK_FULL_VERSION = "1.0.1"
BENCHMARK_GREEN_TEMPLATE_POLICY_SCHEMA = "native-benchmark-green-template-policy/1.0"
BENCHMARK_GREEN_TEMPLATE_POLICY_ID = "explicit-complete-eight-factor-supervision"
BENCHMARK_GREEN_TEMPLATE_POLICY_VERSION = "1.0.0"

BENCHMARK_GREEN_UNIT_DEFAULTS: Mapping[str, object] = {
    "can_to_move": True,
    "keep": False,
    "lose_control": False,
    "see_enemy_bop_ids": [],
    "tire": 0,
    "value": 100.0,
}


class PPOTrainingBenchmarkConfigError(ValueError):
    """The research protocol is incomplete, drifted, or confounded."""


def benchmark_green_template_policy_payload() -> dict[str, object]:
    return {
        "schema_version": BENCHMARK_GREEN_TEMPLATE_POLICY_SCHEMA,
        "policy_id": BENCHMARK_GREEN_TEMPLATE_POLICY_ID,
        "policy_version": BENCHMARK_GREEN_TEMPLATE_POLICY_VERSION,
        "source": "EXPLICIT_BENCHMARK_SUPERVISION",
        "unit_defaults": dict(BENCHMARK_GREEN_UNIT_DEFAULTS),
        "future_information": "FORBIDDEN",
        "policy_observation": "EXCLUDED",
        "missing_field_policy": "FAIL_CLOSED",
    }


BENCHMARK_GREEN_TEMPLATE_POLICY_HASH = canonical_hash(
    benchmark_green_template_policy_payload()
)


@dataclass(frozen=True, slots=True)
class PPOBenchmarkScenario:
    scenario_id: str
    path: str
    scenario_hash: str
    map_id: str
    max_ticks: int
    delta_time_seconds: int

    def to_dict(self) -> dict[str, object]:
        return {
            "scenario_id": self.scenario_id,
            "path": self.path,
            "scenario_hash": self.scenario_hash,
            "map_id": self.map_id,
            "max_ticks": self.max_ticks,
            "delta_time_seconds": self.delta_time_seconds,
        }


@dataclass(frozen=True, slots=True)
class PPOBenchmarkTreatment:
    treatment_id: str
    reward_mode: PPORewardMode
    reward: PPOAuthorityReference
    shaping: PPOAuthorityReference | None

    def to_dict(self) -> dict[str, object]:
        return {
            "treatment_id": self.treatment_id,
            "reward_mode": self.reward_mode.value,
            "reward_ref": self.reward.to_dict(),
            "shaping_ref": None if self.shaping is None else self.shaping.to_dict(),
        }


@dataclass(frozen=True, slots=True)
class PPOBenchmarkRunSpec:
    treatment_id: str
    scenario_id: str
    scenario_hash: str
    train_seed_index: int
    trained_side: str
    run_id: str

    def to_dict(self) -> dict[str, object]:
        return {
            "treatment_id": self.treatment_id,
            "scenario_id": self.scenario_id,
            "scenario_hash": self.scenario_hash,
            "train_seed_index": self.train_seed_index,
            "trained_side": self.trained_side,
            "run_id": self.run_id,
        }


@dataclass(frozen=True, slots=True)
class PPOTrainingBenchmarkConfig:
    benchmark_id: str
    benchmark_version: str
    stage: str
    scenarios: tuple[PPOBenchmarkScenario, ...]
    treatments: tuple[PPOBenchmarkTreatment, ...]
    train_seed_indices: tuple[int, ...]
    trained_sides: tuple[str, ...]
    evaluation_episode_indices: tuple[int, ...]
    evaluation_episodes_per_checkpoint: int
    evaluation_cadence_transitions: tuple[int, ...]
    transitions_per_run: int
    checkpoint_cadence_transitions: int
    num_envs: int
    num_workers: int
    opponent: PPOAuthorityReference
    ppo: PPOAuthorityReference
    parallel_runner: PPOAuthorityReference
    seed: PPOAuthorityReference
    green_template_policy_hash: str
    checkpoint_selection: str
    run_scheduling: str
    reward_replay_verification: str
    engine_replay_verification: str
    episode_artifact_retention: str
    bootstrap_resamples: int
    bootstrap_seed: int
    content_hash: str

    @property
    def schema_version(self) -> str:
        return PPO_TRAINING_BENCHMARK_SCHEMA_VERSION

    @property
    def eval_seed_set_hash(self) -> str:
        seed = load_training_seed_config()
        values = [
            {
                "env_index": env_index,
                "episode_index": episode_index,
                "seed": seed.derive_seed(
                    TrainingSeedStream.EVALUATION,
                    env_index=env_index,
                    episode_index=episode_index,
                ),
            }
            for env_index, episode_index in self.evaluation_episode_coordinates
        ]
        return canonical_hash(values)

    @property
    def evaluation_episode_coordinates(self) -> tuple[tuple[int, int], ...]:
        return tuple(
            (
                ordinal % self.num_envs,
                self.evaluation_episode_indices[ordinal // self.num_envs],
            )
            for ordinal in range(self.evaluation_episodes_per_checkpoint)
        )

    @property
    def planned_transitions(self) -> int:
        return len(self.run_matrix()) * self.transitions_per_run

    def run_matrix(self) -> tuple[PPOBenchmarkRunSpec, ...]:
        result = []
        for scenario in self.scenarios:
            for side in self.trained_sides:
                for train_seed_index in self.train_seed_indices:
                    for treatment in self.treatments:
                        identity = {
                            "benchmark_hash": self.content_hash,
                            "treatment": treatment.treatment_id,
                            "scenario_hash": scenario.scenario_hash,
                            "train_seed_index": train_seed_index,
                            "trained_side": side,
                        }
                        result.append(PPOBenchmarkRunSpec(
                            treatment.treatment_id, scenario.scenario_id,
                            scenario.scenario_hash, train_seed_index, side,
                            canonical_hash(identity)[:20],
                        ))
        return tuple(result)

    def semantic_payload(self) -> dict[str, object]:
        payload = {
            "schema_version": self.schema_version,
            "benchmark_id": self.benchmark_id,
            "benchmark_version": self.benchmark_version,
            "stage": self.stage,
            "scenario_suite": [item.to_dict() for item in self.scenarios],
            "treatments": [item.to_dict() for item in self.treatments],
            "train_seed_indices": list(self.train_seed_indices),
            "trained_sides": list(self.trained_sides),
            "opponent_ref": self.opponent.to_dict(),
            "ppo_config_ref": self.ppo.to_dict(),
            "parallel_runner_ref": self.parallel_runner.to_dict(),
            "seed_config_ref": self.seed.to_dict(),
            "green_template_policy_ref": {
                "id": BENCHMARK_GREEN_TEMPLATE_POLICY_ID,
                "version": BENCHMARK_GREEN_TEMPLATE_POLICY_VERSION,
                "hash": self.green_template_policy_hash,
            },
            "training_budget": {
                "unit": "COMMITTED_ENVIRONMENT_TRANSITIONS",
                "transitions_per_run": self.transitions_per_run,
            },
            "evaluation": {
                "namespace": "EVALUATION",
                "episode_indices": list(self.evaluation_episode_indices),
                "episodes_per_checkpoint": self.evaluation_episodes_per_checkpoint,
                "cadence_transitions": list(self.evaluation_cadence_transitions),
                "action_selection": "MASKED_ARGMAX",
                "primary_metric": "WIN_RATE_ENGINE_OUTCOME",
                "eval_seed_set_hash": self.eval_seed_set_hash,
            },
            "checkpoint": {
                "cadence_transitions": self.checkpoint_cadence_transitions,
                "primary_selection": self.checkpoint_selection,
            },
            "aggregation": {
                "statistical_unit": "TRAINING_SEED_RUN",
                "paired_treatments": True,
                "scenario_macro_average": "EQUAL_WEIGHT",
                "auc": "NORMALIZED_TRAPEZOIDAL_WIN_RATE_VS_TRANSITIONS",
                "raw_evaluation_points": "RETAIN",
            },
            "replay_verification": {
                "reward": self.reward_replay_verification,
                "engine": self.engine_replay_verification,
            },
            "execution": {
                "run_scheduling": self.run_scheduling,
                "num_envs": self.num_envs,
                "num_workers": self.num_workers,
            },
        }
        if self.stage == "FULL":
            payload["full_analysis"] = {
                "bootstrap_resamples": self.bootstrap_resamples,
                "bootstrap_seed": self.bootstrap_seed,
                "paired_ci": "PERCENTILE_95",
                "thresholds": [0.5, 0.75],
                "variability_band": "MEAN_PLUS_MINUS_STD",
                "runner_boundary_lifecycle": "AUTO_RESET_INACTIVE_AT_ROLLOUT_START_V1",
            }
            payload["artifact_retention"] = {
                "episode_packages": self.episode_artifact_retention,
                "checkpoints": "FINAL_ONLY_EVALUATION_RESULTS_RETAINED",
            }
            payload["training_budget"]["partial_rollout_policy"] = (
                "EXACT_EVALUATION_AND_FINAL_BOUNDARIES"
            )
        return payload

    def to_dict(self) -> dict[str, object]:
        value = self.semantic_payload()
        value["benchmark_hash"] = self.content_hash
        return value


def normalized_trapezoidal_auc(points: Sequence[tuple[int, float]]) -> float:
    if not points:
        raise PPOTrainingBenchmarkConfigError("AUC requires at least one point")
    ordered = sorted(points)
    if list(points) != ordered or len({x for x, _ in ordered}) != len(ordered):
        raise PPOTrainingBenchmarkConfigError("AUC grid must be unique and ordered")
    if any(type(x) is not int or x < 0 or not 0.0 <= y <= 1.0 for x, y in ordered):
        raise PPOTrainingBenchmarkConfigError("AUC point is outside its contract")
    if len(ordered) == 1 or ordered[-1][0] == ordered[0][0]:
        return float(ordered[0][1])
    area = sum(
        (right_x - left_x) * (left_y + right_y) / 2.0
        for (left_x, left_y), (right_x, right_y) in zip(ordered, ordered[1:])
    )
    return area / (ordered[-1][0] - ordered[0][0])


def build_ppo_training_benchmark_config(
    value: Mapping[str, object],
) -> PPOTrainingBenchmarkConfig:
    expected = {
        "schema_version", "benchmark_id", "benchmark_version", "stage",
        "scenario_suite", "treatments", "train_seed_indices", "trained_sides",
        "opponent_ref", "ppo_config_ref", "parallel_runner_ref", "seed_config_ref",
        "green_template_policy_ref", "training_budget", "evaluation", "checkpoint",
        "replay_verification", "execution",
    }
    stage = value.get("stage") if isinstance(value, Mapping) else None
    if stage == "FULL":
        expected |= {"full_analysis", "artifact_retention"}
    if not isinstance(value, Mapping) or set(value) != expected:
        raise PPOTrainingBenchmarkConfigError("benchmark fields are missing or unknown")
    identity = (value["benchmark_id"], value["benchmark_version"])
    expected_identity = (
        (PPO_TRAINING_BENCHMARK_ID, PPO_TRAINING_BENCHMARK_VERSION)
        if stage == "PILOT" else
        (PPO_TRAINING_BENCHMARK_FULL_ID, PPO_TRAINING_BENCHMARK_FULL_VERSION)
    )
    if value["schema_version"] != PPO_TRAINING_BENCHMARK_SCHEMA_VERSION or identity != expected_identity:
        raise PPOTrainingBenchmarkConfigError("unsupported benchmark identity")
    if stage not in {"PILOT", "FULL"}:
        raise PPOTrainingBenchmarkConfigError("v1 config must be explicit PILOT or FULL")

    scenarios = tuple(_scenario(item) for item in _sequence(value["scenario_suite"]))
    if len(scenarios) != 2 or len({item.scenario_hash for item in scenarios}) != 2:
        raise PPOTrainingBenchmarkConfigError("pilot requires exactly two distinct scenarios")
    treatments = tuple(_treatment(item) for item in _sequence(value["treatments"]))
    if tuple(item.treatment_id for item in treatments) != (
        "BASE_ONLY", "EIGHT_FACTOR_SHAPING"
    ):
        raise PPOTrainingBenchmarkConfigError("pilot treatment order/arms are frozen")
    if treatments[0].reward_mode is not PPORewardMode.BASE_ONLY or treatments[0].shaping:
        raise PPOTrainingBenchmarkConfigError("Base treatment is confounded")
    if treatments[1].reward_mode is not PPORewardMode.COMPOSITE_EIGHT_FACTOR or not treatments[1].shaping:
        raise PPOTrainingBenchmarkConfigError("shaping treatment is incomplete")

    ppo = load_ppo_algorithm_config()
    parallel = load_ppo_parallel_runner_config()
    seed = load_training_seed_config()
    ppo_ref = _ref(value["ppo_config_ref"])
    parallel_ref = _ref(value["parallel_runner_ref"])
    seed_ref = _ref(value["seed_config_ref"])
    if ppo_ref != PPOAuthorityReference(ppo.algorithm_id, ppo.algorithm_version, ppo.content_hash):
        raise PPOTrainingBenchmarkConfigError("PPO authority drift")
    if parallel_ref != PPOAuthorityReference(parallel.runner_id, parallel.runner_version, parallel.content_hash):
        raise PPOTrainingBenchmarkConfigError("parallel runner authority drift")
    if seed_ref != PPOAuthorityReference(seed.config_id, seed.config_version, seed.content_hash):
        raise PPOTrainingBenchmarkConfigError("seed authority drift")
    green = _mapping(value["green_template_policy_ref"])
    if green != {
        "id": BENCHMARK_GREEN_TEMPLATE_POLICY_ID,
        "version": BENCHMARK_GREEN_TEMPLATE_POLICY_VERSION,
        "hash": BENCHMARK_GREEN_TEMPLATE_POLICY_HASH,
    }:
        raise PPOTrainingBenchmarkConfigError("benchmark GREEN template authority drift")

    budget = _mapping(value["training_budget"])
    evaluation = _mapping(value["evaluation"])
    checkpoint = _mapping(value["checkpoint"])
    replay = _mapping(value["replay_verification"])
    execution = _mapping(value["execution"])
    full_analysis = _mapping(value["full_analysis"]) if stage == "FULL" else {}
    retention = _mapping(value["artifact_retention"]) if stage == "FULL" else {}
    if set(budget) != {"unit", "transitions_per_run"} or budget["unit"] != "COMMITTED_ENVIRONMENT_TRANSITIONS":
        raise PPOTrainingBenchmarkConfigError("training budget authority is invalid")
    if set(evaluation) != {"namespace", "episode_indices", "episodes_per_checkpoint", "cadence_transitions", "action_selection", "primary_metric"}:
        raise PPOTrainingBenchmarkConfigError("evaluation fields are invalid")
    if (evaluation["namespace"], evaluation["action_selection"], evaluation["primary_metric"]) != (
        "EVALUATION", "MASKED_ARGMAX", "WIN_RATE_ENGINE_OUTCOME"
    ):
        raise PPOTrainingBenchmarkConfigError("evaluation semantics drifted")
    if set(checkpoint) != {"cadence_transitions", "primary_selection"} or checkpoint["primary_selection"] != "LAST_CHECKPOINT_ONLY":
        raise PPOTrainingBenchmarkConfigError("checkpoint selection is invalid")
    if set(replay) != {"reward", "engine"}:
        raise PPOTrainingBenchmarkConfigError("replay policy fields are invalid")
    if set(execution) != {"run_scheduling", "num_envs", "num_workers"}:
        raise PPOTrainingBenchmarkConfigError("execution fields are invalid")

    common = dict(
        benchmark_id=str(value["benchmark_id"]), benchmark_version=str(value["benchmark_version"]),
        stage=str(value["stage"]), scenarios=scenarios, treatments=treatments,
        train_seed_indices=_indices(value["train_seed_indices"], "train_seed_indices"),
        trained_sides=tuple(str(item) for item in _sequence(value["trained_sides"])),
        evaluation_episode_indices=_indices(evaluation["episode_indices"], "evaluation episode_indices"),
        evaluation_episodes_per_checkpoint=_positive(evaluation["episodes_per_checkpoint"], "evaluation episodes"),
        evaluation_cadence_transitions=_nonnegative_indices(evaluation["cadence_transitions"], "evaluation cadence"),
        transitions_per_run=_positive(budget["transitions_per_run"], "transitions_per_run"),
        checkpoint_cadence_transitions=_positive(checkpoint["cadence_transitions"], "checkpoint cadence"),
        num_envs=_positive(execution["num_envs"], "num_envs"),
        num_workers=_positive(execution["num_workers"], "num_workers"),
        opponent=_ref(value["opponent_ref"]), ppo=ppo_ref, parallel_runner=parallel_ref,
        seed=seed_ref, green_template_policy_hash=BENCHMARK_GREEN_TEMPLATE_POLICY_HASH,
        checkpoint_selection=str(checkpoint["primary_selection"]),
        run_scheduling=str(execution["run_scheduling"]),
        reward_replay_verification=str(replay["reward"]),
        engine_replay_verification=str(replay["engine"]),
        episode_artifact_retention=(
            str(retention["episode_packages"])
            if stage == "FULL" else "ALL_COMPLETED_EPISODES"
        ),
        bootstrap_resamples=(
            _positive(full_analysis["bootstrap_resamples"], "bootstrap resamples")
            if stage == "FULL" else 0
        ),
        bootstrap_seed=(
            _positive(full_analysis["bootstrap_seed"], "bootstrap seed")
            if stage == "FULL" else 0
        ),
    )
    if common["trained_sides"] != ("RED",) or common["run_scheduling"] != "RUNS_SEQUENTIAL":
        raise PPOTrainingBenchmarkConfigError("pilot side/scheduling contract drifted")
    if common["num_envs"] != common["num_workers"] or common["num_envs"] != parallel.num_envs:
        raise PPOTrainingBenchmarkConfigError("pilot must reuse production worker count")
    if common["transitions_per_run"] < ppo.rollout_length:
        raise PPOTrainingBenchmarkConfigError("budget must contain at least one production PPO rollout")
    required_waves = (
        common["evaluation_episodes_per_checkpoint"] + common["num_envs"] - 1
    ) // common["num_envs"]
    if len(common["evaluation_episode_indices"]) != required_waves:
        raise PPOTrainingBenchmarkConfigError("evaluation seed wave/count mismatch")
    if common["evaluation_cadence_transitions"][0] != 0 or common["evaluation_cadence_transitions"][-1] != common["transitions_per_run"]:
        raise PPOTrainingBenchmarkConfigError("evaluation must include transition 0 and final")
    if stage == "PILOT" and common["checkpoint_cadence_transitions"] != ppo.rollout_length:
        raise PPOTrainingBenchmarkConfigError("pilot checkpoint cadence must be an update boundary")
    if stage == "FULL" and common["checkpoint_cadence_transitions"] != 10_000:
        raise PPOTrainingBenchmarkConfigError("FULL checkpoint cadence must match evaluation grid")
    if (common["reward_replay_verification"], common["engine_replay_verification"]) != (
        "EVERY_COMPLETED_EPISODE", "FIRST_COMPLETED_EPISODE_PER_RUN"
    ):
        raise PPOTrainingBenchmarkConfigError("pilot replay verification policy drifted")
    if stage == "PILOT":
        if common["train_seed_indices"] != (0, 1) or common["transitions_per_run"] != 2048:
            raise PPOTrainingBenchmarkConfigError("pilot seed/budget contract drifted")
    else:
        if set(full_analysis) != {"bootstrap_resamples", "bootstrap_seed"} or set(retention) != {"episode_packages"}:
            raise PPOTrainingBenchmarkConfigError("FULL analysis/retention fields are invalid")
        if common["train_seed_indices"] != (0, 1, 2, 3, 4):
            raise PPOTrainingBenchmarkConfigError("FULL requires paired seed indices 0..4")
        if common["transitions_per_run"] != 50_000 or common["evaluation_cadence_transitions"] != (
            0, 10_000, 20_000, 30_000, 40_000, 50_000
        ):
            raise PPOTrainingBenchmarkConfigError("FULL budget/evaluation grid drifted")
        if common["evaluation_episodes_per_checkpoint"] != 30:
            raise PPOTrainingBenchmarkConfigError("FULL requires 30 evaluation episodes")
        if common["episode_artifact_retention"] != "FIRST_ENGINE_REPLAY_VERIFIED_ONLY":
            raise PPOTrainingBenchmarkConfigError("FULL episode retention policy drifted")
        if common["bootstrap_resamples"] != 10_000:
            raise PPOTrainingBenchmarkConfigError("FULL bootstrap resample count drifted")
    temporary = object.__new__(PPOTrainingBenchmarkConfig)
    for name, item in common.items():
        object.__setattr__(temporary, name, item)
    object.__setattr__(temporary, "content_hash", "")
    return PPOTrainingBenchmarkConfig(
        **common, content_hash=canonical_hash(temporary.semantic_payload())
    )


@lru_cache(maxsize=4)
def load_ppo_training_benchmark_config(
    path: str | Path | None = None,
) -> PPOTrainingBenchmarkConfig:
    source = Path(path) if path is not None else Path(__file__).resolve().parents[3] / "config/benchmarks/native_ppo_training_benchmark_pilot_v1.json"
    return build_ppo_training_benchmark_config(json.loads(source.read_text(encoding="utf-8")))


def _scenario(value: object) -> PPOBenchmarkScenario:
    item = _mapping(value)
    if set(item) != {"scenario_id", "path", "scenario_hash", "map_id", "max_ticks", "delta_time_seconds"}:
        raise PPOTrainingBenchmarkConfigError("scenario fields are invalid")
    scenario = load_scenario(str(item["path"]))
    candidate = PPOBenchmarkScenario(
        str(item["scenario_id"]), str(item["path"]), str(item["scenario_hash"]),
        str(item["map_id"]), _positive(item["max_ticks"], "max_ticks"),
        _positive(item["delta_time_seconds"], "delta_time_seconds"),
    )
    if (candidate.scenario_id, candidate.scenario_hash, candidate.map_id, candidate.max_ticks) != (
        scenario.scenario_id, scenario.scenario_hash, scenario.map.map_id,
        scenario.terminal_rules.max_ticks,
    ):
        raise PPOTrainingBenchmarkConfigError(f"scenario authority drift: {candidate.path}")
    return candidate


def _treatment(value: object) -> PPOBenchmarkTreatment:
    item = _mapping(value)
    if set(item) != {"treatment_id", "reward_mode", "reward_ref", "shaping_ref"}:
        raise PPOTrainingBenchmarkConfigError("treatment fields are invalid")
    shaping = None if item["shaping_ref"] is None else _ref(item["shaping_ref"])
    return PPOBenchmarkTreatment(
        str(item["treatment_id"]), PPORewardMode(str(item["reward_mode"])),
        _ref(item["reward_ref"]), shaping,
    )


def _mapping(value: object) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise PPOTrainingBenchmarkConfigError("expected object")
    return value


def _sequence(value: object) -> Sequence[object]:
    if not isinstance(value, (list, tuple)):
        raise PPOTrainingBenchmarkConfigError("expected array")
    return value


def _ref(value: object) -> PPOAuthorityReference:
    item = _mapping(value)
    if set(item) != {"id", "version", "hash"}:
        raise PPOTrainingBenchmarkConfigError("authority reference fields are invalid")
    return PPOAuthorityReference(str(item["id"]), str(item["version"]), str(item["hash"]))


def _positive(value: object, name: str) -> int:
    if type(value) is not int or value <= 0:
        raise PPOTrainingBenchmarkConfigError(f"{name} must be positive")
    return value


def _indices(value: object, name: str) -> tuple[int, ...]:
    result = _nonnegative_indices(value, name)
    if not result:
        raise PPOTrainingBenchmarkConfigError(f"{name} must not be empty")
    return result


def _nonnegative_indices(value: object, name: str) -> tuple[int, ...]:
    sequence = tuple(_sequence(value))
    if any(type(item) is not int or item < 0 for item in sequence) or tuple(sorted(set(sequence))) != sequence:
        raise PPOTrainingBenchmarkConfigError(f"{name} must be sorted unique non-negative integers")
    return sequence  # type: ignore[return-value]


__all__ = [
    "BENCHMARK_GREEN_TEMPLATE_POLICY_HASH", "BENCHMARK_GREEN_UNIT_DEFAULTS",
    "PPOBenchmarkRunSpec", "PPOBenchmarkScenario", "PPOBenchmarkTreatment",
    "PPOTrainingBenchmarkConfig", "PPOTrainingBenchmarkConfigError",
    "PPO_TRAINING_BENCHMARK_ID", "PPO_TRAINING_BENCHMARK_SCHEMA_VERSION",
    "PPO_TRAINING_BENCHMARK_VERSION", "benchmark_green_template_policy_payload",
    "build_ppo_training_benchmark_config", "load_ppo_training_benchmark_config",
    "normalized_trapezoidal_auc",
]
