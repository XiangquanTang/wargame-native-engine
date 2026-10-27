# Agent 游戏工具：观察、估算、操作

这组函数用于虚拟兵棋引擎。宿主绑定会话和席位，LLM 提供 JSON 参数。三类职责分开：

1. `game_region_visibility` 回答“这个算子能否观察指定区域里的假设目标”。
2. `game_weapon_catalog` 提供武器映射；`game_fire_effect` 估算打击效果。
3. 操作统一走 `preview_game_operation` → `submit_game_operation` →
   `game_command_result`。

## 从调用链理解代码

`GameToolAdapter` 负责工具名、参数、响应上限和请求去重；
`RealtimeAgentSession` 绑定席位、观测版本和冻结投影；
查询模块计算只读结果，`ProductOperationService` 处理操作预检和执行。

- 区域查询：`core/region_visibility.py` 中的类型、参数校验与结果。
- 打击查询：`adjudication/fire_effect_query.py`；武器映射：`adjudication/weapon_catalog.py`。
- 操作意图：`agents/seat.py` 的 `SeatOperation`。
- 接口入口：`agents/tool_calling.py`；可选 JSON 边界：`agents/tool_transport.py`。

建议阅读顺序：DTO → 纯查询 → Realtime 封装 → Agent 适配器 → 测试。
静态地图使用独立的 [structured_map 工具](map_structuring.md)。

## 最小可运行示例

从仓库根目录运行，设置 `PYTHONPATH=src`。示例沿用六格公开场景，
先查询，再预检移动、提交、由宿主推进一帧并读取结果：

```python
from pathlib import Path
from tempfile import TemporaryDirectory

from wargame_native_engine.agents.realtime import (
    ManualRealtimeClock, RealtimeAgentSession,
)
from wargame_native_engine.agents.tool_calling import GameToolAdapter
from wargame_native_engine.data import ScenarioBundle
from wargame_native_engine.operations import ScenarioMatchFactory

with TemporaryDirectory() as directory:
    session = RealtimeAgentSession(
        ScenarioMatchFactory(
            bundle=ScenarioBundle.from_path("examples/source_movement_scenario.json"),
            storage_root=Path(directory),
            red_controller_id="example-red", blue_controller_id="example-blue",
            decision_interval_ms=1000, frame_interval_ms=1000,
        ),
        run_namespace="tool-example", tick_interval_ms=1000,
        clock=ManualRealtimeClock(),
    )
    try:
        reset = session.reset(seed=42)
        tools = GameToolAdapter(session, reset.red_binding)
        observation = tools.dispatch("game_observation", {})
        visibility = tools.dispatch("game_region_visibility", {
            "observation_version": observation["observation_version"],
            "actor_id": 1,
            "region": {"x_min": 1, "y_min": 1, "width": 6, "height": 1},
            "target_profile": {
                "target_class": "PERSONNEL", "concealed": False, "height_m": 0,
            },
            "actor_assertion": {"current_hex": 101, "subtype": "fixture-vehicle"},
        })
        assert visibility["visibility"]["cell_count"] == 6
        catalog = tools.dispatch("game_weapon_catalog", {})
        effect = tools.dispatch("game_fire_effect", {
            "observation_version": observation["observation_version"],
            "actor_id": 1, "weapon_id": 36,
            "target_hex": 102, "target_model": "fixture-personnel",
            "target_profile": {
                "domain": "PERSONNEL", "blood": 3, "armor_class": "NONE",
                "concealed": False, "suppressed": False, "moving": False,
                "marching": False, "stacked": False, "close_combat": False,
            },
        })
        preview = tools.dispatch("preview_game_operation", {
            "observation_version": observation["observation_version"],
            "request_id": "preview-1",
            "operation": {
                "operation_type": "MOVE", "actor_ids": [1],
                "destination_hex": 102,
            },
        })
        receipt = tools.dispatch("submit_game_operation", {
            "preview_token": preview["preview"]["preview_token"],
            "request_id": "submit-1", "confirm_partial": False,
        })
        assert receipt["accepted"] and receipt["status"] == "PENDING"
        session.advance_tick()  # Host, never the model-facing dispatcher.
        feedback = tools.dispatch("game_command_result", {
            "command_sequence": receipt["command_sequence"],
        })
        assert feedback["command"]["status"] == "APPLIED"
        assert feedback["command"]["observation_version"] == observation["observation_version"]
        print(feedback)
    finally:
        session.close()
```

## 八个函数的输入与输出

所有响应都经过 JSON 序列化并与内部对象断开引用。查询返回
`episode_id`、`side`、`observation_version`、tick/frame/time 身份信息。

- `game_observation({})`：当前己方观测、版本及终局标记。
- `game_capabilities({actor_ids})`：选定己方算子的候选操作。
- `game_region_visibility({observation_version, actor_id, region, target_profile, actor_assertion?})`：
  结果位于 `visibility`，包括 `coverage=all|some|none`、`all_visible`、
  `any_visible`、`visible_count`、`coverage_fraction` 和逐格结果。
- `game_weapon_catalog({})`：结果位于 `catalog`，包含公开武器定义、型号映射与来源；
  缺失映射标记 `UNRESOLVED`。
- `game_fire_effect({observation_version, actor_id, weapon_id, target_id?|target_hex?,
  attacker_model?, target_model?, target_profile?, fire_mode?, guider_id?, indirect_profile?})`：结果位于 `effect`。
  返回伤害概率、损失分布、期望损失和可执行性；无法解析时返回明确状态及原因。
- `preview_game_operation({observation_version, request_id, operation})`：
  逐算子预检和 `preview_token`。
- `submit_game_operation({preview_token, request_id, confirm_partial})`：
  提交原样预检的操作，`accepted` 表示已排队。
- `game_command_result({command_sequence})`：读取本适配器提交的 command 和 executions。
  `APPLIED` 表示命令已交给引擎；长耗时动作的完成状态见 executions。

### 区域可见性参数

`region={x_min,y_min,width,height}` 使用 odd-r 列/行坐标，格号为 `y*100+x`。
所有格子必须在地图内，矩形最多 4096 格；结果按行优先排列。
`cell_fields` 声明 `cells` 每行的列名：格号、是否可观察、是否通视、距离、
观察上限、原因。`observable` 包含距离与目标修正，`line_of_sight` 单独表达通视。

`target_profile={target_class,concealed,height_m}` 是区域内的假设目标：

- `target_class`：`PERSONNEL`、`VEHICLE`、`HELICOPTER`、`DRONE` 或 `LOITERING_MUNITION`。
- 地面目标高度为 0；无人机/巡飞弹为 200；直升机为 20、200 或 500 米。
- 可选 `empty_fire_exposure_frames` 为 0–10 的整数，默认 0；正数假设目标处于空射暴露期，
  按现有扩展规则直接可见，`reason=empty_fire_exposure`。
  此分支的 `line_of_sight`、`observation_limit` 为 `null`，表示未评估。
- `actor_assertion` 可带 `current_hex`、`operator_type`、`subtype`。
  它们必须匹配当前己方观测，否则返回 `actor_assertion_mismatch`。

查询范围是“存活且已部署的假设目标”的覆盖情况，`target_assumptions` 明示该条件。
实际敌军的存在与隐藏状态始终由席位观测边界隔离。

### 打击估算参数

`target_id` 和 `target_hex` 必须且只能选一个：前者只能指向当前可见敌人，
后者是假设位置，并须指定 `target_model`。
`attacker_model` 是己方型号断言，实际目标模式下 `target_model` 也是型号断言。

`target_profile` 声明计算假设：

- `domain`：`PERSONNEL` 或 `VEHICLE`；`blood`：1 到 2^53−1 的整数。
  ID 与格号也必须为该安全范围内的非负整数。
- `armor_class`：`NONE`、`LIGHT`、`MEDIUM`、`HEAVY` 或 `COMPOSITE`。
- `concealed`、`suppressed`、`moving`、`marching`、`stacked`、`close_combat`：六个布尔值。

提供 profile 时字段必须完整；省略则返回 `UNRESOLVED` 和空概率。
`damage_probability` 是造成血量损失的概率，`expected_damage` 单位是血量。
三种射击模式的命中定义：

- `DIRECT`（默认）：Source 规则没有独立命中检定，`hit_probability=null`。
- `GUIDED`：`actor_id` 是射手，另带 `guider_id`；目标必须当前可见，
  且匹配冻结动作目录中的合法引导射击。注意这与操作的 `actor_ids=[guider_id]` 不同。
  `hit_probability` 按现有引导事件定义为非 `NO_EFFECT` 的概率，包含只压制不扣血的结果。
- `INDIRECT`：另带 `indirect_profile={observation_policy,planned_hex?}`。
  `observation_policy` 为 `UNOBSERVED`、`GRID_CORRECTED` 或 `TARGET_CORRECTED`；
  `planned_hex` 省略时使用目标格。结果分别提供散布表命中概率、目标格受影响概率与伤害概率，
  并包含飞行时间、持续时间和精确损失分布。

间瞄分布以“任务已发射、目标位置/状态保持假设值、落地观察策略符合输入”为条件，
范围为单目标首次落地，要求 `stacked=false`。飞行期间变化和持续区域后续效果由实际引擎处理。
`can_execute` 表达当前真实目标的执行证据，假设目标模式下为 `null`；
`can_execute_under_assumptions` 表达假设下的准备状态。实际操作统一走 preview/submit。

在上例的 `game_fire_effect` 参数基础上，间瞄改为武器 72、`fire_mode=INDIRECT`，
并添加 `indirect_profile={"observation_policy":"UNOBSERVED"}`；前提是当前己方算子
确实装备该武器。引导示例形状为 `actor_id=1,weapon_id=73,target_id=2,guider_id=3,
fire_mode=GUIDED`，ID 与引导关系应取自当前场景。

## 一个操作契约覆盖十二种操作

每种操作都放在 preview 的 `operation` 字段，必须带 `operation_type` 和
`actor_ids`。具体可用性来自当前 capabilities，最终以执行时校验为准。

| 类型 | 意图与附加字段 |
|---|---|
| `MOVE` | 机动；`destination_hex`，可选 `movement_mode`；路径由服务端规划 |
| `SHOOT` | 直射；`target_id` 或 `target_hex`；可选 `weapon_choices=[[actor_id,weapon_id]]` |
| `GUIDED_FIRE` | `actor_ids=[guider_id]`；`fire_mode=GUIDED`、`guider_id`、`shooter_id`、`weapon_id`、`target_id` |
| `INDIRECT_FIRE` | `actor_ids=[shooter_id]`；`fire_mode=INDIRECT`、`shooter_id`、`weapon_id`、`target_hex`，可选 `target_id` |
| `BOARD` | 搭载；一个 actor，`target_id` 为载具 |
| `UNLOAD` | 卸载；一个载具 actor，`target_id` 为被搭载算子 |
| `CAPTURE` | 夺控；一个 actor，`target_id` 为目标点 |
| `ENTER_CONCEALMENT` | 主动隐蔽；选定 actor |
| `STOP_MOVE` | 停止机动；选定 actor |
| `WEAPON_UNFOLD` | 展开武器；选定 actor |
| `WEAPON_LOCK` | 武器锁定；选定 actor |
| `WAIT` | 算子等待，或 `actor_ids=[]` 且 `whole_side_wait=true` 表示全方等待 |

`SHOOT` 或 `INDIRECT_FIRE` 只指定位置时执行空射资源/暴露流程，伤害目标必须由 `target_id` 显式指定。
群体操作仅部分可执行时，submit 必须显式设置 `confirm_partial=true`。

## 版本、重试与边界

宿主每席位每局保留一个适配器并持有时钟。Schema 唯一来源是 `function_schemas()`；
接入模型提供商时由宿主转换其外层格式。原有五个函数的名称、参数和返回值保持兼容。

- 查询使用同一版本的冻结席位投影。预检/提交采用严格当前版本；过期后重新观察、
  重新决策、用新 ID 预检。慢模型的调度与刷新策略由宿主负责。
- 同一预检 ID 对应同一版本/操作；同一提交 ID 对应同一 token/确认值。
  提交响应丢失时，保留适配器与原 ID 原样重试，读取缓存回执。
  每个 token 至多提交一次，去重在进程内有效；reset 后创建新适配器。
- 默认每局 4096 个不同预检与提交、64 个唯一 actor、512 KiB 响应；超限明确报错。
  预检进入服务即保留 ID；提交回执在响应检查前保存，以便恢复。
- 查询只读；submit 排队；宿主推进 tick；既有引擎完成裁决与日志。
  command 与 executions 各自带快照状态，读取时保留它们的时间身份。

常用错误：`invalid_arguments`、`actor_not_owned`、`actor_assertion_mismatch`、
`region_out_of_map`、`stale_observation`/`observation_version_stale`、
`request_id_conflict`、`preview_already_submitted`、`response_too_large`。
打击领域保留大写错误码，例如 `TARGET_NOT_VISIBLE`、`ACTOR_NOT_CONTROLLED`、
`ATTACKER_MODEL_MISMATCH`、`INVALID_TARGET_PROFILE`。
领域错误类型为 `GameToolError`、`RealtimeAgentSessionError`、`CommandGatewayError`；
未预期的程序异常继续传播，由宿主记录并修复。

### 可选 JSON 传输层

```python
from wargame_native_engine.agents.tool_transport import GameToolTransport

transport = GameToolTransport(tools, max_argument_bytes=64 * 1024)
manifest = transport.describe()
reply = transport.invoke("game_capabilities", '{"actor_ids":[1]}')
```

`describe()` 返回传输版本、引擎版本、裁决语义版本、schema SHA-256 和限制。
`invoke()` 返回 `{transport_version,ok:true,result}`，或
`{transport_version,ok:false,error:{code,message,recovery}}`。
恢复建议为 `refresh_observation`、`create_adapter_for_current_episode`、
`host_intervention` 或 `review_request`。

JSON 输入拒绝重复键、非对象根、非有限数字、无效 Unicode、超过 64 层嵌套及
超限 UTF-8 字节。`response_too_large` 也按原提交 ID 恢复。
实验记录保留 manifest、源码/场景/规则版本、seed、观测、模型版本与完整工具往返。

## 验证与其他入口

```bash
PYTHONPATH=src python -m unittest discover -s devtools/checks -p 'test_game_tool*.py' -v
PYTHONPATH=src python -m unittest checks.test_public_interface_contract checks.test_fire_effect_tool_modes -v
```

RL 训练仍使用 `NativeTrainingEnv` 的 decision-v1/v2 与对应
`current_decision_binding`，见[训练契约](../checks/TRAINING_VALIDATION.md)。
训练动作编码、Agent 协议与原有 `submit_operation` 语义保持兼容。
