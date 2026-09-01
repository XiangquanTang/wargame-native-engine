"""Inspect and validate the repository capability coverage matrix."""

from __future__ import annotations

import argparse
from pathlib import Path
from typing import Sequence

from wargame_native_engine.capability_registry import (
    CapabilityDescriptor,
    CapabilityRegistry,
    CapabilityRegistryError,
    load_default_capability_registry,
)
from wargame_native_engine.demo_registry import (
    DemoRegistry,
    DemoRegistryError,
    load_default_demo_registry,
)


def render_matrix(descriptors: Sequence[CapabilityDescriptor]) -> str:
    headers = (
        "CAPABILITY",
        "CATEGORY",
        "ENGINE",
        "DEMO",
        "REPLAY",
        "HUMAN",
        "AGENT",
        "DEMO ID",
    )
    rows = [
        (
            item.capability_id,
            item.category,
            item.engine_status,
            item.demo_status,
            _yes_no(item.replay_support),
            _yes_no(item.human_support),
            _yes_no(item.agent_support),
            item.demo_id,
        )
        for item in sorted(descriptors, key=lambda value: value.capability_id)
    ]
    widths = [
        max(len(headers[index]), *(len(row[index]) for row in rows))
        for index in range(len(headers))
    ]
    lines = ["Capability Matrix", _format_row(headers, widths)]
    lines.append("  ".join("-" * width for width in widths))
    lines.extend(_format_row(row, widths) for row in rows)
    return "\n".join(lines)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Inspect Native simplified capability traceability"
    )
    parser.add_argument("command", choices=("list", "check"))
    parser.add_argument("--registry", type=Path)
    parser.add_argument("--demo-registry", type=Path)
    args = parser.parse_args(argv)

    try:
        capabilities = (
            CapabilityRegistry.load(args.registry)
            if args.registry is not None
            else load_default_capability_registry()
        )
        if args.command == "list":
            print(render_matrix(capabilities.descriptors))
            return 0
        demos = (
            DemoRegistry.load(args.demo_registry)
            if args.demo_registry is not None
            else load_default_demo_registry()
        )
        capabilities.validate_demo_traceability(demos)
    except (CapabilityRegistryError, DemoRegistryError) as exc:
        parser.exit(1, f"Capability traceability: FAIL: {exc}\n")

    print(render_matrix(capabilities.descriptors))
    print(
        f"\nCapability traceability: PASS "
        f"({len(capabilities.descriptors)} capabilities)"
    )
    return 0


def _yes_no(value: bool) -> str:
    return "YES" if value else "NO"


def _format_row(values: Sequence[str], widths: Sequence[int]) -> str:
    return "  ".join(
        value.ljust(widths[index]) for index, value in enumerate(values)
    ).rstrip()


if __name__ == "__main__":
    raise SystemExit(main())
