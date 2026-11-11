# Source 动作与机动字段导出（1.3）

这是 **Native 新生成帧的兼容投影**，不是旧平台规则复刻，也不是历史 JSON 的格式归一化。
`source-frame-compat/1.0`、`1.1`、`1.2` 保持原导出行为；新对局使用 `1.3`。
分析模型选择（Source / Native）只改变分析结果，不改变 1.3 的动作、状态或 Native facts。

## 原始动作外壳

```json
{
  "cur_step": 75,
  "message": {
    "actor": "p11",
    "obj_id": 62,
    "type": 1,
    "move_path": [4129, 4229]
  },
  "extra": {
    "native": {
      "schema_version": "native-source-action-export/1.0",
      "semantics": "ACTION_INTENT_NOT_EXECUTION",
      "action_id": "…",
      "action": {"side": "red", "actor_id": 62, "action_type": "MOVE", "target_id": null,
                 "target_position": null, "parameters": {"path": [4129, 4229]}},
      "source_projection": {"status": "MAPPED", "reasons": []},
      "admission": {"accepted": true, "code": "…", "semantics": "ACCEPTANCE_NOT_COMPLETION"}
    }
  }
}
```

上例仅说明结构，不是战果证据。真实对局数据案例由公开工具接口生成。

- `actor` 使用该单位已声明的 Source `owner`，不把红蓝颜色、数组下标或虚构席位当玩家。
- `obj_id` 是动作主体单位；引导射击中是**引导者**，`guided_obj_id` 是**射手**。
- `cur_step` 取 Native 裁决前的 `tick_before`。帧 `time.cur_step` 是裁决后的状态时钟。
  录像文件序号 / compact `seq` 是第三个独立概念；亚秒多帧可能共用一个整数状态时钟。
- `actions` 是**本步输入意图**，不是持续动作的重复广播，更不是完成证明。
  `accepted` / APPLIED 不等于行军抵达、锁定转换完成或命中。
- 原生完整动作始终保留在 `extra.native.action`，严格重演输入仍在其独立步骤记录中。
  `MAPPED` 仅表示原始 `message` 字段已有已核实映射，不宣称两引擎全部语义等价。

## 已确认的映射

| Native 动作 | 原始 type | 原始 message 专属字段 |
|---|---:|---|
| MOVE（有明确格路径） | 1 | `move_path` |
| SHOOT / ATTACK / FIRE DIRECT（单位目标） | 2 | `target_obj_id`, `weapon_id` |
| BOARD | 3 | `target_obj_id` |
| UNLOAD | 4 | `target_obj_id` |
| CAPTURE | 5 | 无 |
| FIRE INDIRECT | 8 | `jm_pos`, `weapon_id` |
| FIRE GUIDED | 9 | `target_obj_id`, `weapon_id`, `guided_obj_id` |
| STOP_MOVE | 10 | 无 |
| WEAPON_LOCK | 11 | 无；武器选择是 Native 扩展 |
| WEAPON_UNFOLD | 12 | 无 |
| END_DEPLOYMENT | 333 | 仅 `actor`、`type`，不含 `obj_id` |

武器特定锁定不往旧 `message` 硬塞 `weapon_id`；完整值保留在
`extra.native.action.parameters.weapon_id`，并声明 `native_extensions: ["WEAPON_SPECIFIC_LOCK"]`。
END_DEPLOYMENT 只有该阵营唯一明确 owner 时才能生成原始 message；多个 owner 不猜发令玩家。

### 明确不猜的部分

- 原始 CHANGE_STATE 的动作码为 **6**，但原始 Agent 只转发候选 `target_state`，
  没有定义该枚举。不能把下方 MoveType 路径枚举强行当作 `target_state` 枚举。
- WEAPON_UNLOCK、ENTER_CONCEALMENT、WAIT、DEPLOY 没有核实的原始数值动作码。
  原始 **7、13–20 已有别的动作占用**，不分配这些数值冒充兼容。
- Native 空格直射不是 Source `type:8` 间瞄；没有原始单位目标形状时保留 Native 扩展。
- 任一必要字段或 owner 未知时，不生成假的 `message`；保留 `cur_step` 和
  `extra.native`，`source_projection.status=UNMAPPED` 及具体原因。

## 机动事实

1.3 中 `cur_hex`、`cur_pos`、`move_path` 来自当前权威 Native 状态：
当前裁决格、当前路径段格内进度、后续路径。`stop` 是另一个独立停止事实。
输入动作的 `move_path` 与若干秒后状态的剩余 `move_path` 不必相同。

已核实原始 Agent `MoveType` 与其 `get_move_type`：

| Source move_state | 含义 | Native 1.3 投影 |
|---:|---|---|
| 0 | Maneuver | VEHICLE + NORMAL |
| 1 | March | VEHICLE + MARCHING |
| 2 | Walk | PERSONNEL + NORMAL |
| 3 | Fly | AIR + NORMAL |

`move_state` 不是“正在移动”的 bool，也不能据非零值断言“行军”。
Native CHARGE_1 / CHARGE_2、未知域或无已核实对应关系的组合输出 `null`，
并在单位 `extra.native.movement_projection` 中声明 `UNMAPPED`；真实
`extra.native.movement_mode` 不丢失。不会借旧模板中的数字伪造当前模式。

读取器只在 **1.3 + 明确 UNMAPPED 标记** 时允许该 null，保留原值并产生 UNKNOWN warning。
旧 1.0–1.2 的输入校验不放宽。将未知模式导入可执行 Native 世界会明确报错，
而不是默默采用 NORMAL；回放显示不得把 unknown 显示为已知行军/普通姿态。

## 依据与边界

- 用户提供的 `2328.json`：动作 `cur_step=2272`，状态 `time.cur_step=2273`，
  间瞄 `{actor:"p11", obj_id:62, type:8, jm_pos:4129, weapon_id:72}`。
- 原始 `人机混合基准AI-灵活型.zip`，SHA-256
  `abaad58d04ac342a784b32df21ca28c0a94621163194e76c226e574579c21837`：
  `ai/agent.py` 的动作生成函数与 `ActionType`（887–909 行）、
  `BopType`（883–884 行）、`MoveType`（912–913 行）及 `get_move_type`（539–550 行）。
- `docs/rules/Native兵棋裁决知识库_Final.md` §3 明确格、格内进度、后续路径三者的含义。
- 这是出站投影，不改 `config/source_compat/action_mapping_v1.json` 的入站规则权威。
  不运行原始 Agent 来猜规则，不改动用户历史帧。

## 验证

`checks/test_source_action_export.py` 覆盖已确认动作、未知动作无损保留、owner 缺失、
OWN/敌方隔离、真实移动从中段到抵达、分析开关事实相同、旧契约严格重演和 unknown 输入边界。
对 v22 原实现与 v23 的同一六帧序列，三个旧契约合计 18 个完整 GREEN canonical hash 相同；
该比较使用固定公开 engine_version，覆盖 MOVE、持续推进、STOP 和拒绝动作，排除软件版本标识变化。

## 普通 compact 录制边界

上述完整 Native action 扩展仍用于 live/legacy-debug。普通 compact 在独立播放投影中
去掉已由 Source wrapper 表达的重复 command 结构，但保留 admission、业务 action_id、
side/action_type 和尚未映射的参数（包括 LOCK weapon_id）。UNMAPPED 不精简其原 Native 输入。
这不影响历史原始 JSON 的无损打包；详见 compact_replay_v23.md。
