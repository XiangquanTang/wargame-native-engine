"""Small product Facade composing Data, Services, Operations and Evidence."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from wargame_native_engine.backends.native import NativeEngineBackend
from wargame_native_engine.compat.source_frame.template import (
    source_frame_template_for_scenario,
)
from wargame_native_engine.data import ScenarioBundle, ScenarioCatalog
from wargame_native_engine.gateway import CommandGateway, SessionManager
from wargame_native_engine.operations.controller import SideController
from wargame_native_engine.session.game_session import GameSession
from wargame_native_engine.session.source_environment import (
    SourceCompatibleEnvironment,
)


@dataclass(frozen=True, slots=True)
class MatchHandle:
    bundle: ScenarioBundle
    gateway: CommandGateway
    session_id: str
    red_controller_id: str
    blue_controller_id: str

    def bind_controller(
        self, side: str, *, controller_id: str
    ) -> SideController:
        normalized = str(side).lower()
        expected = {
            "red": self.red_controller_id,
            "blue": self.blue_controller_id,
        }.get(normalized)
        if expected is None:
            raise ValueError("side must be RED or BLUE")
        if controller_id != expected:
            raise PermissionError(
                f"controller {controller_id!r} is not bound to {normalized.upper()}"
            )
        return SideController(self.gateway, self.session_id, controller_id)

    @property
    def map_record(self) -> dict[str, object]:
        return dict(self.bundle.render_map_record())

    @property
    def status(self):
        """Return the detached public descriptor for this match."""

        return self.gateway.manager.descriptor(self.session_id)

    def publish(
        self,
        output_dir: str | Path,
        *,
        truncated: bool = False,
        overwrite: bool = False,
    ):
        """Publish facts and derived evidence through the managed service."""

        return self.gateway.publish_session(
            self.session_id,
            self.red_controller_id,
            str(output_dir),
            truncated=truncated,
            overwrite=overwrite,
        )

    def close(self) -> None:
        self.gateway.close_session(self.session_id)


@dataclass(frozen=True, slots=True)
class ScenarioMatchFactory:
    """Public, path-independent match construction for session adapters.

    The factory owns an immutable :class:`ScenarioBundle` reference and the
    configuration needed to create a fresh managed match for each episode.  It
    does not own an Engine or a mutable WorldState; callers close every returned
    :class:`MatchHandle` (normally through their environment/session).
    """

    bundle: ScenarioBundle
    storage_root: Path
    red_controller_id: str
    blue_controller_id: str
    decision_interval_ms: int = 20_000
    frame_interval_ms: int = 20_000
    record: bool = True
    deadline_mode: str = "synchronous"
    deadline_seconds: float | None = None
    clock: Any | None = None
    objective_capture_policy: str | Mapping[str, Any] | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.bundle, ScenarioBundle):
            raise TypeError("bundle must be a ScenarioBundle")
        object.__setattr__(self, "storage_root", Path(self.storage_root))
        for field_name in ("red_controller_id", "blue_controller_id"):
            value = getattr(self, field_name)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{field_name} must be non-empty")
        for field_name in ("decision_interval_ms", "frame_interval_ms"):
            value = getattr(self, field_name)
            if type(value) is not int or value <= 0:
                raise ValueError(f"{field_name} must be a positive int")
        if type(self.record) is not bool:
            raise TypeError("record must be bool")

    @classmethod
    def from_path(
        cls,
        scenario_path: str | Path,
        *,
        storage_root: str | Path,
        red_controller_id: str,
        blue_controller_id: str,
        map_runtime_data: str | Path | None = None,
        decision_interval_ms: int = 20_000,
        frame_interval_ms: int = 20_000,
        record: bool = True,
        deadline_mode: str = "synchronous",
        deadline_seconds: float | None = None,
        clock: Any | None = None,
        objective_capture_policy: str | Mapping[str, Any] | None = None,
    ) -> "ScenarioMatchFactory":
        """Load one explicit public scenario and optional runtime-map asset."""

        bundle = ScenarioBundle.from_path(scenario_path)
        if map_runtime_data is not None:
            bundle = bundle.with_map_runtime_data(map_runtime_data)
        return cls(
            bundle=bundle,
            storage_root=Path(storage_root),
            red_controller_id=red_controller_id,
            blue_controller_id=blue_controller_id,
            decision_interval_ms=decision_interval_ms,
            frame_interval_ms=frame_interval_ms,
            record=record,
            deadline_mode=deadline_mode,
            deadline_seconds=deadline_seconds,
            clock=clock,
            objective_capture_policy=objective_capture_policy,
        )

    def __call__(self, seed: int, episode_sequence: int) -> MatchHandle:
        if type(seed) is not int:
            raise TypeError("seed must be an exact int")
        if type(episode_sequence) is not int or episode_sequence <= 0:
            raise ValueError("episode_sequence must be a positive int")
        app = WargameFacade(
            ScenarioCatalog.from_bundles({
                self.bundle.scenario_identity.scenario_id: self.bundle,
            }),
            storage_root=self.storage_root / f"episode-{episode_sequence:06d}",
        )
        return app.create_match(
            scenario_id=self.bundle.scenario_identity.scenario_id,
            red_controller_id=self.red_controller_id,
            blue_controller_id=self.blue_controller_id,
            seed=seed,
            decision_interval_ms=self.decision_interval_ms,
            frame_interval_ms=self.frame_interval_ms,
            record=self.record,
            deadline_mode=self.deadline_mode,
            deadline_seconds=self.deadline_seconds,
            clock=self.clock,
            objective_capture_policy=self.objective_capture_policy,
        )


class WargameFacade:
    """Application root; never a second engine or advancement path."""

    def __init__(
        self,
        catalog: ScenarioCatalog,
        *,
        storage_root: str | Path,
    ) -> None:
        if not isinstance(catalog, ScenarioCatalog):
            raise TypeError("catalog must be a ScenarioCatalog")
        self.catalog = catalog
        self.storage_root = Path(storage_root)

    @classmethod
    def open(
        cls,
        data_root: str | Path,
        *,
        storage_root: str | Path,
    ) -> "WargameFacade":
        return cls(
            ScenarioCatalog.open(data_root),
            storage_root=storage_root,
        )

    def create_match(
        self,
        *,
        scenario_id: str,
        red_controller_id: str,
        blue_controller_id: str,
        seed: int = 42,
        decision_interval_ms: int = 20_000,
        frame_interval_ms: int = 20_000,
        record: bool = True,
        deadline_mode: str = "synchronous",
        deadline_seconds: float | None = None,
        clock: Any | None = None,
        objective_capture_policy: str | Mapping[str, Any] | None = None,
    ) -> MatchHandle:
        bundle = self.catalog.load_scenario(scenario_id)

        def environment_factory() -> SourceCompatibleEnvironment:
            scenario = bundle.scenario
            return SourceCompatibleEnvironment(
                GameSession(NativeEngineBackend(
                    scenario,
                    map_runtime_data=bundle.map_runtime_data,
                    objective_capture_policy=objective_capture_policy,
                )),
                template=source_frame_template_for_scenario(scenario),
            )

        manager = SessionManager(
            environment_factory,
            storage_root=self.storage_root / bundle.scenario_identity.scenario_id,
        )
        gateway = CommandGateway(
            manager,
            decision_interval_ms=decision_interval_ms,
            frame_interval_ms=frame_interval_ms,
            deadline_mode=deadline_mode,
            deadline_seconds=deadline_seconds,
            clock=clock,
        )
        descriptor = gateway.create_session(
            red_controller_id=red_controller_id,
            blue_controller_id=blue_controller_id,
            seed=seed,
            record=record,
        )
        return MatchHandle(
            bundle,
            gateway,
            descriptor.session_id,
            red_controller_id,
            blue_controller_id,
        )
