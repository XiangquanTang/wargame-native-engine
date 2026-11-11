"""Pack/restore Source JSON without interpreting or normalizing business fields."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import shutil
import tempfile

from wargame_native_engine.replay.compact import (
    CompactReplayReader, CompactReplayWriter, _decode, _encode, json_equal,
    MAX_RECORD_BYTES, MAX_ASSET_BYTES,
)


def parser() -> argparse.ArgumentParser:
    value = argparse.ArgumentParser(description=__doc__)
    commands = value.add_subparsers(dest="command", required=True)
    pack = commands.add_parser("pack", help="losslessly pack numbered raw JSON frames (gaps preserved)")
    pack.add_argument("frames", nargs="+", type=Path)
    pack.add_argument("--output", required=True, type=Path)
    pack.add_argument("--checkpoint-interval", type=int, default=128)
    pack.add_argument("--assets", type=Path, help="optional map/scenario asset JSON object; never inferred")
    unpack = commands.add_parser("unpack", help="restore the original JSON trees and original filenames")
    unpack.add_argument("archive", type=Path)
    unpack.add_argument("--output", required=True, type=Path)
    check = commands.add_parser("check", help="stream-validate all frames and the external hash when available")
    check.add_argument("archive", type=Path)
    return value


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    if args.command == "pack":
        target = args.output
        if os.path.lexists(target) or os.path.lexists(str(target) + ".sha256"):
            raise FileExistsError(f"refusing to overwrite compact output: {target}")
        files = sorted(args.frames, key=lambda path: int(path.stem))
        if any(not path.is_file() or path.suffix != ".json" or not path.stem.isdecimal() for path in files):
            raise ValueError("input frames must be numbered .json files")
        target.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix=".compact-pack-", dir=target.parent) as temporary:
            writer = CompactReplayWriter(Path(temporary) / "recording", checkpoint_interval=args.checkpoint_interval)
            assets = {} if args.assets is None else _decode(args.assets.read_bytes(), MAX_ASSET_BYTES)
            writer.start({"source": "raw-json-import", "missing_frames": "not-synthesized"}, assets=assets)
            for path in files:
                if path.stat().st_size > MAX_RECORD_BYTES:
                    raise ValueError("input frame exceeds size limit")
                frame = _decode(path.read_bytes())
                if not isinstance(frame, dict):
                    raise ValueError("Source frame root must be an object")
                writer.append(frame, name=path.name)
            writer.finalize()
            staged = writer.package_zip(Path(temporary) / target.name)
            reader = CompactReplayReader(staged)
            for path, (name, frame, _) in zip(files, reader.iter_records()):
                if path.name != name or not json_equal(_decode(path.read_bytes()), frame):
                    raise ValueError("compact roundtrip mismatch")
            # Archive and hash publish together via staged directory when using
            # live publication. CLI flat-file publication is no-overwrite and
            # cleans up only this invocation's newly-created file on failure.
            os.link(staged, target)
            try:
                os.link(str(staged) + ".sha256", str(target) + ".sha256")
            except BaseException:
                target.unlink()
                raise
        print(json.dumps({"replay_format": reader.manifest["replay_format"], "frames": reader.frame_count,
                          "frame_names": reader.frame_names, "roundtrip": "exact-json-tree",
                          "output": str(target)}, ensure_ascii=False))
    elif args.command == "unpack":
        target = args.output
        if os.path.lexists(target):
            raise FileExistsError(f"refusing to overwrite decoded output: {target}")
        reader = CompactReplayReader(args.archive)
        target.parent.mkdir(parents=True, exist_ok=True)
        staging = Path(tempfile.mkdtemp(prefix=".compact-unpack-", dir=target.parent))
        try:
            for name, frame, _ in reader.iter_records():
                (staging / name).write_bytes(_encode(frame) + b"\n")
            os.rename(staging, target)
        finally:
            if staging.exists():
                shutil.rmtree(staging)
        print(json.dumps({"frames": reader.frame_count, "frame_names": reader.frame_names,
                          "output": str(target), "hash_verified": reader.hash_verified}))
    else:
        reader = CompactReplayReader(args.archive)
        count = sum(1 for _ in reader.iter_records())
        print(json.dumps({"replay_format": reader.manifest["replay_format"], "frames": count,
                          "frame_names": reader.frame_names, "hash_verified": reader.hash_verified,
                          "strict_native_reexecution": "not-requested"}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
