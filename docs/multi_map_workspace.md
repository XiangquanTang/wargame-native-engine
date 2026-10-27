# 多地图加载与预览

多地图目录为 `local_data/maps`，与 `local_data/map123` 同级。Map123 和已有复盘保持原位。

## 1. 初始化并上传

在项目目录执行：

```bash
PYTHONPATH=src:. python -m scripts.run_maps init
```

将地图 ZIP 或已解压的地图文件夹放入 `local_data/maps/inbox/`。支持：

- 原始地图：`basic.json`，可带同目录的 `cost.pickle`、一个 `*see.npz`。
- Native 地图：`native-map.json` 或 `native_map.native.json`。
- Native 场景：`scenario*.json`。
- ZIP 可包含多个上述地图目录。

单个自定义文件名可用 `--input /完整路径/map.json` 指定。

目录分工：

- `inbox/`：上传的原始地图。
- `runtime-cache/`：按地图和源文件内容寻址的机动／视线缓存。
- `reports/`：每次检查的新报告、标准化地图和离线预览。
- `runs/`：多地图对局输出。

## 2. 批量检查

完整检查地图与已提供的运行数据：

```bash
PYTHONPATH=src:. python -m scripts.run_maps check
```

只检查地形并生成预览，独立于比例尺、机动表、视线表和通道配置：

```bash
PYTHONPATH=src:. python -m scripts.run_maps check --terrain-only
```

指定一个地图包：

```bash
PYTHONPATH=src:. python -m scripts.run_maps check --input /mnt/workspace/map_456.zip
```

原始数据缺少坐标制声明时，邻接边方向通过 odd-r 校验且包含可验证行奇偶偏移的斜向边后，按 odd-r 兼容约定处理；也可显式添加 `--coordinate-system odd-r`。已有但不支持的坐标声明仍报错，不被默认值覆盖。检查会核对已提供邻接边的方向是否符合 odd-r；报告 `coordinate_system_source` 区分源声明、CLI 和兼容约定，`coordinate_geometry` 记录检查的边数。没有斜向邻接证据且缺少坐标声明时，需通过 CLI 明确指定坐标制，地形预览也采用同样校验。要求每张图都具有可加载的机动／视线表时添加 `--require-runtime`；这不要求包含全部十个高度组合。

每张地图独立检查，一张失败后继续下一张。报告区分地形可加载、场景可加载与运行表状态；错误项保留具体原因。空目录或存在失败时命令返回退出码 2，全部通过返回 0。

检查复用现有地图转换、运行数据校验与只读内存映射实现。地图逐张处理；同内容缓存复用。原始文件不改写。运行表占用空间取决于地图大小，100×100 的十层完整视线表约 1 GB。

## 3. 查看地图

命令输出 `index` 和 `report` 的完整路径。打开对应目录的 `index.html`，点击地图名称进入预览，支持缩放、拖动、坐标和高程图例。页面也可下载到本机直接打开。

在云实例通过文件服务查看：

```bash
python -m http.server 8000 --directory local_data/maps/reports --bind 127.0.0.1
```

使用实例已有的 8000 端口访问入口，打开本次 `check-.../index.html`。

原始地形包用于地形与运行表测试。带完整算子配置的 Native 场景另存为报告子目录的 `scenario.json`，可作为 `scripts.run_realtime_dual_control --scenario-bundle` 输入；文件位置见报告中的 `scenario_path`。

已有示例地图启动入口保持一致：

```bash
PYTHONPATH=src:. python -m scripts.run_maps list
PYTHONPATH=src:. python -m scripts.run_maps run map123
```

## 4. 不同视线通道的兼容

支持 1–10 个静态视线通道，通道编号沿用 q_map 的十种定义：0 地地、1 低低、2 低地、3 超低地、4 超低超低、5 低超低、6 高地、7 高超低、8 高低、9 高高。高度分别为地面、超低空 20 米、低空 200 米、高空 500 米。

没有额外配置时，按“数组第 i 层对应规范通道 i”的兼容约定读取。例如三层对应 `[0, 1, 2]`。这是兼容约定，不是对旧地图生产版本的鉴定；报告会记录 `visibility_mapping_source`，并列出已有和缺失通道。

若某个地图保留的不是前 N 个通道，或物理存储顺序不同，在它的 `basic.json` 同目录放置 `map-compatibility.json`，数组顺序与 NPZ 各层一一对应：

```json
{"visibility_modes": [0, 3, 4]}
```

也可不改 ZIP，通过命令指定映射（应用于本次检查的所有原始地图）：

```bash
PYTHONPATH=src:. python -m scripts.run_maps check --input map_456.zip --visibility-modes 0,3,4
```

CLI 与同目录配置同时存在时必须一致；冲突单独记录为该地图失败。编号必须为 0–9 的唯一整数，数量必须等于实际数组层数。不同映射使用不同缓存，改变配置后不会沿用旧解释。已有十通道默认数据保持原来的查询顺序与兼容性。

缺少的高度组合不生成虚拟通道。请求这些组合时返回明确的 `missing_visibility_mode` 错误；区域查询工具会报告 `visibility_unavailable`，涉及缺失组合的场景观测刷新可能拒绝执行。`runtime.status=ready` 表示提供的运行表已成功加载，并不表示支持所有场景。地形预览和已有通道仍可使用。用于完整对局前，应确认场景里的算子高度组合都在地图提供的范围内。

## 5. 比例尺与数据来源

这些原始地图沿用 Map123 的每格 200 米约定。原始 `scale` 缺失时，完整运行转换使用 200 米；原文件声明了有效比例尺时保留其值，无效值报告错误。检查报告的 `hex_size_m` 与 `hex_size_source` 给出有效比例尺及来源，`conversion.json` 保存转换参数。

`structured-map.json` 保留源数据事实，因此源文件没有比例尺时其 `physical.hex_size_m` 仍为 null；完整运行使用的比例尺以 `native-map.json` 和检查报告为准。原始文件不会被补写。
