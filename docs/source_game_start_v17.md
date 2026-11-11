# 上传地图与想定后开始新对局

地图 ZIP 与对应想定 JSON 分开放在 `local_data/maps/inbox/`，不用再压成总包。
本入口使用已有 Native 引擎规则，不启动旧平台 SDK。

## 启动

在项目根目录执行：

```bash
PYTHONPATH=src:. python -m scripts.run_maps start --input local_data/maps/inbox/map_221.zip --scenario-input local_data/maps/inbox/2201010101.json
```

9601：

```bash
PYTHONPATH=src:. python -m scripts.run_maps start --input local_data/maps/inbox/map_9601.zip --scenario-input local_data/maps/inbox/201033029601.json
```

默认 Web 端口为 8003、监听地址为 `127.0.0.1`，终端打印红蓝入口。按原云实例访问方式打开页面。
同一时刻启动第二个服务时使用不同端口，例如追加 `--port 8004`。

只解析和检查、不启动服务时追加 `--prepare-only`。可追加 `--coordinate-system odd-r`、
`--visibility-modes 0,1,2` 显式指定地图兼容参数；默认逻辑与多地图检查一致。

每次启动都生成独立的场景目录 `local_data/maps/scenarios/game-...` 和新的对局目录
`local_data/maps/runs/source-game-...`。原文件、旧场景与复盘不覆盖。

## 转换内容

- 按想定保存双方身份、原名、位置、血量、弹药、武器 ID、夺控点及路障。
- 初始搭载关系校验后导入；下车后的 `launcher/launch_ids` 保留为来源关系，不当作已搭载。
- 机动与视线运行表绑定转换后的地图，包括想定路障，避免使用另一版本的缓存。
- `source-scenario.json`、`source-map.json` 保存输入原文；生成的 `native-supplement.json`
  和转换清单记录来源、缺失字段及 Native 执行约定。

原想定未声明的执行字段沿用显式 Native 约定，例如观察范围配置、初始高度 0、种子 42；
其中 221 的无人机没有原始高度字段，初始高度 0 不代表已恢复旧平台飞行高度。
旧版 `observe_distance` 字典原样保留，观测继续采用现有 Native 命名配置。
时间上限按源 `max_time / tick` 转为 Native 帧数，并在补充配置中标明，不宣称旧平台计分等价。
221 的武器 5（便携导弹对地）使用知识库便携导弹对车辆表：射程 10 格，只作用于车辆，标准直瞄冷却 75 秒；沿用明确的 Native 弹药池 100 绑定，不增加弹药。
武器 69/71 的导弹池只在唯一可用池明确时采用该池；歧义输入拒绝，弹药不补充。

本入口要求一张原始地图含 `basic.json`、`cost.pickle` 和唯一 `*see.npz`。
它不会凭文件名猜测双方编成，也不把检测通过的地形预览当作可运行想定。

## 最简规则对打与观战

在原命令后添加两个席位策略即可；不添加时仍为原来的双人手动控制。

```bash
PYTHONPATH=src:. python -m scripts.run_maps start --input local_data/maps/inbox/map_221.zip --scenario-input local_data/maps/inbox/2201010101.json --red-policy simple-capture --blue-policy simple-capture --max-ticks 600
```

9601 只替换输入文件：

```bash
PYTHONPATH=src:. python -m scripts.run_maps start --input local_data/maps/inbox/map_9601.zip --scenario-input local_data/maps/inbox/201033029601.json --red-policy simple-capture --blue-policy simple-capture --max-ticks 600
```

- 按终端打印的红/蓝 URL 观看各自受限视角。规则控制的席位只读，服务端也拒绝该席位的浏览器指令，避免覆盖规则已排队动作。
- 可只设置一侧 `simple-capture`，另一侧默认 `human`，手动操作与规则对打。
- `--max-ticks` 是运行预算，到达后保存并退出，记作截断，不代表获胜。不设置时运行到想定终局或按 Ctrl+C；Ctrl+C 会等待正在裁决的 tick 完成，再保存已完成的帧，不会从中途打断裁决。
- 在双方均为 `simple-capture` 且设置 `--max-ticks` 时，可追加 `--fast`，离线加速运行，不启动 HTTP 服务。它不缩短游戏 tick；默认仍为每 tick 1000 ms 的模拟时间。
- 每侧每 tick 只提交一份完整决策；空决策不排队 WAIT，正在执行的路线继续前进。实时观战采用宿主统一推进、墙钟节拍，不让规则控制器自己推进时间。
- 输出目录除了原有 `replay.zip`，还包含 `policy-summary.json`：席位策略、决策原因、提交数量、命令最终状态及拒绝原因。数量是命令/意图统计，**不是命中、伤亡或实际效果统计**；被拒绝的组合命令也可能已有部分同组动作成功，效果应看复盘裁决。
- 若规则命令在执行时被拒绝，立即停止自动对打，保存未完成复盘与具体指令/错误，进程返回 2；不会反复重发或把接收回执当执行成功。

该策略是接口验证用的确定性基线，不包含空中、引导、间瞄、运输/下车等战术；既有规则、弹药和裁决未被改动。
