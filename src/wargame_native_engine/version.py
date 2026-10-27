__version__ = "0.4.22"
ENGINE_VERSION = __version__
RULESET_VERSION = "1.0.0"

# Changes only when the meaning of an accepted, sealed action batch changes.
# Strict replay rejects a different value before executing its first step.
# 1.10 normalizes declared map elevations to exact grades before fire lookup.
ADJUDICATION_SEMANTICS_VERSION = "native-joint-prestate/1.10"
