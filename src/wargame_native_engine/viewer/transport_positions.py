"""Read-only carried-location adapter shared by live and recorded viewers.

Only use units already included in this perspective. Historical raw coordinates
remain unchanged; display location is derived from a same-frame, reciprocal,
same-side BOARD relation, never proximity, roster identity, or future frames.
"""
from collections.abc import Mapping, Sequence


def attach_mounted_positions(viewer_units: list[dict], source_units: Sequence[Mapping], *, perspective: str = "GREEN") -> None:
    projected = {unit["operator_id"]: unit for unit in viewer_units}
    raw = {unit.get("obj_id"): unit for unit in source_units}
    for passenger in viewer_units:
        identifier = passenger["operator_id"]
        source = raw.get(identifier, {})
        carrier_id = source.get("car")
        # Side contact snapshots never provide transport membership. Do not
        # backfill it using omniscient raw records even if passed accidentally.
        if perspective != "GREEN" and passenger.get("side") != perspective.lower():
            continue
        aboard = source.get("on_board")
        if not (aboard is True or type(aboard) is int and aboard == 1) or type(carrier_id) is not int or carrier_id == identifier:
            continue
        carrier = projected.get(carrier_id)
        carrier_source = raw.get(carrier_id, {})
        occupants = carrier_source.get("passenger_ids", [])
        if (carrier is None or carrier.get("side") != passenger.get("side")
                or not isinstance(occupants, list)
                or not any(type(value) is int and value == identifier for value in occupants)
                or type(carrier_source.get("cur_hex")) is not int):
            continue
        # Legacy Source 1.0 may retain initial raw car/on_board fields after
        # unloading. Explicit current Native relation facts override that stale
        # template; contradictions must never hide an already unloaded unit.
        extra = source.get("extra")
        native = extra.get("native") if isinstance(extra, Mapping) else None
        extra = carrier_source.get("extra")
        carrier_native = extra.get("native") if isinstance(extra, Mapping) else None
        if (isinstance(native, Mapping) and "mounted_on" in native
                and (type(native["mounted_on"]) is not int or native["mounted_on"] != carrier_id)
                or isinstance(carrier_native, Mapping) and "passenger_id" in carrier_native
                and (type(carrier_native["passenger_id"]) is not int or carrier_native["passenger_id"] != identifier)):
            continue
        # An active boarding transition is still shown at the submission
        # location by the live lifecycle UI. Its carrier is locked stationary.
        if passenger.get("boarding_state") == "BOARDING":
            continue
        current_hex = carrier_source["cur_hex"]
        if current_hex < 0:
            continue
        path = carrier_source.get("move_path", [])
        target = path[0] if isinstance(path, list) and path and type(path[0]) is int and path[0] >= 0 else None
        progress = carrier_source.get("cur_pos", 0)
        progress = float(progress) if type(progress) in (int, float) else 0.0
        passenger.update({
            "current_hex": current_hex, "row": current_hex // 100, "column": current_hex % 100,
            "movement_target_hex": target, "movement_progress": max(0.0, min(1.0, progress)),
            "map_visible": False, "spatially_present": False, "spatial_status": "ON_BOARD",
            "carrier_id": carrier_id, "on_board": True,
            "position_source": "RECORDED_CARRIER_RELATION",
        })
        carried = carrier.setdefault("carried_units", [])
        if not any(unit.get("operator_id") == identifier for unit in carried):
            carried.append({key: passenger.get(key) for key in (
                "operator_id", "name", "icon_key", "alive", "destroyed", "boarding_state",
            )})
        carrier["transport_display_count"] = 1 + len(carried)
