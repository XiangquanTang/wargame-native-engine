# 静态地图结构化 function

第一阶段直接使用 `structure_map` 的标准结果；第二阶段由模型调用独立的
`structured_map` function 获取相同的静态地形、高程与方向边数据。

## 宿主示例

宿主加载获准地图，按原生 `map_id` 注册 `NativeMap`，向模型提供工具定义与调用循环。
在仓库根目录使用 `PYTHONPATH=src` 运行：

```python
from wargame_native_engine.agents.map_tool_calling import (
    MapToolAdapter, function_schemas,
)
from wargame_native_engine.io.scenario import load_scenario
from wargame_native_engine.io.structured_map import structure_map

native_map = load_scenario("examples/source_movement_scenario.json").map
direct = structure_map(native_map)
adapter = MapToolAdapter({native_map.map_id: native_map})
schemas = function_schemas()  # 包装为所用模型 API 的 tools。

received = []
offset = 0
while True:
    # 宿主处理模型返回的名称、JSON 参数，再将结果反馈给该 function call。
    result = adapter.dispatch("structured_map", {
        "map_id": native_map.map_id, "offset": offset, "limit": 2,
    })
    assert result["map_hash"] == direct["map_hash"]
    received.extend(result["cells"])
    if not result["pagination"]["has_more"]:
        break
    offset = result["pagination"]["next_offset"]
assert received == direct["cells"]
print(direct["map_id"], direct["cell_count"], direct["cell_columns"])
```

完整且符合 benchmark 格式的 basic 地图，可由宿主通过
`convert_benchmark_basic_map(approved_path).native_map` 加载后注册。
首调生成结构化缓存，后续复用；更换地图时创建新 adapter。

## 工具参数与提示词

唯一 function 为 `structured_map`，参数 JSON Schema：

```json
{"type":"object","properties":{
  "map_id":{"type":"string","minLength":1,"maxLength":128},
  "offset":{"type":"integer","minimum":0},
  "limit":{"type":"integer","minimum":1,"maximum":256}
},"required":["map_id","offset","limit"],"additionalProperties":false}
```

提示词示例：“请调用 structured_map 获取地图 source-movement-fixture-map。
从 offset=0 开始，limit=256，按 next_offset 继续直到 has_more=false。
按 cell_columns 解释每行，未知值保留 null。”

## 数据与分页契约

- `cells` 按 y、x 排序；`cell_columns` 依次为 x、y、elevation_m、elevation_level、terrain_code、position_id、elevation_raw、terrain_raw、terrain_name、roadblock、neighbors、roads、rivers。
- x 为列、y 为行，position_id = y × 100 + x；方向顺序为 E、NE、NW、W、SW、SE。邻居、道路、河流随格分页。
- 每页携带 schema、地图身份、坐标、边界、尺寸、高程摘要、地形图例、列定义和 cell_count。`map_hash` 是规范化静态内容 hash；文件加载入口另附来源文件名与内容 hash。
- 高程按声明的 LEVEL/METERS 与单位转换；整数级别需精确整除，未知或非整数级别保留 null。原始高程与地形保留在 raw 列。
- `pagination` 包含 offset、limit、returned、total、next_offset、has_more。末页 next_offset 为 null；offset 等于 total 返回空末页，超过 total 报错。
- 默认上限为每页 256 格、512 KiB UTF-8 JSON。超限报 response_too_large，可减小 limit；若一格或元数据仍超限，由宿主调整字节预算。
- 参数字段闭合，分页参数只接受整数。注册表、返回值和 schema 均隔离可变引用。
- `MapToolError.code`：unknown_tool、invalid_arguments、unknown_map、invalid_map、invalid_offset、response_too_large。宿主将错误反馈给模型。

## 离线导出与验证

```bash
PYTHONPATH=src python -m wargame_native_engine.cli.structure_map examples/source_movement_scenario.json --input-format native-scenario
PYTHONPATH=src python -m unittest discover -s devtools/checks -p test_map_tools.py -v
```

输入格式显式选择 native-scenario、native-map 或 basic；输出默认为 stdout，
`--output NEW.json` 创建新文件。basic 可通过 `--coordinate-system odd-r` 显式声明坐标。
Python 文件入口为 `load_structured_map(path, input_format=...)`。
