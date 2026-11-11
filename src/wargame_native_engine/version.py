__version__ = "0.4.24.1"
ENGINE_VERSION = __version__
RULESET_VERSION = "1.0.0"

# Changes only when the meaning of an accepted, sealed action batch changes.
# Strict replay rejects a different value before executing its first step.
# 1.11 allows completed matching DIRECT locks through movement/posture transitions.
# Weapon unfold now requires normal command control like lock/unlock.
ADJUDICATION_SEMANTICS_VERSION = "native-joint-prestate/1.11"
