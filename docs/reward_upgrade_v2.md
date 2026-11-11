# 奖励升级包：progressive-v2

本升级只改训练奖励与相应配置、日志和回归测试；不改引擎裁决、动作粒度、策略采样或已有权重。**旧的 30-update checkpoint 不被覆盖，也没有声称新奖励已经训练出更强策略。**

## 先选择新实验，不要直接续训旧奖励

- 不指定 `--reward-preset` 时仍是 `legacy-v1`，五项旧 reward metadata 保持原格式，旧 checkpoint 可继续评估/按旧奖励续训。
- 新实验请明确指定 `--reward-preset progressive-v2`。下述命令是本升级推荐的新实验配置，而非静默更改全局默认。
- `--resume` 是严格续训：地图、想定、奖励元数据等必须一致。旧奖励 checkpoint 加 `progressive-v2` 会明确拒绝，不能借同一个训练记录悄悄换奖励。
- 本包未增加“仅载入权重”迁移入口。新奖励实验从新模型 + 规则 BC 热启动开始；不要手工篡改 checkpoint metadata 绕过检查。
- 输出目录必须不存在。旧 checkpoint 和旧实验目录请保留；不要用旧目录作为新 `--output`。

## 推荐初值

| 参数 | progressive-v2 默认值 | 含义 |
|---|---:|---|
| `--win-weight` | 10 | 原生胜/负奖励系数 |
| `--capture-weight` | 5 | 本方夺控点价值加权占有比例的净变化 |
| `--distance-weight` | 1 | 非线性接近势的净变化；累计净增益最多 1 |
| `--damage-weight` | 1 | 敌方新增真实损失的归一化奖励 |
| `--own-damage-weight` | 1 | 本方新增真实损失的归一化惩罚 |
| `--time-weight` | 0.005 | 每实际仿真秒扣分，不是每决策扣分 |
| `--approach-scale` | 3 | 接近势的图距离尺度（格） |
| `--eight-factor-weight` | 0 | 默认关闭旧八因子 shaping，避免重复计算战损方向 |
| `--gamma` | 0.99 | PPO 每决策折扣 |

占有全部目标的净增益为 5，明显大于全部接近势的净增益上界 1；单点按该点价值占总目标价值的比例分配，不是每按一次 CAPTURE 就奖 5。丢点扣回，再夺回只恢复先前扣分，不因反复占领产生额外未折扣净收益。靠近奖励随靠近程度递增；原地停留不刷接近分，后退扣回。

所有系数均可覆盖，但这些只是可检验的起始配置，不是最优权重承诺。日志保留原始项、加权项、状态前后值与总和，便于之后根据评估而不是训练总分调参。

## 公式与防重复计分边界

```text
r = 10 × 原生终局胜负
  + 5 × Δ目标加权占有比例
  + 1 × ΔΦ接近
  + 1 × Δ敌方累计真实损失比例
  - 1 × Δ本方累计真实损失比例
  - 0.005 × (本步实际仿真毫秒差 / 1000)
```

未占领点 `φ = exp(-d / 3)`，`d` 是公开有向邻接图到目标夺控区域（中心/邻格）的距离；取存活、在场、未搭载己方地面算子的最小距离。没有可用位置时势为零；已占领点势为 1；再按点价值加权，得到 `[0,1]` 的 `Φ接近`。越靠近目标，同样前进一格的增益越大。它不是地形代价或隐藏敌军信息。v2 用普通势差，**真正终局也不强制将距离势清零**，因此占领带来的接近收益不会被终局清零抵消。若手动开启八因子，v2 同样使用普通八因子势差；legacy-v1 保留原折扣势差与终局清零。

战损只读引擎权威快照 `health/alive`，按新增实际损失结算，不做命中因果归因（包括附带损伤或引擎生命周期销毁），不奖励开火次数、压制、敌人暂时不可见或从显示列表消失。部分来源规则将血量离散变化同步到 `health`，这里按已存在的实际粒度结算，不虚构连续伤害。每个单位记录截至当前最大实际损失，分母固定为想定初始全编制（含延迟增援）的初始健康值之和；初始健康值不是最大健康值，开局已经存在的损伤不奖励；每方累计归一化损失最多 1。增援到达不会产生伤害，缺失单位不假定死亡；死亡只补尚未计入的剩余损失，治疗后再损失不能重复刷同一份分数。权威战损和时间只在 reward 路径使用，不增加 actor 输入。

普通势差/净占有差意味着停留为零、来回未折扣净收益为零，**并不保证 γ<1 时的折扣回报没有循环偏好，也不是保持原最优策略不变的理论 shaping**。时间成本帮助压制拖延，但不是任何地图/系数下无循环获利的证明；须从复盘监测反复移动/争夺行为。

## 服务器上的新实验命令（仅供你执行）

工作目录沿用现有部署目录名（目录名含 v24 不代表本包退回旧源码）。先把升级包部署到该目录并备份原代码；不删除 `local_data` 和已有权重。环境已有可用 Torch/CUDA/Ray 时无需重新安装。

先短测真实 Ray 进程和配置（输出目录也必须全新）：

```bash
conda activate jzj
cd ~/miaosuan/wargame-native-engine-v24-ray
PYTHONPATH=src:. python -m scripts.run_rl train \
  --input local_data/maps/inbox/map_221.zip \
  --scenario-input local_data/maps/inbox/2201010101.json \
  --output local_data/rl/progressive-v2-raycheck-221 \
  --reward-preset progressive-v2 \
  --num-workers 2 --device cuda:0 --ray-check \
  --bc-steps 0 --rollout-steps 1 --steps-per-episode 2 --eval-steps 1
```

确认 `ray-check.json` 成功后，新训练参考命令：

```bash
PYTHONPATH=src:. python -m scripts.run_rl train \
  --input local_data/maps/inbox/map_221.zip \
  --scenario-input local_data/maps/inbox/2201010101.json \
  --output local_data/rl/progressive-v2-221-run01 \
  --reward-preset progressive-v2 \
  --num-workers 16 --device cuda:0 \
  --bc-steps 1500 --bc-epochs 4 \
  --updates 30 --rollout-steps 128 --steps-per-episode 1500 \
  --epochs 4 --minibatch-size 128 --eval-steps 1500 \
  --checkpoint-every 5
```

16 个 worker 需要至少 17 个可用 CPU 核心（程序保留一个核心预算），否则会拒绝，应降低 worker 数。BC 步数是**每 worker**，上述配置合计 24,000 个教师决策，可能很慢；这是用户启动长实验的参考，并非本次交付已经运行。CPU worker 采样、`cuda:0` 集中学习；每次更新收集 16×128=2,048 个决策。原随机合法动作对手与单算子决策机制保持不变。

## 怎样判断有效

用同一地图/想定、对手、种子组、决策间隔和评估时长比较旧新 checkpoint 的胜率、夺控情况、真实战损和动作分布。**不同奖励版本的总奖励不能直接比较。** 保存各自的 `evaluation.json` 与 compact replay。评估自动继承 checkpoint 奖励版本，不会拿新奖励解释旧模型训练元数据。

固定时长且无人提前终局时，各策略时间成本相同；单独增加时间扣分并不能让原地等待的动作比其它动作更差。它主要惩罚拖长的回合，接近/占领/战损项才区分同一时长内行为。真实提前终局停止计时，时间上限截断仍保留 PPO bootstrap；本升级不虚构胜利或重写结束规则。改变决策间隔后时间项仍按实际秒数，但 PPO 折扣仍按决策，训练目标不保证完全等价。
