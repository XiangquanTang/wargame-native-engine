"""Read-only external compact archive objective/Canvas probe; writes only evidence.

PYTHONPATH=src:. python checks/probe_replay_objectives.py REPLAY_DIR OUTPUT_DIR
The replay remains external: do not ship its evidence JSON in the source package.
"""
import hashlib
import json
from pathlib import Path
import subprocess
import sys

from wargame_native_engine.viewer.replay_view import load_replay_view


def main():
    replay_dir, output = map(Path, sys.argv[1:])
    output.mkdir(parents=True, exist_ok=True)
    archive = replay_dir / "replay.zip"
    before = hashlib.sha256(archive.read_bytes()).hexdigest()
    view = load_replay_view(replay_dir)
    transitions, previous = [], None
    selected = {0, view.frame_count - 1}
    for ordinal, raw in enumerate(view.reader.iter_raw_frames()):
        cities = raw["cities"]
        assert cities, f"No recorded objectives at ordinal {ordinal}"
        state = [(city["coord"], city["type"], city["flag"]) for city in cities]
        if state != previous:
            transitions.append({"ordinal": ordinal, "frame_index": view.frame_indices[ordinal], "cities": state})
            selected.update((max(0, ordinal - 1), ordinal))
        previous = state
    frames = []
    for ordinal in sorted(selected):
        index = view.frame_indices[ordinal]
        raw = view.raw_frame_record(index)
        for perspective in ("GREEN", "RED", "BLUE"):
            record = view.frame_record(index, perspective)
            assert record["cities"] == raw["cities"], (index, perspective)
            frames.append(record)
        assert view.raw_frame_record(index) == raw
    fixture = output / "objective-render-input.json"
    fixture.write_text(json.dumps({"map": view.map_record(), "frames": frames}), encoding="utf-8")
    result = subprocess.run(["node", str(Path(__file__).with_name("replay_objectives_ui.cjs")), str(fixture)],
        capture_output=True, text=True, check=False, timeout=30)
    assert result.returncode == 0, result.stdout + result.stderr
    after = hashlib.sha256(archive.read_bytes()).hexdigest()
    assert before == after
    report = {"frame_count": view.frame_count, "all_raw_city_frames_checked": view.frame_count,
        "selected_ordinals": sorted(selected), "ownership_transitions": transitions,
        "perspectives": ["GREEN", "RED", "BLUE"], "projected_frames_checked": len(frames),
        "canvas": json.loads(result.stdout), "archive_sha256_before": before,
        "archive_sha256_after": after, "archive_unchanged": True}
    (output / "objective-probe.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
