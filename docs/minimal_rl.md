# 最小深度强化学习基线：规则模仿 → PPO

新的训练入口是 `python -m scripts.run_rl`。旧 `run_training_roundtrip.py`、旧展平编码器及训练协议保留用于兼容/回归，不再作为推荐训练路线。引擎裁决、结束规则、人工操作及原实时 `simple-capture` 不变。

## 安装与先跑通

在项目根目录安装可选依赖（CPU/GPU 的 PyTorch 按自己的机器安装）：

```bash
python -m pip install -e '.[learner]'
PYTHONPATH=src:. python -m scripts.run_rl train \
  --input local_data/maps/inbox/map_221.zip \
  --scenario-input local_data/maps/inbox/2201010101.json \
  --output local_data/rl/smoke-221 \
  --bc-steps 4 --bc-epochs 1 \
  --updates 1 --rollout-steps 4 --epochs 1 --minibatch-size 4 \
  --steps-per-episode 8 --eval-steps 4
```

这是安装/真实梯度更新/存取权重/复盘的冒烟测试，不是训练充分或策略强度证明。输出目录必须不存在。map9601 改为自己的 `map_9601.zip` 与对应 `201033029601.json`；不要混用地图想定。

正式实验可从以下参数起步（不是最佳超参数承诺）：

```bash
PYTHONPATH=src:. python -m scripts.run_rl train \
  --input local_data/maps/inbox/map_221.zip \
  --scenario-input local_data/maps/inbox/2201010101.json \
  --output local_data/rl/baseline-221 \
  --bc-steps 1500 --bc-epochs 4 --steps-per-episode 1500 \
  --updates 10 --rollout-steps 128 --eval-steps 1500
```

默认 BC 128 步仅为较短热启动；远距离地图可能只产生锁定/机动标签，不能据此声称学会射击/夺控。查看 `imitation-summary.json` 动作覆盖再增加步数。地图221实测短评估约 6 秒/决策（机器、候选、局面会改变速度）；长训练可能耗时数小时。建议先短测，不默认长跑。每决策默认推进1000ms，折扣也是每决策；不要直接比较不同决策间隔的奖励。

## 实现与边界

- `training/minimal_teacher.py`：直接调用已有 `SimpleCaptureSeatAgent`。按己方算子轮转，一次只授权一个算子，取一个真实规则指令；不是实时模式的多算子批量下令。空闲推进但不作为 WAIT 标签。教师 MOVE 通过环境规划器加入本次候选；重新绑定目录后精确匹配动作、算子、目标、武器、目的地。无法匹配或歧义显式失败，不伪造标签。
- `training/minimal_ppo.py`：共享实体编码器 + 候选评分器 + 价值头，13,330 个参数。全局8；己方实体18；已观测敌方10；夺控点7；每候选70（动作类别、执行者/目标/武器属性和几何关系）。实体数量随局面变化，无固定32个夺控点。数字 ID 只用于关联，不直接当数值特征。没有21,038维全连接输入。它是无记忆的小型基线，不是 AlphaStar 复刻。
- `training/minimal_runtime.py`：复用 NativeTrainingEnv、原网关裁决、地图运行表和 Native 八因子分析。旧定长数据表仅在环境边界复用，网络去除填充、不展平输入。己方/敌方/武器/弹药容量来自实际想定，超限显式报错。
- 候选是有界空间规划的**子集**，默认最多256个、最多128次规划请求；并非枚举全图全部合法动作。教师可额外规划规则目的地，策略推理仍使用当前空间候选。超限失败，不悄悄改标签。未来如需更多战略目的地，应改候选生成器而非奖励作弊。
- 环境负责候选合法性和最终裁决；模型只对当前掩码内候选采样。合法性与能否打中/执行成功并不等价。PPO保存采样当时的不可变特征、候选和掩码，不在更新时重新生成。
- BC 是监督交叉熵，只热启动策略（共享编码器也更新），不伪造 advantage。随后重新开局收集 PPO on-policy 数据，价值头再由回报训练。
- 默认对手是固定随机种子的 `random-legal`：从对方当前空间候选中均匀选一个动作；**不是 simple-capture，也不是自博弈**。`--opponent no-op` 仅用于诊断。现有红蓝 simple-capture 启动命令不受影响。
- Actor 只看该方受限观察与公开静态地图/武器资料；完整八因子只用于 reward，禁止进入 actor 输入。

## 奖励和 advantage

以下为兼容旧 checkpoint 的 `legacy-v1` 默认。新实验的非线性接近、较大占领奖励、真实战损与每仿真秒时间成本见 [progressive-v2 升级说明](reward_upgrade_v2.md)，需显式 `--reward-preset progressive-v2`。

```
r = 1.0 × 原生胜负奖励
  + 1.0 × (本方夺控点加权占有比例_after - before)
  + 0.1 × (γ Φ_distance_after - Φ_distance_before)
  + 0.03 × (γ Φ_eight_after - Φ_eight_before)
```

权重均可 CLI 调整，默认 γ=0.99。占领奖励根据实际归属变化，不按 CAPTURE 指令数奖励；失去夺控点为负。距离势是公开地图有向邻接图上到夺控格/邻格的距离进度，按夺控点价值加权，使用存活、在场、未搭载的己方地面人员/车辆；不是地形代价、精确可通行路径或隐藏敌军信息。已占领点保留势值1，避免占领后删除目标导致突变。八因子势取 Native 分析 `phi`，红正蓝负；覆盖不完整/缺值直接失败，不默默置零。折扣势差并不保证原地不动时奖励为零。

真正终局的后继势/价值置零；时间上限是截断，保留后继势和值作 bootstrap，但 GAE 不跨重置传播。`δ=r+γV(next)-V(now)`，GAE逆序累计 `δ+γλA(next)`（λ=.95）；PPO使用旧 log_prob 的 clipped ratio，加价值损失、熵项和梯度裁剪。现有引擎结束条件没有被覆盖；训练上限不会自动宣布八因子领先方胜利。

## 输出、评估、复盘

- `initial.pt`：随机初始权重；`imitation.pt`：BC后；`final.pt`：每次 PPO 更新后保存的最新权重，含优化器配置/状态。
- `imitation-summary.json`：标签动作数、空闲步数、训练集模仿准确率前后、BC参数变化（不是泛化指标）。
- `training.jsonl`：各更新损失、参数变化、奖励原值/加权分量与步数。默认不发布训练局复盘，临时训练资料关闭后清理。
- `evaluation.json`：重新加载 final.pt 后的确定性 argmax 评估；`replay/replay.zip`：可由既有播放器加载的 compact 复盘，地图想定各存一次。

```bash
PYTHONPATH=src:. python -m scripts.run_rl evaluate \
  --input local_data/maps/inbox/map_221.zip \
  --scenario-input local_data/maps/inbox/2201010101.json \
  --checkpoint local_data/rl/baseline-221/final.pt \
  --output local_data/rl/eval-221 --seed 42 --eval-steps 1500
PYTHONPATH=src:. python scripts/run_replay_viewer.py \
  --replay-dir local_data/rl/eval-221/replay --port 8003
```

评估未填写的阵营、对手、决策间隔和候选上限继承 checkpoint；显式不一致会拒绝，避免静默换配置。奖励权重也继承 checkpoint。

同地图想定、权重、种子、决策间隔、候选上限和对手配置可复现动作轨迹。更换地图是迁移评估，不代表保证有效。默认 CPU；CUDA 不保证跨硬件逐位一致。这里只交付可训练基线，不附带宣称已训练充分的权重。
