# 庙算灵活型 Native 规则基线适配

## 定位与边界

本入口的策略名称为 `miaosuan_flexible_native_v1`。它是外部固定优先级规则
Agent 的 Native 兼容变体，不是 DRL，也不声称复现原环境策略强度。
原文件仍由用户单独提供；仓库不包含、自动下载或解压外部 Agent。

两个评测轨道必须分开解释：

- 原 `evaluation.run_benchmark`：learner 每个决策只能选择一个当前目录 ID；默认
  no-op 对手保持不变。可选 `opponent_factory(context)`、`opponent_id` 和
  `opponent_provenance` 支持真实 Native Agent 对手。每局重新构造、reset 和 close，
  `context.side` 是对手方；实际对手身份进入 manifest/hash。
- 新 `evaluation.miaosuan_runner.run_miaosuan_match`：单方 Source 完整算子批量输出，
  同一时间点生成双方动作后统一提交 NativeEngine。Python API 默认对手 `first-valid` 并非空操作；CLI 默认使用 `self-play`，
  使双方拥有相同的全方批量动作权限。
  与 Native 单动作对手对战是接口验证，动作配额不相同，不能直接宣称同预算胜率优势。
  `self_play=True` 才是双方相同 Source 批量接口，且独立构造实例和派生随机种子。

原训练、实时工具调用和操作接口没有改名或替换。两轨结果 schema 不同；历史
manifest、无自定义对手的配置与旧 artifact 读取方式不变。

## 兼容差异

以 adapter 输出的 `adapters` provenance 和每条动作 trace 为准：

- 使用 role=1 的单独指挥员，控制本方全部存活算子；这不是多人/人机混合席位协作。
- 输入仅为 RED/BLUE 过滤后的 Source 帧；不把 GREEN、敌方私有数据或未来状态给策略。
- static map/cost/visibility 来自已验证 runtime cache；不从不完整地图伪造移动或视距数据。
- Source numeric action 与 Native 当前合法动作精确匹配；不匹配则拒绝，无法支持的
  communication/grouping 等动作标为 unsupported。没有静默替换成另一条合法动作。
- 原策略所用 `attack_level` 不在 Native 合法动作数据中。兼容目录使用等分值和稳定顺序，
  会改变目标排序，不能称为原版等价评测。
- 对不可见敌方未知观察距离，`[]` 兼容为 `None`，不推测或泄露真实敌方距离。
- `translated` 表示通过适配匹配，不代表引擎采纳或造成伤害；以 replay 的裁决结果为准。

- JMPlan（type 8）通过当前本方间瞄 capability 和引擎 builder 绑定目标，不枚举全图目标，
  不绕过 Native 间瞄射程、状态和弹药检查。该 builder 不传给外部策略。
- `time.cur_step` 保持 Source 原契约的引擎 tick，不重新解释成秒。修改
  decision_interval_ms 会改变原策略 50/300/800 等 tick 阈值所对应的仿真时间，
  这种参数差异必须在比较时保持一致。

## 使用

先审查并信任外部代码和依赖。以下显式开关会在当前进程权限下导入外部 Python，
不是沙箱或网络隔离。不要把开关当作绕过运行审批的方式。

```bash
PYTHONPATH=src python scripts/run_miaosuan_baseline.py \
  --scenario examples/map123/scenario.json \
  --prepare-runtime-from examples/map123 \
  --agent-root /path/to/reviewed-external-directory \
  --agent-module ai.agent --agent-class Agent \
  --allow-external-code --opponent self-play \
  --max-decisions 3 --seed 42 --output /tmp/new-miaosuan-run
```

若已准备缓存，可把 `--prepare-runtime-from` 换成
`--map-runtime-data /path/to/validated-runtime-cache`。二者不能同时使用。
默认 `self-play` 是相同权限的批量策略自对弈。显式选择 `first-valid`、
`random-legal` 或 `noop` 仅用于不同动作预算下的接口 smoke，不作为可比的能力 benchmark。
换边使用 `--side blue`，多 seed 必须分别运行到新的目录。

普通小场景也必须提供与其地图匹配的 runtime 数据；缺少地图依赖会明确报错，
不会偷偷使用 Map123 的缓存或构造全可见地图。仓库 fake-policy 测试单独验证
runner 的批量、失败、随机种子、对手与 replay 链路，不执行上传代码。

Python 调用传入 `ScenarioBundle`、输出新目录、`agent_factory(side, seed)` 和
`policy_id`；factory 必须返回新的外部对象。Native 对手 factory 同样接收
`(side, seed)`，自定义时必须显式提供 `opponent_id`。外部 factory 本身应使用
收到的 seed，而非任意进程全局随机状态。adapter setup/step 的 Python random
状态独立保存恢复；仅支持单线程运行，不能隔离第三方线程或未申明随机库。

## 预算、证据和解释

`--decision-budget-ms` 对每方调用做返回后检查；超时的联合决策不推进仿真。
它不是硬中断，阻塞调用、构造/setup/close 和地图准备不包含在此时限中。
外部服务必须自行设置网络超时；不受信任代码应在额外的受限子进程中执行。

`--max-trace-bytes` 限制 decisions.jsonl，不限制 replay、runtime cache 或外部代码内存。
无法为最低证据留空间时不调用策略；实际输出过大时不应用该批动作，并在 result
中保留未应用提案摘要。不能无限制记录恶意超大策略对象。

输出包括 `result.json`、`decisions.jsonl`、`replay.zip` 及 recording 的原始帧。
记录原策略代码摘要（CLI）、adapter 差异、双边种子、每步原提案的 translated/
rejected/unsupported、Native 动作、耗时和最终状态。异常只保存类型，不记录可能
含凭证的原始异常文本。策略异常仍尽力发布并 strict replay 已提交的前缀。

正常到达短 horizon 仍做 strict native replay；`completed` 只说明有界协议完成，
不代表任务成功、策略更强或整局终局。`horizon_truncated`、引擎 outcome 和
`strict_replay_matches` 分开保留。运行期间源码漂移会标记 invalidated_provenance_drift。
