"""Small product Facade composing Data, Services, Operations and Evidence."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

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
    ) -> MatchHandle:
        bundle = self.catalog.load_scenario(scenario_id)

        def environment_factory() -> SourceCompatibleEnvironment:
            scenario = bundle.scenario
            return SourceCompatibleEnvironment(
                GameSession(NativeEngineBackend(scenario)),
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
