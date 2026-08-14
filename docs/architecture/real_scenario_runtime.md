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

## 可重复的产品闭环验收

900 秒验收 Profile 的转换参数与正式 Profile 相同，只替换显式 Native 终止上限。生成验收 Bundle 后执行：

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

## 边界和限制

* 这是历史数据校准的 Native simplified 运行，不是旧 SDK 或官方规则等价复刻。
* 正式 Profile 已实际推进到 720 秒并完成相同 MOVE/SHOOT，未伪造终局；900 秒 Profile 只用于有限时间的终局/strict replay 验收。
* 武器 5 和 72 有来源 ID 映射，但当前 Native direct-fire 不执行其类别；转换 Manifest 明确告警。
* Map 221、96、9601 尚无逐场景审定 supplement，不能作为已完成的可执行转换。
* 本机没有 Node 或浏览器运行时；真实双端 HTTP 已测试，但没有伪造浏览器截图。
