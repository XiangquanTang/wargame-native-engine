from __future__ import annotations

import json
from pathlib import Path
import shutil
import subprocess

import pytest


STATIC = Path("src/wargame_native_engine/viewer/static")


def test_live_action_index_is_server_id_only_and_has_no_pathfinder() -> None:
    source = (STATIC / "live_actions.js").read_text(encoding="utf-8")

    assert "item.action_id" in source
    assert "parameters?.path" in source
    assert "path[path.length - 1]" in source
    assert "actionsForMoveTarget" in source
    assert "actionsForShootTarget" in source
    assert "new EngineAction" not in source
    assert "pathfind" not in source.lower()
    assert "fetch(" not in source


def test_map_renderer_exposes_preview_and_double_activation_without_step() -> None:
    source = (STATIC / "map_renderer.js").read_text(encoding="utf-8")

    assert "setActionTargets" in source
    assert "_drawActionTargets" in source
    assert 'addEventListener("dblclick"' in source
    assert "onHexActivated" in source
    assert "onUnitActivated" in source
    assert "valid_action_id" not in source
    assert "/api/command" not in source


def test_dual_control_uses_shared_renderer_and_server_planning_only() -> None:
    source = (STATIC / "dual_control.js").read_text(encoding="utf-8")
    page = (STATIC / "dual_control_index.html").read_text(encoding="utf-8")
    renderer = (STATIC / "map_renderer.js").read_text(encoding="utf-8")

    assert 'new window.ReplayMapRenderer.MapRenderer' in source
    assert '"/api/product/preview"' in source
    assert '"/api/product/execute"' in source
    assert '"/api/product/capabilities"' in source
    assert "/api/product/status?token=" in source
    assert "let isRefreshing = false" in source
    assert "redrawMap: false" in source
    assert '"/api/plan-move"' not in source
    assert '"/api/batch/stage"' not in source
    assert '"/api/batch/submit"' not in source
    assert "destination_hex" in source
    assert "result.path" in source
    assert "setIndirectFireAreas" in source
    assert "indirect_fire_active_areas" in source
    assert "_drawTacticalOverlays" in renderer
    assert "active_remaining_seconds" in renderer
    assert "new EngineAction" not in source
    assert "pathfind" not in source.lower()
    assert "lineOfSight" not in source
    assert '<script src="/static/map_renderer.js"></script>' in page
    assert '<canvas id="battlefield"' in page
    assert "plannedPaths" in renderer
    assert "planned.path.map" in renderer


def test_product_page_hides_internal_control_panels_and_supports_multiselect() -> None:
    source = (STATIC / "dual_control.js").read_text(encoding="utf-8")
    page = (STATIC / "dual_control_index.html").read_text(encoding="utf-8")
    renderer = (STATIC / "map_renderer.js").read_text(encoding="utf-8")

    assert "算子命令" not in page
    assert "本窗口批次" not in page
    assert "action_id" not in page.lower()
    assert "decisionwindow" not in page.lower()
    assert "expected_window" not in page.lower()
    assert '<details id="advanced-debug"' in page
    assert '<details id="advanced-debug" class="advanced-debug" open' not in page
    for identifier in (
        "capability-panel", "direct-fire-panel", "weapon-choices",
        "cancel-operation", "execute-operation",
    ):
        assert f'id="{identifier}"' in page
    assert "data-operation=" not in page
    assert '<script src="/static/capability_ui.js"></script>' in page
    assert "const selectedUnitIds = new Set()" in source
    assert "modifiers.ctrlKey || modifiers.metaKey" in source
    assert "modifiers.shiftKey" in source
    assert 'event.key !== "Escape"' in source
    assert "isFriendlySelectable" in source
    assert "selectedUnitIds.has" in renderer
    assert "ctrlKey: event.ctrlKey" in renderer
    assert "this.drag.moved" in renderer


def test_realtime_tick_refresh_preserves_the_interaction_draft() -> None:
    source = (STATIC / "dual_control.js").read_text(encoding="utf-8")
    refresh = source[
        source.index("async function refresh()"):
        source.index('$("capability-panel").addEventListener')
    ]
    hex_selection = source[
        source.index("async function handleHexSelection"):
        source.index("async function selectDirectFireWeapon")
    ]

    assert "const refreshEpoch = interactionEpoch" in refresh
    assert "refreshEpoch === interactionEpoch" in refresh
    assert "reconcileInteractionDraft()" in refresh
    for destructive_update in (
        "selectedUnitIds.clear()",
        "activeTool = null",
        "targetHex = null",
        "targetUnitId = null",
        "operationPreview = null",
        "executeRequestId = null",
    ):
        assert destructive_update not in refresh
    assert hex_selection.index('activeTool === "MOVE"') < hex_selection.index(
        'previewOperation("MOVE"'
    )
    assert "selectedUnitIds.clear()" not in hex_selection


def test_inflight_refresh_and_preview_cannot_overwrite_newer_interaction() -> None:
    source = (STATIC / "dual_control.js").read_text(encoding="utf-8")
    capabilities = source[
        source.index("async function loadCapabilities"):
        source.index("async function handleUnitSelection")
    ]
    preview = source[
        source.index("async function previewOperation"):
        source.index("async function executeOperation")
    ]

    assert "expectedEpoch = interactionEpoch" in capabilities
    assert "expectedEpoch !== interactionEpoch" in capabilities
    assert "requestedActorIds" in capabilities
    assert "const previewEpoch = interactionEpoch" in preview
    assert preview.count("previewEpoch !== interactionEpoch") == 2
    assert "operationPreview = nextPreview" in preview


def test_product_toolbar_surfaces_rule_reasons_for_disabled_actions() -> None:
    source = (STATIC / "dual_control.js").read_text(encoding="utf-8")
    operation_count = source[
        source.index("function operationCount"):
        source.index("function renderToolbar")
    ]
    toolbar = source[
        source.index("function renderCapabilityPanel"):
        source.index("function renderDirectFirePanel")
    ]

    assert "unavailable[0].message" in operation_count
    assert "function operationHint" in operation_count
    assert "new Set" in operation_count
    assert "button.title = operationHint(capability.operation_type)" in toolbar


def test_human_command_ui_is_capability_driven_and_weapon_specific() -> None:
    source = (STATIC / "dual_control.js").read_text(encoding="utf-8")

    assert "let selectedCapability = null" in source
    assert "let selectedWeaponId = null" in source
    assert "function renderCapabilityPanel" in source
    assert "function renderDirectFirePanel" in source
    assert "activeShootTargetIds" in source
    assert "weaponEnvelopeHexes(tacticalOverlay, selectedWeaponId)" in source
    assert "weapon_choices: { [unitIds[0]]: selectedWeaponId }" in source


def test_live_action_index_pure_functions_when_node_is_available() -> None:
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node.js is not installed")
    module_path = str((STATIC / "live_actions.js").resolve())
    fixture = [
        {
            "action_id": "move-102",
            "source": "source_action_router",
            "action": {
                "side": "red", "actor_id": 1, "action_type": "MOVE",
                "target_id": None,
                "parameters": {"path": [102], "movement_mode": "NORMAL"},
            },
        },
        {
            "action_id": "shoot-2",
            "source": "source_action_router",
            "action": {
                "side": "red", "actor_id": 1, "action_type": "SHOOT",
                "target_id": 2, "parameters": {"weapon_id": 7},
            },
        },
        {
            "action_id": "blue-hidden",
            "source": "source_action_router",
            "action": {
                "side": "blue", "actor_id": 2, "action_type": "WAIT",
                "target_id": None, "parameters": {},
            },
        },
    ]
    script = f"""
const api = require({json.dumps(module_path)});
const values = {json.dumps(fixture)};
const targets = api.actionTargets(values, 1);
if (JSON.stringify(targets.moves[0].path) !== '[102]') process.exit(2);
if (targets.moves[0].action_id !== 'move-102') process.exit(3);
if (targets.shoot_target_ids[0] !== 2) process.exit(4);
if (api.actionsForMoveTarget(values, 1, 102)[0].action_id !== 'move-102') process.exit(5);
if (api.actionsForShootTarget(values, 1, 2)[0].action_id !== 'shoot-2') process.exit(6);
if (api.actionsForActor(values, 1).some((item) => item.action.side === 'blue')) process.exit(7);
"""
    result = subprocess.run(
        [node, "-e", script], check=False, capture_output=True, text=True,
    )
    assert result.returncode == 0, result.stderr
