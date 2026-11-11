# Capture pressure v3: an explicit new experiment

Use `--reward-preset capture-pressure-v3 --warm-start /path/to/old/final.pt --output /new/nonexistent/run`.
This does not install anything or modify the parent checkpoint. Default training remains legacy-v1; progressive-v2 remains supported. Do not reuse or delete the old run. `--resume` and `--warm-start` are mutually exclusive. Strict resume remains the Ray-only continuation route and still rejects reward/encoder/map/scenario and other recorded contract mismatches.

## Reward definition

All rates use elapsed **simulation** time, not wall time or decision count. For each static positive-value objective, default major rate is 3 and minor rate 1.5 points per 60 seconds. Major means `objective.value >= --pressure-major-value` (default 2); other positive values are minor; zero-value objectives have no pressure. No names are inspected.

**Map 221 supplied by the user has objective values 80 and 50. Its commands must include `--pressure-major-value 80`**, so the 80-value objective gets rate 3 and the 50-value objective rate 1.5.

For an uncaptured objective with minimum directed graph distance d to its capture region, pressure multiplier is `1 - (1 - floor) * exp(-d / scale)`, with default floor .5 and scale 3 graph edges. Alive, spatially present, unmounted ground units only can reduce distance. Standing in the capture region still pays 50%; own control pays zero; enemy control doubles pressure. Missing/unreachable objective geometry or missing rows receive full neutral pressure, never an ownership/approach credit. Pressure is summed across objectives. Integration uses the pre-step rate times the exact elapsed seconds divided by 60; a capture does not retroactively erase the preceding interval. Losing control restores pressure on the next interval.

Defaults: win 10; capture 5 times net value-weighted ownership change; approach-potential change 1; normalized enemy damage 3; normalized own damage penalty 1; normalized enemy kills 1; ordinary time penalty 0; eight-factor reward 0. Capture loss gives an equal negative change, so repeated recapture cannot farm net capture reward. Damage uses immutable scenario health capacities and each unit's maximum observed actual loss. Healing, disappearing, reserve arrival, repeated death, and reappearance cannot repeat credit. Kill reward is once per initially alive positive-health enemy, divided by the original eligible enemy count. Total damage+kill positive reward is bounded by 4; all-objective capture gain is 5. Terminal adjudication, opponents, action legality and one-command windows are unchanged.

Each setting is explicit via `--pressure-major-rate`, `--pressure-minor-rate`, `--pressure-major-value`, `--pressure-distance-scale`, `--pressure-floor`, `--pressure-enemy-multiplier`, `--kill-weight` and the existing reward coefficients. Run/checkpoint metadata records the resolved values. Auditable components include `objective_pressure` and `enemy_kill` (zero under old presets).

## Encoder migration and checkpoint safety

Old absent encoder metadata means `native-minimal-entity-ppo/1.0`. Old evaluation, match and strict resume retain exactly the old encoder. New v3 uses `native-minimal-entity-ppo/1.1-capture-objective`. Architecture/feature widths remain unchanged. CAPTURE alone joins the typed public objective table instead of the operator table. Its ten target slots encode normalized x/y/radius, log value, neutral/own/enemy flags and three zeros. Its target geometry is the public objective location. Missing objectives fail closed; identical operator/objective IDs cannot alias. Other action features are unchanged. No hidden enemy information is added to policy input.

Warm-start uses `torch.load(weights_only=True)`, validates architecture, tensor names, shapes, dtypes and finiteness, and copies the own/enemy/objective encoders plus policy tensors exactly. The critic, Adam state, sampling/shuffle RNG, counters and episodes are freshly initialized. Behavior cloning defaults to zero for warm-start; an explicit `--bc-steps` opts back in. Parent SHA-256, config/reward, copied/reset modules, encoder transition and `exact_resume: false` are recorded. Actor tensors are identical immediately after loading; changed CAPTURE inputs mean CAPTURE logits are **not** claimed identical. Worker and learner use the same versioned encoder. Old reward metadata remains compatible.

## Suggested pipeline smoke (not policy quality evidence)

After installing with the separately delivered safety installer and checking Ray on the actual server, use a fresh directory and the existing environment:

```bash
PYTHONPATH=src:. python -m scripts.run_rl train \
  --input local_data/maps/inbox/map_221.zip \
  --scenario-input local_data/maps/inbox/2201010101.json \
  --output local_data/rl/capture-pressure-v3-smoke-221-new \
  --warm-start <PROJECT_ROOT>/local_data/rl/progressive-v2-221-20261031-210016/final.pt \
  --reward-preset capture-pressure-v3 --pressure-major-value 80 \
  --num-workers 2 --device cuda:0 --bc-steps 0 \
  --updates 1 --rollout-steps 2 --steps-per-episode 4 \
  --epochs 1 --minibatch-size 2 --eval-steps 4 --checkpoint-every 1
```

Local short tests cannot establish improved learned strategy or server CUDA/multi-process Ray compatibility. Keep original checkpoints and evaluate new training independently.
