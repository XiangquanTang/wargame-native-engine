# Weapon inventory and attack intent boundary

The Human product surface exposes a detached OWN-only weapon inventory from
the same `ControllerView` version as its observation and action catalog. The
versioned response schema is `human-command-capabilities/1.1`. The
inventory is not a legal-action list: every carried weapon remains present,
and each item refers to the current authoritative ammunition pool. Weapons
sharing a pool report the same count and identify the other pool users; they
do not create independent ammunition totals.

For direct fire, a controller submits actor, visible target, and an explicit
carried `weapon_id`. The Gateway verifies identity, control, visibility,
window version, and carried-weapon membership. It keeps the resulting intent
private from the legal action catalog and routes it through the normal
`GameSession -> NativeEngine.step` boundary. Native simplified adjudication
then decides range, target compatibility, ammunition, cooldown, movement, and
effects. A rejected intent is recorded as an Engine rejection and never
reported as applied.

This feature does not change historical Source-frame bytes. In particular,
old archives whose `remain_bullet_nums` values were static cannot recover a
historically accurate inventory. Live Human inventory uses the new dynamic
projection; replay action and event semantics remain compatible, and new
submitted intents remain strictly replayable through their recorded
`EngineAction`.
