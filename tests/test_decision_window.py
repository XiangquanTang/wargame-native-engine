from __future__ import annotations

from copy import deepcopy

import pytest

from wargame_native_engine.core.action import (
    ACTION_PRIORITY,
    ActionType,
    EngineAction,
    ValidAction,
)
from wargame_native_engine.gateway import (
    CommandGatewayError,
    DecisionWindow,
    DecisionWindowState,
)


def _valid(
    side: str,
    actor_id: int,
    *,
    action_type: ActionType = ActionType.MOVE,
    path: list[int] | None = None,
) -> ValidAction:
    action = EngineAction(
        side,
        actor_id,
        action_type,
        parameters={} if path is None else {"path": path},
    )
    return ValidAction(action, ACTION_PRIORITY[action_type])


def _window(
    red: tuple[ValidAction, ...],
    blue: tuple[ValidAction, ...],
) -> DecisionWindow:
    return DecisionWindow.open(
        session_id="session",
        decision_window_index=7,
        opened_at_sim_time_ms=12000,
        red_valid_actions=red,
        blue_valid_actions=blue,
    )


def test_hundred_actors_submit_one_ordered_batch_without_shared_mutability() -> None:
    red = tuple(_valid("red", actor, path=[actor, actor + 1]) for actor in range(100))
    window = _window(red, ())
    supplied = [item.action_id for item in reversed(red)]
    first_action_id = red[0].action_id

    window.submit(
        side="red", request_id="red-batch", ordered_action_ids=supplied,
    )
    supplied.clear()
    red[0].action.parameters["path"].append(999)  # type: ignore[union-attr]

    assert len(window.submissions["red"].ordered_action_ids) == 100
    assert window.submissions["red"].ordered_action_ids[0] == red[-1].action_id
    frozen = window.valid_action_snapshots["red"][first_action_id]
    assert frozen.action.parameters["path"] == [0, 1]


def test_batch_rejects_duplicate_actor_duplicate_id_and_nonfrozen_id() -> None:
    first = _valid("red", 1, action_type=ActionType.MOVE, path=[1, 2])
    second = _valid("red", 1, action_type=ActionType.STOP_MOVE)

    with pytest.raises(CommandGatewayError, match="duplicate_actor_action"):
        _window((first, second), ()).submit(
            side="red",
            request_id="duplicate-actor",
            ordered_action_ids=[first.action_id, second.action_id],
        )
    with pytest.raises(CommandGatewayError, match="duplicate_action_id"):
        _window((first,), ()).submit(
            side="red",
            request_id="duplicate-id",
            ordered_action_ids=[first.action_id, first.action_id],
        )
    with pytest.raises(CommandGatewayError, match="action_not_currently_valid"):
        _window((first,), ()).submit(
            side="red",
            request_id="not-frozen",
            ordered_action_ids=["action-not-in-window"],
        )


def test_other_side_action_id_is_not_authorized() -> None:
    red = _valid("red", 1)
    blue = _valid("blue", 2)
    window = _window((red,), (blue,))

    with pytest.raises(CommandGatewayError, match="not a current RED action"):
        window.submit(
            side="red",
            request_id="wrong-side",
            ordered_action_ids=[blue.action_id],
        )


def test_empty_batches_are_noop_and_arrival_order_does_not_change_merge() -> None:
    red = (_valid("red", 20, action_type=ActionType.STOP_MOVE),)
    blue = (_valid("blue", 10, action_type=ActionType.MOVE, path=[10, 11]),)

    def complete(order: tuple[str, str]) -> DecisionWindow:
        window = _window(red, blue)
        selected = {"red": [red[0].action_id], "blue": [blue[0].action_id]}
        for side in order:
            window.submit(
                side=side,
                request_id=f"{side}-request",
                ordered_action_ids=selected[side],
            )
        assert window.lifecycle_state is DecisionWindowState.READY
        window.close()
        return window

    red_first = complete(("red", "blue"))
    blue_first = complete(("blue", "red"))
    assert red_first.merged_action_batch == blue_first.merged_action_batch
    assert tuple(item.side for item in red_first.merged_action_batch) == (
        "blue", "red",
    )

    empty = _window((), ())
    empty.submit(side="red", request_id="r", ordered_action_ids=[])
    empty.submit(side="blue", request_id="b", ordered_action_ids=[])
    empty.close()
    assert empty.merged_action_batch == ()


def test_action_records_and_resolved_batch_are_detached_from_callers() -> None:
    source = _valid("red", 3, path=[3, 4])
    window = _window((source,), ())
    record = window.action_records("red")[0]
    record["action"]["parameters"]["path"].append(1000)  # type: ignore[index]
    window.submit(
        side="red", request_id="r", ordered_action_ids=[source.action_id],
    )
    window.submit(side="blue", request_id="b", ordered_action_ids=[])
    window.close()

    assert window.merged_action_batch[0].parameters["path"] == [3, 4]
    copied = deepcopy(window.merged_action_batch[0].to_dict())
    copied["parameters"]["path"].append(2000)
    assert window.merged_action_batch[0].parameters["path"] == [3, 4]


def test_closed_window_rejects_new_submission_and_closes_once() -> None:
    window = _window((), ())
    window.submit(side="red", request_id="r", ordered_action_ids=[])
    window.submit(side="blue", request_id="b", ordered_action_ids=[])
    window.close()
    window.mark_advancing()
    window.mark_completed()

    with pytest.raises(CommandGatewayError, match="decision_window_closed"):
        window.submit(side="red", request_id="again", ordered_action_ids=[])
    with pytest.raises(CommandGatewayError, match="decision_window_not_ready"):
        window.close()
