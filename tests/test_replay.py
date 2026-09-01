from __future__ import annotations

import json

import pytest

from wargame_native_engine.agents import FirstValidActionAgent
from wargame_native_engine.backends.native import NativeEngineBackend
from wargame_native_engine.io.replay import (
    ReplayReader,
    UnsupportedReplaySemanticsError,
    replay_actions,
)
from wargame_native_engine.io.serialization import file_sha256
from wargame_native_engine.session.episode import EpisodeRunner
from wargame_native_engine.session.game_session import GameSession


def _run(scenario, output):
    session = GameSession(NativeEngineBackend(scenario, max_ticks=50))
    agents = {side: FirstValidActionAgent() for side in scenario.side_order}
    return EpisodeRunner(session, agents=agents, output_dir=output).run(42)


def test_action_event_are_separate(scenario, tmp_path) -> None:
    output = tmp_path / "replay"
    _run(scenario, output)
    bundle = ReplayReader().read(output)
    assert bundle["actions"] and bundle["events"]
    assert {item["schema_version"] for item in bundle["actions"]} == {
        "native-action/1.0"}
    assert {item["schema_version"] for item in bundle["events"]} == {
        "native-event/1.0"}
    assert "event_type" not in bundle["actions"][0]
    assert "action" not in bundle["events"][0]


def test_replay_round_trip(scenario, tmp_path) -> None:
    output = tmp_path / "replay"
    outcome = _run(scenario, output)
    bundle = ReplayReader().read(output)
    verification = replay_actions(scenario, output)
    assert bundle["summary"]["result"] == outcome.result
    assert verification.matches
    assert verification.ticks == outcome.result["ticks"]
    assert (output / "checksums.sha256").is_file()


def test_reader_keeps_legacy_tick_named_snapshot_archives_compatible(
    scenario,
    tmp_path,
) -> None:
    output = tmp_path / "legacy-tick-snapshots"
    _run(scenario, output)
    snapshots = sorted((output / "snapshots").glob("step-*.json"))
    assert snapshots
    replacements = []
    for source in snapshots:
        snapshot = json.loads(source.read_text(encoding="utf-8"))
        target = source.with_name(f"tick-{snapshot['world']['tick']:08d}.json")
        assert not target.exists()
        source.rename(target)
        replacements.append((source, target))

    manifest_path = output / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    hashes = manifest["collection_hashes"]
    for source, target in replacements:
        hashes.pop(str(source.relative_to(output)))
        hashes[str(target.relative_to(output))] = file_sha256(target)
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )
    archive_files = sorted(
        path for path in output.rglob("*")
        if path.is_file() and path.name != "checksums.sha256"
    )
    (output / "checksums.sha256").write_text(
        "\n".join(
            f"{file_sha256(path)}  {path.relative_to(output)}"
            for path in archive_files
        ) + "\n",
        encoding="utf-8",
    )

    bundle = ReplayReader().read(output)
    assert len(bundle["snapshots"]) == len(bundle["steps"]) + 1
    assert replay_actions(scenario, output, strict=True).matches is True


def test_strict_replay_rejects_incompatible_semantics_before_engine_step(
    scenario, tmp_path, monkeypatch,
) -> None:
    output = tmp_path / "replay-semantics"
    _run(scenario, output)
    manifest_path = output / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["adjudication_semantics_version"] = "ordered-sequential/0"
    manifest_path.write_text(
        json.dumps(manifest, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )

    def constructed(*args, **kwargs):
        raise AssertionError("engine must not be constructed")

    monkeypatch.setattr("wargame_native_engine.io.replay.NativeEngine", constructed)
    with pytest.raises(UnsupportedReplaySemanticsError, match="ordered-sequential"):
        replay_actions(scenario, output, strict=True)
