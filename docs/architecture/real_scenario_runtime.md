# 真实 Source Scenario 的 Native 产品运行链

## 已闭合范围

首个可执行样例是 Map 123 / Scenario 2201010105。旧文件只读，转换后形成 canonical `NativeScenario`；其 10,000 格 `NativeMap` 同时进入 `ScenarioBundle.render_map_record()` 和 `NativeEngine`，不存在 Viewer 另载同名地图的路径。

运行链保持为：

```text
canonical NativeScenario
→ ScenarioBundle / ScenarioCatalog
→ WargameFacade
→ SideController / ProductOperationService
→ CommandGateway / DecisionWindow
→ GameSession.step
→ NativeEngine
→ GREEN Recorder / strict replay / EightFactorAnalyzer
```

转换 Manifest 是来源证据，不发送给 RED/BLUE 页面。控制页面只收到本方 RenderScene、Side Observation 和产品操作状态；GREEN、全知分析和另一方冻结动作不进入页面。

## 启动真实双端页面

先按 `source_scenario_conversion.md` 生成 `/tmp/wargame_source_2201010105/native_scenario.json`，再运行：

```bash
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src python -m scripts.run_dual_side_control \
  --scenario-bundle /tmp/wargame_source_2201010105/native_scenario.json \
  --output-dir /tmp/wargame_source_2201010105_match \
  --host 127.0.0.1 --port 8002
```

启动日志会打印 Scenario identity/hash、Map identity/hash、转换模式以及 RED/BLUE 地址。服务仍只监听环回地址，不声明互联网认证能力。

如果已按 `map_runtime_data.md` 将同一 Map 123 的 `cost.pickle` 和
`123see.npz` 转换为 canonical runtime，可以显式增加：

```text
--map-runtime-data /tmp/wargame_map123_runtime
```

此时 Engine 与 Renderer 仍使用同一个 ScenarioBundle/Map identity；移动
相邻边代价和静态 LOS 来自 hash 绑定的数据，Replay manifest 与 strict
replay 也绑定该资源。未提供该参数时继续使用既有算法式 Native simplified
移动和 LOS，不会在运行时访问旧 benchmark 工程。

## 可重复的产品闭环验收

900 秒验收 Profile 的转换参数与正式 Profile 相同，只替换显式 Native 终止上限，用于较快的端到端回归。生成验收 Bundle 后执行：

```bash
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src python -m scripts.run_source_native_match \
  --scenario-bundle /tmp/wargame_source_2201010105_acceptance/native_scenario.json \
  --output-dir /tmp/wargame_source_2201010105_acceptance_match \
  --seed 42
```

脚本不会访问 `WorldState`。它通过产品 Preview 执行以下固定证据路径：

1. RED 27/28/29 从 2742/2642/2542 向 2940 规划 3/4/5 段路径；
2. BLUE 通过产品层提交整方等待；
3. RED 71 从 2442 沿 19 段路径到 3327，经过多个事实帧并完成 75 秒停车；
4. 服务器在当前 RED 视角生成对 BLUE 75 的合法武器 73 和 54；验收显式选择 73；
5. Native direct-fire 记录 LOS、距离、弹药、表格和结果事件；
6. 运行到 900 秒 Native `max_ticks`，发布 Replay、control audit 和分析；
7. strict replay 逐步通过，在线与离线分析 JSONL 字节完全相同。

3328 看似接近 3427，但在 odd-r 下距离为 2，且该视线受地形高程阻挡；验收使用真正相邻且由规划器可达的 3327。该选择来自同一 odd-r/LOS 规则链，不是临时扩大射程或修改位置。

正式 2,880 秒 Profile 也已完整运行到 Native `max_ticks`：48 个决策窗口、48 个 step、49 个事实帧，最终 `sim_time_ms=2880000`。其 Replay SHA-256 为 `4e465cb123c318c5636ac75d47180b2b7bf9043bc3d07437bbc232f2072f0a21`，在线和离线分析 SHA-256 均为 `0fd368d77697b6921ea40621bc3348b2cbe2943ddd356855fd78e76c7433f2f1`，strict replay 通过。因此 900 秒 Profile 不再承担替代正式终局的含义，只保留为快速验收 Profile。

## 双端控制与页面刷新

RED 与 BLUE 在同一真实 Scenario 中分别取得 26/23 个己方单位；BLUE 初始同样具有 WAIT 和 MOVE 等产品操作能力。RED 提交后，RED 映射为“等待对方”，BLUE 保持“等待选择”，服务端并未阻止 BLUE 操作。

控制页初次加载使用完整 bootstrap；之后每秒只轮询轻量 product status。仅当 `frame_index` 改变时重新取得完整 RenderScene 并重绘 10,000 格地图，同时拒绝重叠刷新请求。这样保留同一地图/单位事实契约，并避免双浏览器每秒重复传输、解析和重绘约 1.7 MiB 静态场景。

## 边界和限制

* 这是历史数据校准的 Native simplified 运行，不是旧 SDK 或官方规则等价复刻。
* 正式 Profile 已实际推进到 2,880 秒并由 Native `max_ticks` 终局；900 秒 Profile 只用于较快的 MOVE/SHOOT/strict replay 回归。
* 武器 5 和 72 有来源 ID 映射，但当前 Native direct-fire 不执行其类别；转换 Manifest 明确告警。
* Map 221、96、9601 尚无逐场景审定 supplement，不能作为已完成的可执行转换。
* 本机没有 Node 或浏览器运行时；真实双端 HTTP 已测试，但没有伪造浏览器截图。
