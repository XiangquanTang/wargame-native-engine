#!/usr/bin/env python3
"""Run the local, single-replay, read-only analysis viewer."""

from __future__ import annotations

import argparse
from collections.abc import Sequence
import sys

from wargame_native_engine.viewer import (
    create_replay_viewer_server,
    load_replay_view,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="View one local replay and its external eight-factor analysis.",
    )
    parser.add_argument("--replay-dir", required=True)
    parser.add_argument(
        "--map-config",
        help="optional explicit odd-r basic-map JSON; no filename inference",
    )
    parser.add_argument(
        "--map-coordinate-system",
        choices=("odd-r",),
        help=(
            "explicit presentation-only geometry for a map file that omits "
            "coordinate_system"
        ),
    )
    parser.add_argument(
        "--scenario-config",
        help="optional explicit Source scenario JSON for static display facts",
    )
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        view = load_replay_view(
            args.replay_dir,
            map_config=args.map_config,
            map_coordinate_system=args.map_coordinate_system,
            scenario_config=args.scenario_config,
        )
        server = create_replay_viewer_server(
            view,
            host=args.host,
            port=args.port,
        )
    except Exception as exc:
        print(f"{type(exc).__name__}: {exc}", file=sys.stderr)
        return 1

    host, port = server.server_address[:2]
    print(
        f"Replay viewer: http://{host}:{port}/ "
        f"({view.frame_count} frames, read-only)",
        flush=True,
    )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
