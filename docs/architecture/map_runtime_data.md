# 地图移动代价与静态可视数据运行时

## 目的与边界

Native simplified rules 可以显式绑定一组与 `NativeMap` 身份一致的地图
运行时数据：四种移动代价矩阵和十种端点高度组合的静态视线矩阵。这解决
的是“如何安全、确定地使用已有地图数据”，不是恢复旧 SDK 的裁决实现。

运行时仍保持以下边界：

* 地图资源只影响相邻边移动代价和静态 LOS；
* 动作必须包含显式、逐格相邻路径，Engine 不推断缺失路径；
* `GameSession.step()` 仍是唯一推进边界；
* 观察距离、森林/居民地修正、隐蔽、射击和终止仍由 Native 规则决定；
* RED/BLUE 只得到投影后的观察，资源矩阵和 GREEN 证据不向控制端暴露；
* Recorder 只观察结果，不读取源 pickle/NPZ，也不调用规则；
* 未配置资源时，现有算法式移动和 LOS 路径完全不变。

## 为什么需要离线转换

Map 123 的 `123see.npz` 压缩文件约 22 MiB，但其中布尔数组的精确形状为
`(10, 100, 100, 100, 100)`，展开数据为 1,000,000,000 字节。文件大不是
不能使用的理由，但不能在每个 Session 内反复解压和复制。

`scripts.convert_map_runtime_data` 是唯一接受历史 `cost.pickle` 和
`see.npz` 的边界。它会：

1. 使用禁止全局对象、reduce 和持久引用的受限 unpickler 读取 cost；
2. 校验恰好四种模式、完整矩形尺寸、相邻边、有限正数和精度；
3. 把代价写为稳定排序、精确有理数的 canonical JSON；
4. 校验 NPZ 只有一个 `data.npy`，并验证 NPY 版本、`|b1`、C-order、精确
   shape 和精确解压尺寸；
5. 流式提取为只读 NPY，并记录源文件与 canonical payload 的 SHA-256；
6. 在原子发布前用正式运行时 loader 完整自检。

运行时不会 unpickle，也不依赖 NumPy。静态 LOS 使用只读 `mmap` 按单个
布尔值寻址；有界结果缓存只优化重复查询，不改变数据权威性。这个设计让
大文件可以作为共享、不可变资源被多个 Session 使用，而无需逐局复制约
954 MiB。

## Canonical 目录

转换结果为：

```text
<runtime-dir>/
├── map_runtime_manifest.native.json
├── movement_costs.native.json
└── visibility.native.npy
```

manifest 绑定：

```text
NativeMap map_id/hash
源 basic.json hash
源 cost.pickle hash
源 see.npz hash
canonical movement payload hash
canonical visibility payload hash
转换器版本、网格尺寸、坐标系统、模式语义
```

加载时会重新验证 Map identity 和两个 payload hash。Snapshot、Session
identity、Replay manifest 和 strict replay 都携带同一身份记录；缺失或
更换资源会导致 restore/strict replay 拒绝，而不是静默退回另一套规则。

## 移动模式

`cost.pickle` 的四个矩阵按固定顺序解释：

| mode | Native 使用者 |
|---:|---|
| 0 | VEHICLE + NORMAL |
| 1 | VEHICLE + MARCHING |
| 2 | PERSONNEL（Charge 仍应用明确的 Native 状态倍率） |
| 3 | AIR + NORMAL |

因子语义为 `maximum_speed / actual_speed`。每段速度由同一个
`MovementPlanner` 计算为 `basic_speed / factor`，规划、合法性检查和执行
共同使用这一结果。缺失边对该模式不可通行；证据记录 mode、精确因子和源
hash。没有显式 AIR 矩阵时，原有 `AIR_MOVEMENT_UNSUPPORTED` 行为不变。

## 静态可视模式

十种模式对应地面、20m、200m、500m 端点组合。非对称高度组合将高端点
放在矩阵第一坐标，查询方向不会改变结果。只有具有明确高度映射的单位才
使用矩阵：地面单位为 0m，canonical DRONE/LOITERING_MUNITION 为 200m，
或单位显式携带受支持的 `visibility_height_m`。未知 AIR 不会被猜入某个
模式，而是使用既有算法式 LOS。

矩阵的 `false` 会阻止观察；矩阵的 `true` 仅表示静态 LOS 通过，仍必须
通过 Native 观察距离、地形和隐蔽规则。Direct-fire 的
`observation_evidence` 记录实际使用的 mode、端点和两个 hash。

## Map 123 转换与启动

旧工程仅在离线转换时作为只读输入，不会成为运行时依赖：

```bash
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src \
python -m scripts.convert_map_runtime_data \
  --map-config /home/txq/wargame/wargame_benchmark_lab/local_data/sdk_data_snapshot/phase9a_multireplay_pilot/maps/123/basic.json \
  --movement-cost /home/txq/wargame/wargame_benchmark_lab/local_data/sdk_data_snapshot/phase9a_multireplay_pilot/maps/123/cost.pickle \
  --visibility-npz /home/txq/wargame/wargame_benchmark_lab/local_data/sdk_data_snapshot/phase9a_multireplay_pilot/maps/123/123see.npz \
  --coordinate-system odd-r \
  --output-dir /tmp/wargame_map123_runtime
```

转换完成后，正式运行只读取 canonical Scenario 和 canonical runtime：

```bash
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src \
python -m scripts.run_dual_side_control \
  --scenario-bundle /tmp/wargame_source_2201010105/native_scenario.json \
  --map-runtime-data /tmp/wargame_map123_runtime \
  --output-dir /tmp/wargame_map123_live \
  --host 127.0.0.1 --port 8002
```

同一参数也适用于 `scripts.run_source_native_match`。发布后的 Replay 必须在
strict replay 时提供同一 map-runtime resource；该约束防止事实记录与重演
使用不同地图代价或静态 LOS。

## 已验证范围

当前真实大文件转换和对局 smoke 只验证 Map 123 / Scenario 2201010105。
它不证明 Map 221、96、9601 或 37 万局均已兼容，也不证明 Native simplified
rules 与旧 SDK 等价。具体 hash、边数、Replay 和测试证据见
`docs/verification/map_runtime_data_acceptance.json`。
