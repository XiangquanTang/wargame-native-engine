"""Human-readable startup only; no hashing, engine mutation or data loading."""
from collections import Counter
import json
from wargame_native_engine.version import ENGINE_VERSION, ADJUDICATION_SEMANTICS_VERSION


def print_startup(bundle, scenario, args, host, port):
    """Called only after reset and successful server binding."""
    runtime = bundle.map_runtime_data
    record = {
        "version": ENGINE_VERSION,
        "semantics": ADJUDICATION_SEMANTICS_VERSION,
        "scenario_path": str(args.scenario_bundle.resolve()),
        "scenario_id": bundle.scenario_identity.scenario_id,
        "map_id": bundle.map_identity.map_id,
        "map_source": str(args.scenario_bundle.resolve()) + "#map",
        "dimensions": [scenario.map.width, scenario.map.height],
        "distance_unit": scenario.map.distance_unit,
        "hex_count": len(scenario.map.hexes),
        "units": dict(sorted(Counter(unit.side for unit in scenario.operators).items())),
        "runtime_path": str(args.map_runtime_data.resolve()) if args.map_runtime_data else None,
        "movement_modes": list(runtime.movement.mode_order) if runtime else None,
        "los_modes": list(runtime.visibility.mode_order) if runtime else None,
        "output_dir": str(args.output_dir.resolve()),
        "tick_interval_ms": args.tick_interval_ms,
        "server": f"http://{host}:{port}",
    }
    if args.json:
        print(json.dumps({"startup": record, "identity": dict(bundle.public_record())}, ensure_ascii=False, sort_keys=True), flush=True)
    else:
        print(f"READY: v{ENGINE_VERSION} | realtime-fixed-tick {args.tick_interval_ms} ms", flush=True)
        print(f"SCENARIO: {record['scenario_id']} | {record['scenario_path']}", flush=True)
        print(f"MAP: {record['map_id']} | {record['map_source']}", flush=True)
        print(f"SIZE: {scenario.map.width} × {scenario.map.height} {scenario.map.distance_unit}; hexes={record['hex_count']}", flush=True)
        print("UNITS: " + ", ".join(f"{side}={count}" for side, count in record['units'].items()), flush=True)
        if runtime:
            print(f"RUNTIME: 已加载机动/视线表 | movement={record['movement_modes']} LOS={record['los_modes']}", flush=True)
        else:
            print("RUNTIME: scenario geometry/rules (no external movement or LOS table)", flush=True)
        print(f"OUTPUT: {record['output_dir']}", flush=True)
    print(f"RED:  http://{host}:{port}/red?token={args.red_token}", flush=True)
    print(f"BLUE: http://{host}:{port}/blue?token={args.blue_token}", flush=True)
    print("按 Ctrl+C 停止并保存复盘。", flush=True)
