# 可独立摘取的决策数据

本版保持原生对局、PPO 权重、规则策略、LLM/Jev 选择机制不变，改造决策记录和查看方式。
新对局在自身目录生成 `decisions.jsonl`，红蓝每方每窗口各一条 `native-decision-sample/1.0`。

## 每条记录包含什么

- `match_id / side / decision / window / sim_time_ms`：可读来源与时间，不需要全局哈希查表。
- `policy_input.observation`：该方当时可见的完整原生观测，去除技术哈希和动作 ID 链接。
- `policy_input.public_map`：该方控制器可获得的公共几何。合法格 ID 使用无损连续区间编码；单条记录可用 `expand_public_map` 还原，不依赖外部地图文件。它是公共格集合，不是额外全知地形/敌情。
- `policy_input.own_weapon_inventory / own_weapon_catalog`：当前己方库存和对应武器资料。
- `policy_input.strategic_plan`：该条决策当时缓存的完整 LLM 战略。规则/PPO 或未报告计划的插件为 `null`，不伪造计划。
- `policy_input.legal_candidates`：`c0/c1/...` 局部编号、完整业务动作字段和中文描述（已知规则授权哈希仅留在引擎绑定中）；保留全部合法候选，没有静默截断。
- `selection`：完整选中候选。未下达命令为 `null`。
- `history`：最多四条之前同方的选择及执行回执，内容直接嵌入；不递归嵌套以前整条记录，也没有未来数据。
- `model_details`：统一插件实际使用的可见态势、完整战略、当前候选概率及耗时。此轮模型未调用时明确 `model_calls=0`。
- `execution`：从引擎本窗口实际回执得到的 `accepted / rejected`，或明确的无命令/回执缺失/窗口未完成状态。
- `feedback`：推进一个窗口后，该方可见的新观测与时间，和模型输入分开。

`accepted` 仅说明引擎接受命令，不等于已移动到达、成功占领或击毁目标；应继续看后续反馈。
观察者胜负、完整双方复盘不注入任一方模型输入。训练时不要把 `feedback` 或未来标签混入输入。
记录中的历史用于分析/样本制作，并未暗中增加当前控制器的模型输入。

## 哈希在哪里

业务决策样本、LLM/Jev 决策日志和模型请求不依赖长哈希。模型仍使用当前窗口局部候选编号。
原生引擎内部的内容身份、传统复盘完整性校验和升级包文件校验保持兼容；执行时由原生候选表解析完整动作。
同一窗口的 `c0` 与下一窗口的 `c0` 没有跨窗口身份关系，必须使用该记录自己携带的候选内容。

## 查看

在项目目录，用新对局的实际路径替换示例中的目录：

```bash
PYTHONPATH=src:. python -m scripts.inspect_decisions --input local_data/matches/本局目录/decisions.jsonl --side blue --limit 20
```

输出示例：`窗口15 时间15000ms | 蓝方1号：停止移动 | 候选6 | 执行:accepted | LLM任务:...`
移动描述附带路径格编号，其他命令保留业务参数。查看某一时间段：

```bash
PYTHONPATH=src:. python -m scripts.inspect_decisions --input local_data/matches/本局目录/decisions.jsonl --side blue --start-window 100 --end-window 120 --limit 0
```

## 摘取片段

```bash
PYTHONPATH=src:. python -m scripts.inspect_decisions --input local_data/matches/本局目录/decisions.jsonl --side blue --start-window 100 --end-window 120 --output local_data/datasets/blue-100-120.jsonl
```

端点都包含。输出路径必须不存在。输出仍是完整样本，计划在每行内，不需要额外复制计划日志、索引文件或原局前100帧。
它可独立阅读、分析和制作 SFT/LoRA 输入目标；精确恢复仿真仍需完整引擎世界状态、场景与运行配置，不能把可见观测当作全知恢复检查点。

## 旧对局

授权字段兼容修复可读取初版已生成的完整 1.0 样本，不改写源文件。具体字段边界和验证见 [修复说明](decision_authority_hotfix.md)。Jev 候选中去除规则授权哈希可能改变真实模型概率，不承诺选择逐步不变。

升级不改写已有复盘、checkpoint 或日志。旧 hash-only `decisions.jsonl` 缺少当时观测/计划/命令参数，提取器会明确拒绝，不靠默认值或后续状态补造。
LLM/Jev 的旧 `-controller.jsonl` 也不是完整新样本。新格式从升级后新运行的对局产生。现有原生复盘播放器继续可用；本包先提供命令行可读查看，不声称已改造 Web UI。

## 两条基线

```bash
PYTHONPATH=src:. python -m scripts.run_rl_rules_match --max-steps 20
```

```bash
PYTHONPATH=src:. python -m scripts.run_llm_jev_match --max-steps 20
```

两者均调用同一个 `run_match`，相同数据格式。真实地图、想定、模型路径可通过各脚本 `--help` 覆盖。
统一插件通过旧 CLI 的 `--blue llm` 加指定工厂接入，独立插件进程返回选中原生候选及分析元数据，元数据不参与引擎裁决。

## 验证边界

新增测试覆盖单条摘取、完整计划保留、双方视野、原生接受/拒绝回执、失败窗口、非法旧记录、公共格区间还原、真实 HTTP 测试替身与插件进程。
真实大模型/GPU 对局需要在目标服务器验证。既有服务不需要重新下载模型；升级后重启对局 Python 进程。
