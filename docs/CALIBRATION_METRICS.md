# Calibration metrics

Trajectory output reports Euclidean mean, median, maximum and final position
error, coverage and matched-operator ratio. Time output reports errors for
first movement, contact, attack, damage, destruction and terminal where both
sides of the comparison exist.

Event output reports event-order and destruction-order consistency plus missing
counts. Combat/terminal output includes survival and cumulative-damage errors,
winner, reason, terminal/truncated consistency and terminal-time error.
Advantage output compares the optional historical curve with normalized red
minus blue health, including direction, leading side, peak time, final error,
MAE and normalized error.

The transparent total is:

```text
0.35 * position + 0.20 * event + 0.30 * terminal + 0.15 * advantage
```

Each component and weight remains in the report. The advantage curve is one
signal, not a truth oracle. Missing values remain explicit and are not filled
with artificial agreement.
