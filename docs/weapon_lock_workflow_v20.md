# Explicit weapon lock and firing posture (current)

Introduced in v20 (semantics 1.9); v22 retains this behavior with semantics `native-joint-prestate/1.10`.

- LOCK requires stopped, no movement/stop transition, no active state transition, and no command loss. Choose one carried weapon and confirm; the lock becomes ready after the existing transition completes.
- The selected locked weapon can perform ordinary direct fire both stationary and moving. Lock is an alternative ready posture: arriving at the destination does not require unfolding or clearing the lock. Other carried weapons remain unavailable until explicit UNLOCK.
- UNLOCK requires the same stopped/non-command-loss conditions. It only clears binding; it does not refill ammunition or reset any generic, guided, or planned indirect cooldown. Normal elapsed simulation time continues to reduce timers.
- Unlocked weapons retain their original unfolded requirements. Guided and indirect fire retain their own deployment, targeting, and recovery rules. Ammo, visibility, target class, transition, and existing range-attempt adjudication rules are unchanged.
- Command loss is the typed `command_lost` state, migrated only from an explicit original `lose_control` flag. `keep` and suppression are not command loss. This release adds no invented dynamic cause/recovery of command loss, and fresh Source conversion still rejects unsupported active initial states.
- Old recorded replays remain viewable as recorded history; strict re-execution and snapshot restore require the current matching semantics before applying actions.

UI keeps weapon choice → preview → confirmation, and moves action circles into the existing right-side compact top rectangle. Tank weight badges use exact source names (重型坦克 H, 中型坦克 M, 轻型坦克 L); unknown tank weight is unbadged, never inferred from armor.
