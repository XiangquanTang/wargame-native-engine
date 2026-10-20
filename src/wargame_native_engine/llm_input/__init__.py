"""Read the existing frozen Source side frame for one bound RED/BLUE seat."""

from __future__ import annotations

from typing import Any

from wargame_native_engine.agents.realtime import (
    RealtimeAgentSession,
    RealtimeSeatBinding,
)


def get_llm_frame(
    session: RealtimeAgentSession,
    binding: RealtimeSeatBinding,
) -> dict[str, Any]:
    """Return detached JSON data from the binding's latest issued snapshot.

    ``controller_snapshot`` validates the existing authoritative binding and
    copies the paired views under the session lock. Reuse that copy without
    querying the engine, projecting again, or copying the frame a second time.
    The version/time fields keep their existing RealtimeSeatView meanings;
    ``source_frame`` keeps the existing Source projection contract unchanged.
    """

    if not isinstance(binding, RealtimeSeatBinding):
        raise TypeError("binding must be a RealtimeSeatBinding")
    if binding.seat not in {"red", "blue"}:
        raise ValueError("LLM input requires a bound red or blue seat")
    seat, controller = session.controller_snapshot(binding)
    frame = controller.source_frame
    expected = binding.seat.upper()
    if (seat.seat != binding.seat or controller.perspective != expected
            or frame.get("extra", {}).get("native", {}).get("perspective") != expected):
        raise ValueError("LLM input snapshot perspective mismatch")
    return {
        "seat": seat.seat,
        "observation_version": seat.observation_version,
        "tick_index": seat.tick_index,
        "frame_index": seat.frame_index,
        "sim_time_ms": seat.sim_time_ms,
        "source_frame": frame,
    }


__all__ = ["get_llm_frame"]
