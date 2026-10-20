"""Explicit dispatch for Source projection, independent of adjudication."""
from collections.abc import Mapping

LEGACY_SOURCE_CONTRACT = "source-frame-compat/1.0"
CURRENT_SOURCE_CONTRACT = "source-frame-compat/1.1"
SOURCE_CONTRACT_KEY = "source_frame_compatibility_version"


def require_source_contract(value: object) -> str:
    if value not in (LEGACY_SOURCE_CONTRACT, CURRENT_SOURCE_CONTRACT):
        raise ValueError(f"unsupported Source projection contract: {value!r}")
    return str(value)


def source_contract_from_manifest(manifest: Mapping) -> str:
    """Absent historical identity means 1.0; conflicting identities fail closed."""
    contract = require_source_contract(manifest.get(
        SOURCE_CONTRACT_KEY, manifest.get("frame_contract", LEGACY_SOURCE_CONTRACT)
    ))
    if "frame_contract" in manifest and manifest["frame_contract"] != contract:
        raise ValueError("conflicting Source projection contracts")
    return contract


def validate_frame_contract(frame: Mapping, contract: str) -> None:
    extra = frame.get("extra")
    native = extra.get("native") if isinstance(extra, Mapping) else None
    declared = native.get(SOURCE_CONTRACT_KEY) if isinstance(native, Mapping) else None
    if declared is None and contract == LEGACY_SOURCE_CONTRACT:
        return
    if declared != contract:
        raise ValueError("Source frame projection contract mismatch")
