"""Compatibility imports for the shared operator metadata registry.

New non-Viewer code should import from ``shareddata.operator_registry`` directly.
These aliases retain the existing Viewer API without duplicating descriptors,
validation, or the registry loader's cache.
"""

from wargame_native_engine.shareddata.operator_registry import (
    OPERATOR_DESCRIPTOR_SCHEMA,
    OPERATOR_REGISTRY_SCHEMA,
    OperatorDescriptor,
    OperatorIdentity,
    OperatorRegistry,
    OperatorRegistryError,
    OperatorSymbol,
    WeaponDescriptor,
    default_operator_registry_path,
    load_operator_registry,
    resolve_operator,
)

__all__ = [
    "OPERATOR_DESCRIPTOR_SCHEMA",
    "OPERATOR_REGISTRY_SCHEMA",
    "OperatorDescriptor",
    "OperatorIdentity",
    "OperatorRegistry",
    "OperatorRegistryError",
    "OperatorSymbol",
    "WeaponDescriptor",
    "default_operator_registry_path",
    "load_operator_registry",
    "resolve_operator",
]
