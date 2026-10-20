"""Checkpoint identity guard available without importing a tensor backend."""


def require_checkpoint_authority(manifest, expected):
    if any(manifest.get(name) != value for name, value in expected.items()):
        raise ValueError('runner checkpoint/runtime authority mismatch')
