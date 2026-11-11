# Ray 并行采样：今晚训练

本版在0.4.24的实体候选 PPO 上增加**同步 Ray CPU 采样进程 + 集中式 GPU 学习器**。`--num-workers 0` 保留串行路径；正整数启动真实 Ray，不会静默退回串行。每批所有 worker 使用同一版本权重，全部完成后才更新 PPO，禁止混合陈旧策略数据。不是 RLlib，不改裁决和结束规则。

## 安装检查

先安装适合当前驱动的 CUDA PyTorch；用下面命令核实。24GB显存充足，但本模型很小，速度主要取决于CPU环境计算，不保证GPU加速比例。

```bash
# 你的jzj环境已具备Ray2.51.1与CUDA PyTorch2.4.1，无需重装/降级
# 仅全新环境需要：python -m pip install -e '.[parallel]'
python -c "import torch,ray; print('torch=',torch.__version__,'ray=',ray.__version__); print('CUDA=',torch.cuda.is_available()); print(torch.cuda.get_device_name(0) if torch.cuda.is_available() else 'CUDA不可用')"
nproc
free -h
```

依赖范围Ray>=2.48,<3；直接复用你已验证导入的2.51.1。只有 CUDA=True 才使用 `--device cuda`；否则先修好 PyTorch/驱动，或明确改成cpu。GPU只运行学习器，Ray worker固定CPU、每个1个CPU资源和1个PyTorch线程。你已确认64个CPU、251GiB内存、TITAN RTX24GB，下面正式命令用16worker；先通过2worker预检。其他机器应按实际CPU/内存减少，不能只凭显存决定数量。默认Ray对象存储限定1024MiB（--ray-object-store-mb可调），不是按251GiB自动分配巨大共享内存；/dev/shm不足时Ray可能改用较慢的临时目录。程序拒绝超过CPU亲和性数量减1的worker配置。

## 必须先运行服务器并行预检

当前构建环境禁止Ray对象存储所需的Unix socket（EPERM），所以不能声称已通过真实Ray启动或GPU测试。以下命令必须在你的服务器通过后，再启动整夜训练；不会自动改成串行掩盖失败。

```bash
PYTHONPATH=src:. python -u -m scripts.run_rl train \
  --input local_data/maps/inbox/map_221.zip \
  --scenario-input local_data/maps/inbox/2201010101.json \
  --output local_data/rl/ray-check-221 \
  --num-workers 2 --device cuda --ray-check
```

看到 `RAY CHECK PASSED` 才算通过：检查两个不同worker进程、相同策略版本和权重，各运行一次真实环境决策。结果保存`ray-check.json`，不进行BC/PPO训练或长评估。若失败，保留日志与`failure.json`，不要直接开整夜任务。

## 再跑并行训练冒烟

```bash
PYTHONPATH=src:. python -u -m scripts.run_rl train \
  --input local_data/maps/inbox/map_221.zip \
  --scenario-input local_data/maps/inbox/2201010101.json \
  --output local_data/rl/ray-smoke \
  --num-workers 2 --device cuda \
  --bc-steps 4 --bc-epochs 1 --updates 1 --rollout-steps 4 \
  --epochs 1 --minibatch-size 8 --eval-steps 20 --checkpoint-every 1
```

冒烟的20步仅验证流水线，不是正式观摩。正式命令下面默认评估1500步，能保存完整上限内的可观看过程。

## 今晚启动：规则模仿 → PPO → 1500步评估

在项目根目录执行，确保输出目录尚不存在：

```bash
mkdir -p local_data/rl
nohup env PYTHONPATH=src:. python -u -m scripts.run_rl train \
  --input local_data/maps/inbox/map_221.zip \
  --scenario-input local_data/maps/inbox/2201010101.json \
  --output local_data/rl/ray-night-221 \
  --num-workers 16 --device cuda:0 \
  --bc-steps 1500 --bc-epochs 4 --steps-per-episode 1500 \
  --updates 100 --rollout-steps 64 --epochs 4 --minibatch-size 64 \
  --checkpoint-every 5 --eval-steps 1500 \
  > local_data/rl/ray-night-221.log 2>&1 &
echo $! > local_data/rl/ray-night-221.pid
tail -f local_data/rl/ray-night-221.log
```

`tail -f` 按Ctrl+C只退出查看日志，不停止后台训练。此参数是实验起点，不保证一夜完成或策略已优秀。BC步数与rollout步数均是**每worker**：16×1500=24000次规则决策；每次PPO更新16×64=1024次决策，100次共102400次，加1500次最终评估。实际时间受地图、候选数量和CPU影响。长时间采集期间，每60秒打印已完成worker数量和等待时间（不是帧进度），避免误以为程序卡死。规则空闲步不生成伪WAIT标签；看`imitation-summary.json`确认真正的LOCK/MOVE/SHOOT/CAPTURE覆盖，不保证每张地图全部出现。对手仍是random-legal，不是自博弈或规则对手。

## 中断、恢复、查看

- `final.pt` 每个完整BC/PPO阶段原子替换；`checkpoint-000005.pt` 等按配置间隔保留。初始安全权重也保存在`final.pt`，实际阶段看checkpoint元数据。
- Ctrl+C/异常不会把半批轨迹写成已完成更新。查看`interrupted.json`/`failure.json`与日志；已完成安全checkpoint不会被半写文件替换。断电/SIGKILL无法保证生成异常报告，但原子替换保护已有checkpoint。
- 恢复时使用**新输出目录**。恢复模型、优化器和学习器随机状态，丢弃在途轨迹，worker从新种子的新对局开始；不是逐帧无缝恢复。已完成BC的checkpoint不会重复BC。`--updates`是本次额外更新数。学习率/epochs/minibatch继承checkpoint的学习器配置；地图、阵营、奖励等关键环境配置必须匹配，否则拒绝。

```bash
PYTHONPATH=src:. python -u -m scripts.run_rl train \
  --input local_data/maps/inbox/map_221.zip \
  --scenario-input local_data/maps/inbox/2201010101.json \
  --output local_data/rl/ray-night-221-resumed \
  --resume local_data/rl/ray-night-221/final.pt \
  --num-workers 16 --device cuda:0 --updates 50 --rollout-steps 64 \
  --steps-per-episode 1500 --eval-steps 1500
```

如果暂时不想继续训练，任意完整checkpoint都能单独生成1500步评估复盘：

```bash
PYTHONPATH=src:. python -u -m scripts.run_rl evaluate \
  --input local_data/maps/inbox/map_221.zip \
  --scenario-input local_data/maps/inbox/2201010101.json \
  --checkpoint local_data/rl/ray-night-221/final.pt \
  --output local_data/rl/ray-watch-221 --device cuda --eval-steps 1500
PYTHONPATH=src:. python scripts/run_replay_viewer.py \
  --replay-dir local_data/rl/ray-watch-221/replay --port 8003
```

评估仍为单环境确定性argmax，每100步打印进度；达到原生终局会提前结束，1500上限是截断，不强行判胜。训练worker不发布每局巨量复盘；最终评估发布compact回放和Native八因子曲线。现有单环境功能、GUI与回放兼容。

## 并行正确性与限制

每个worker独立环境/随机种子，轨迹保留不可变候选/掩码、当时log_prob和值。真正终止不bootstrap；时间截断保留末状态值但切断GAE；每worker批尾也切断GAE，拼批不会跨worker传播优势。原规则教师先并行采集BC，再清空BC环境，PPO全新on-policy采样。worker失败时整批失败，不将部分批伪装完成、不悄悄重试陈旧状态。Ray只启动本机loopback节点，不自动连接环境变量指定的外部集群。

CPU真实多进程与重载恢复需通过发布验证；无GPU验证机器时不声称CUDA性能或整夜训练效果已经实测。网络很小，CPU采样仍是主要成本，过多worker可能增加内存压力和等待最慢worker的开销。
