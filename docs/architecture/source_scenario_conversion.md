# Source Scenario 到 NativeScenario 的转换边界

本模块只把明确的 Source 数据转成可审计的 **Native simplified rules** 输入。它不加载旧 SDK，不复刻官方裁决，也不使用历史 Replay 的结果反推规则。

## 两种显式模式

`strict-source` 只接受源 Scenario、源 Map 和当前仓库已有的确定性字段映射。任何 Native 运行必需信息缺失都会失败。

`explicit-native-supplement` 额外要求一个有版本、有 Hash 的补充文件。补充文件中的平台能力、装甲类别、武器执行画像、观察画像和终止语义都是 Native 假设，不是旧平台事实。转换 Manifest 分别记录源字段、确定性派生字段和补充字段。

两种模式都必须显式指定源 Scenario、源 `basic.json` 和 Map 坐标参数；不按文件名猜 map ID 或坐标系。

## 字段边界

| Native 字段 | Source 证据 | 转换处理 |
| --- | --- | --- |
| Scenario ID | `scenario_id` | 原值保留，并增加 Native 派生后缀 |
| Map | 显式 `basic.json` | 复用 `convert_benchmark_basic_map()`；Engine 与 Renderer 使用同一 NativeMap |
| 单位 ID | `operators[].obj_id` | exact-int、唯一性校验、原值保留 |
| 阵营 | `operators[].color` | 只接受已记录的 `0→red, 1→blue` 映射 |
| 初始格 | `operators[].cur_hex` | 必须存在于转换后的 NativeMap |
| 血量 | `blood/max_blood` | 原整数值保留，不做隐藏缩放 |
| 单位域 | `type` | 通过当前版本化 Source type mapping 确定性映射 |
| 细分类别 | `(type, sub_type)` | 必须在补充 profile 中精确匹配，不使用名称模糊匹配 |
| 武器携行 | `carry_weapon_ids` | 原 ID 保留；每个 ID 必须有 curated Source mapping |
| 装甲/武器/传感器执行语义 | Source 不完整 | 仅由显式 Native supplement 提供 |
| 部署 | `cur_hex` | 作为 `SOURCE_DEPLOYMENT` 固定开局；不使用 Replay 中间帧 |
| 城市 | `cities` | 作为来源元数据保留；没有证据时不自动开启占领胜负 |
| 终止 | `time.max_time` 语义不完整 | 由显式 Native termination profile 定义 |
| seed | Source 未提供 | 由调用方显式给出，supplement 只记录默认策略 |

## 首个样例选择

四场景就绪度见 `docs/verification/source_scenario_readiness.json`。Map 123 / Scenario 2201010105 被选为首个样例，因为 49 个单位均有有效初始格，五类 `(type, sub_type)` 和九个武器 ID 都能精确枚举，Map 123 的 10,000 格转换无告警。它仍不能使用 `strict-source`：Source 未给出可执行的 Native 武器/装甲/观察画像，而且 `max_time` 与 Native tick 的语义等价性未被证明。

## 隐私与运行方式

旧 Scenario、Map 和 Replay 保持在旧仓库只读位置，不复制进本仓库。转换器代码和 supplement 在当前仓库；转换产生的 canonical Scenario、Manifest 和运行产物写入调用者指定目录（通常 `/tmp` 或 `local_data/`），不提交历史私有输入或生成对局。

转换后的 canonical Scenario 可由 `ScenarioBundle` 和 `ScenarioCatalog` 直接加载，随后只通过 `WargameFacade → CommandGateway → GameSession.step → NativeEngine` 推进。产品 Web 不能读取转换 Manifest 中的隐藏部署，也不能绕过 Side Observation。

## Map 123 的两个显式 Profile

`2201010105.native-supplement.json` 是正式 Native simplified 转换配置，使用显式的 2880 秒上限。`2201010105.native-acceptance-supplement.json` 只把终止上限替换为明确标识的 900 秒验收条件；平台、武器、传感器、部署和地图字段与正式 Profile 相同。后者用于在有限测试时间内走到 Native termination，并不冒充 Source 原始终止规则。

正式转换命令：

```bash
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src python -m scripts.convert_source_scenario \
  --source-scenario /home/txq/wargame/wargame_benchmark_lab/local_data/sdk_data_snapshot/phase9a_multireplay_pilot/scenarios/2201010105.json \
  --source-map /home/txq/wargame/wargame_benchmark_lab/local_data/sdk_data_snapshot/phase9a_multireplay_pilot/maps/123/basic.json \
  --source-scenario-identity scenarios/2201010105.json \
  --source-map-identity maps/123/basic.json \
  --scenario-id 2201010105 --map-id 123 \
  --mode explicit-native-supplement \
  --supplement config/source_scenario_conversion/scenarios/2201010105.native-supplement.json \
  --output-dir /tmp/wargame_source_2201010105
```
