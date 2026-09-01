# Native Capability Registry

`native_capability_registry_v1.json` is a traceability index, not a rule
configuration file. It records the current implementation, Demo, interface and
Replay coverage of named Native simplified capabilities.

The registry cannot authorize an action, change adjudication, or infer a rule
from historical compatibility metadata. Engine code and the curated ruleset
remain authoritative.

```bash
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src python scripts/check_capabilities.py list
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src python scripts/check_capabilities.py check
```
