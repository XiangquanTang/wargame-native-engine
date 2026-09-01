# Unit State Machine Audit v1

> Status: read-only architecture audit, 2026-08-25
> Scope: current Native simplified Source-compatible runtime. This document
> does not define new adjudication rules and does not describe official or
> SDK-equivalent behavior.

## 1. 当前状态清单

### 1.1 结论

当前没有一个完整、封闭的 `UnitState` enum。`OperatorState.state` 是一个
可序列化字符串兼容字段，而真正的算子生命周期由多个互相正交的权威字段共同
表达。仅查看 `state` 会丢失机动、搭载、武器准备、压制和观察语义。

证据入口：

- `src/wargame_native_engine/core/operator.py::OperatorState`
- `src/wargame_native_engine/core/world.py::WorldState`
- `src/wargame_native_engine/core/action_router.py::ActionRouter.common_error`
- `src/wargame_native_engine/core/engine.py::NativeEngine._source_valid_actions`

### 1.2 `state` 字符串的实际取值

| 值 | 当前含义 | 写入位置 | 契约强度 |
|---|---|---|---|
| `ready` | legacy 默认 operational label | `OperatorState.from_dict`、legacy scenario | 兼容值，不是 Source 主状态 |
| `ACTIVE` | UNLOAD 完成后的独立乘员 | `_advance_source_boarding_transitions` | Boarding 明确写入 |
| `STOPPED` | 已停止或状态转换完成 | `MovementRuntime` | Source 常用 operational label |
| `MOVING` | NORMAL/CHARGE 路段运行中 | `MovementRuntime.start` | 必须与 segment 字段联合判断 |
| `MARCHING` | marching 模式路段运行中 | `MovementRuntime.start` | `movement_mode` 才是持续模式权威 |
| `BLOCKED` | 路段启动或到达受阻 | `MovementRuntime` | 短暂值，随后进入停止转换 |
| `TRANSITIONING_TO_STOP` | move-to-stop timer | `MovementRuntime._start_stopping` | 有明确 timer |
| `TRANSITIONING` | 武器或 marching mode 转换 | `_start_source_transition`、`_change_source_state` | 具体种类在 `transition_kind` |
| `SUPPRESSED` | 正在承受压制 | fire effect application | timer 在 `suppression_remaining_ms` |
| `ON_BOARD` | 乘员已建立搭载关系 | `_apply_source_boarding_intents` | 与 `mounted_on`/`deployed=false` 联合成立 |
| `destroyed` | 已毁伤至零 | fire/indirect/boarding cascade | 与 `alive=false`、`blood=0` 联合成立 |
| `CLOSE_COMBAT` | 外部直瞄禁止条件 | Direct Fire request/exporter 只读取 | **未找到 Native 写入转换；来源/进入退出规则 UNKNOWN** |

大小写并不统一（例如 `ACTIVE` 与 `destroyed`）。Legacy `_apply_state()` 还能写入
任意字符串；Source-compatible `_change_source_state()` 仅接受已声明的移动模式
转换。因此 `state` 目前不是可独立验证的有限状态机。

### 1.3 隐含和正交状态

| 状态维度 | 权威字段 | 说明 |
|---|---|---|
| 生存 | `alive`, `blood`, `health`, `max_blood`, `max_health` | `blood==0` 在构造时强制 `alive=false`、`state=destroyed` |
| 部署/在场 | `deployed`, `current_hex` | 观察和 Source valid actions 需要 alive+deployed；搭载乘员没有 hex |
| 实际移动 | `segment_to_hex is not None` (`moving` property) | 比 `state==MOVING` 更精确 |
| 移动任务 | `move_path`, `movement_action_id`, `movement_action_sequence` | 可在停止或阻塞时保留未完成路径 |
| 移动模式 | `movement_mode` | `NORMAL`, `MARCHING`, `CHARGE_1`, `CHARGE_2`；不是生存状态 |
| 停止转换 | `stopped`, `move_to_stop_remaining_ms` | `speed==0` 不等价于已停止完成 |
| 通用转换 | `transition_kind`, `transition_remaining_ms` | weapon lock/unfold 和进入/退出 marching |
| 武器姿态 | `weapon_locked`, `weapon_unfolded` | 两个布尔字段不是严格互斥 enum |
| 武器恢复 | `weapon_cooldown_remaining_ms` | MovementRuntime 每次推进时递减 |
| 压制 | `suppression_remaining_ms` + `state` | timer 是可计算事实，字符串用于表现/兼容 |
| 疲劳 | `fatigue_level`, `fatigue_recovery_elapsed_ms` | 影响人员 charge/move，停止时恢复 |
| 引导恢复 | `guided_fire_recovery_remaining_ms` | 能力生成和验证会阻塞非零值 |
| 引导权限 | `guidance_profile_id`, `guidance_affiliation_id`, evidence | 权限/关系，不是瞬时姿态 |
| 搭载关系 | `mounted_on`, `passenger_id` | 双向一对一关系 |
| 搭载转换锁 | `movement_locked`, `WorldState.boarding_transitions` | 锁期间除 WAIT 外的动作被拒绝 |
| 隐蔽 | `raw_attributes["concealed"]` | 当前实际参与 Visibility 和 fire modifier，但尚无 typed authority field |
| 被发现 | 每个 observer 的 `raw_attributes["see_enemy_bop_ids"]` | 观察刷新派生值，不是目标的全局状态 |

`WorldState.stage`（DEPLOYMENT/COMBAT/TERMINAL）是会话状态，不是单位状态。
`FireMission.phase` 和 `BoardingTransition.status` 也是独立 runtime 状态机。

## 2. 状态转换图

### 2.1 生存与部署轴

```text
NOT_DEPLOYED
  -> DEPLOY action (legacy deployment path)
  -> DEPLOYED / operational

any alive state
  -> committed losses reduce blood to 0
  -> alive=false + state=destroyed

carrier destroyed with mounted passenger
  -> boarding cascade in working WorldState
  -> passenger blood=0 + alive=false + deployed=false + state=destroyed
```

没有轻伤/重伤 enum；存量损伤仅由离散 `blood/health` 表达。

### 2.2 机动轴

```text
STOPPED
  -> MOVE accepted
  -> MOVING (or state=MARCHING when movement_mode=MARCHING)

MOVING
  -> explicit STOP_MOVE / path exhausted / suppression stop / blocked
  -> TRANSITIONING_TO_STOP
  -> timer completes
  -> STOPPED or SUPPRESSED

MOVING
  -> segment legality/stack conflict fails
  -> BLOCKED
  -> TRANSITIONING_TO_STOP

SUPPRESSED + stopped
  -> suppression timer reaches zero
  -> STOPPED

STOPPED
  -> ENTER_MARCHING / EXIT_MARCHING
  -> TRANSITIONING
  -> timer completes and movement_mode changes
  -> STOPPED
```

关键点：`DEPLOYED` 不是 `MOVING/STOPPED` 的同层状态；它是独立在场布尔轴。
`MARCHING` 的持续事实是 `movement_mode=MARCHING`，因为停止后 `state` 会回到
`STOPPED`。

### 2.3 武器姿态轴

```text
STOPPED + no transition
  -> WEAPON_LOCK
  -> TRANSITIONING (weapon_unfolded=false immediately)
  -> weapon_locked=true + STOPPED

STOPPED + no transition
  -> WEAPON_UNFOLD
  -> TRANSITIONING
  -> weapon_unfolded=true + weapon_locked=false + STOPPED

accepted fire
  -> weapon_cooldown_remaining_ms > 0
  -> MovementRuntime countdown
  -> 0 / READY
```

`READY` 当前不是显式状态；它是 alive、deployed、未锁、未转换、弹药、cooldown
以及对应武器规则共同产生的能力结论。

### 2.4 压制轴

```text
alive target
  -> committed suppression effect
  -> state=SUPPRESSED + suppression_remaining_ms=policy duration

SUPPRESSED + personnel moving
  -> stop requested at movement boundary
  -> TRANSITIONING_TO_STOP
  -> SUPPRESSED while timer remains

SUPPRESSED + stopped + timer=0
  -> STOPPED
```

重复接受直瞄/引导 fire 时，已处于 SUPPRESSED 的目标会重置完整 timer。

### 2.5 Boarding 轴

```text
passenger INDEPENDENT/DEPLOYED + carrier empty/stopped
  -> BOARD accepted
  -> passenger ON_BOARD immediately
     mounted_on=carrier, deployed=false, current_hex=None
  -> carrier passenger_id=passenger, movement_locked=true
  -> BOARD transition ACTIVE (75 s)
  -> BOARD COMPLETED
  -> carrier movement_locked=false; passenger remains ON_BOARD

stable ON_BOARD + carrier stopped/unsuppressed
  -> UNLOAD accepted
  -> passenger remains ON_BOARD
  -> carrier movement_locked=true
  -> UNLOAD transition ACTIVE (75 s)
  -> UNLOAD COMPLETED
  -> relation cleared
  -> alive passenger: ACTIVE + deployed=true + carrier current_hex
  -> destroyed passenger: destroyed + deployed=false + no hex
```

Board/Unload timer 状态属于 `WorldState.boarding_transitions`，没有引入
`EMBARKING/DISEMBARKING` 单位枚举，符合当前冻结语义。

## 3. 隐蔽系统审计

### 3.1 当前真实链路

```text
target.raw_attributes.concealed
  + target terrain (FOREST/SETTLEMENT)
  + observation profile/domain
  + evaluate_line_of_sight()
  -> evaluate_visibility()
  -> observer.raw_attributes.see_enemy_bop_ids
  -> detached RED/BLUE observation and valid SHOOT targets
```

证据：

- `visibility/observation.py::evaluate_visibility`：concealed 将观察距离减半；
  FOREST/SETTLEMENT 再次减半；最终还要求 LOS。
- `visibility/line_of_sight.py::evaluate_line_of_sight`：逐 hex ray 检查中间高程和
  FOREST/SETTLEMENT 的 intervening height bonus。
- `core/engine.py::_refresh_source_observation`：逐 observer 生成
  `see_enemy_bop_ids`。
- `adjudication/direct_fire.py` 与 `adjudication/modifiers.py`：可观察是合法性，
  concealed 也进入目标状态修正。

没有发现独立的 `hidden`、`camouflaged`、`stealth`、全局 `observed` 或
`detected_by_side` 权威字段。

### 3.2 应采用的状态边界

不推荐方案 A（把单位设为全局 `VISIBLE/CONCEALED`）。同一目标可同时被 RED
某观察者发现、未被另一观察者发现，VISIBLE 不是世界级单值。

推荐分层契约：

```text
Authoritative unit posture:
  concealment posture / concealed flag

Derived observation relation:
  observer_id(or side) -> detected target IDs + evidence + observation version
```

即隐蔽姿态属于单位/环境事实，被发现属于 Observation 关系。当前架构方向符合
这个边界，但 `concealed` 仍藏在 `raw_attributes`，缺少版本化 typed authority、
来源证据和明确转换。

### 3.3 解除隐蔽规则

当前实现中 MOVE、FIRE、BOARD、UNLOAD、被发现、受到攻击均**不会自动清除**
`raw_attributes.concealed`。未找到对应 Event 或 timer。

这些事件是否解除隐蔽：**UNKNOWN**。在权威规则冻结前不得按经验补充。

## 4. 战斗状态审计

### 4.1 压制影响

| 系统 | 当前行为 |
|---|---|
| MOVE | 拒绝开始；移动中的人员在到达 segment 边界后停车 |
| DIRECT FIRE | 不直接拒绝；以 `attacker_suppressed` 进入机器表修正 |
| GUIDED FIRE | guider 是否被禁止由 GuidanceProfile 决定；当前验证读取 timer |
| INDIRECT FIRE | 当前 capability blocker 只显式检查 cooldown；未统一检查 suppression/moving/unfolded |
| BOARD | passenger 和 carrier 均要求未压制 |
| UNLOAD | carrier 要求未压制；passenger 保持 ON_BOARD |

因此压制不是统一的“禁止所有动作”状态，而是每种 capability 的规则输入。

### 4.2 损伤与损毁

当前支持：

- `blood/max_blood`：离散规则强度；
- `health/max_health`：由 blood 比例同步的连续展示值；
- `alive`；
- `destroyed`；
- `SUPPRESSED`。

当前不支持或没有权威契约：

- `LIGHT_DAMAGE`、`HEAVY_DAMAGE` 等伤情 enum；
- incapacitated/disabled/abandoned；
- 维修和恢复损伤；
- 独立 morale/rout 状态。

上述均标记 **UNKNOWN/MISSING**，不应从 blood 比例自行划档。

### 4.3 已确认的一致性风险

1. `guided_fire_recovery_remaining_ms` 会在能力验证中阻塞 guider/shooter，且提交后
   被设为非零，但当前代码未找到任何 runtime countdown。一次 GUIDED 后可能永久
   `RECOVERING`。这是明确的生命周期实现缺口，建议下一阶段首先增加回归探针，
   不在本轮修复。
2. `state` 与 timer/boolean 可表达矛盾组合，`OperatorState.__post_init__` 仅校验
   局部数值，没有统一验证诸如 `SUPPRESSED <-> suppression_remaining_ms`、
   `ON_BOARD <-> mounted_on/deployed/current_hex` 的完整不变量。Boarding restore
   有额外关系检查，但普通 tick 构造入口没有统一状态规范器。
3. live Boarding 使用 typed `passenger_id/mounted_on`；Direct Fire 的兼容
   `carried_passengers` 仍从 `raw_attributes.passenger_ids` 读取。两条关系来源并存，
   可能造成历史关系证据和 Native live relation 的伤害语义不一致。当前 carrier
   destruction cascade 使用 typed relation，普通 carrier damage policy应继续由
   已冻结规则决定。

## 5. 机动状态审计

### 5.1 权威判定

推荐按当前实现理解：

```text
moving          := segment_to_hex is not None
fully stopped   := stopped=true
                   and moving=false
                   and speed=0
                   and move_to_stop_remaining_ms=0
                   and transition_kind is None
                   and transition_remaining_ms=0
deployed        := deployed=true (independent axis)
```

Boarding 已通过 `_boarding_operator_stopped()` 使用完整 fully-stopped 条件，
没有仅依赖 `state` 字符串。

### 5.2 动作约束

| 动作 | 当前机动约束 |
|---|---|
| MOVE | 无 active segment/path/transition；具备合法 domain/speed；非压制 |
| STOP_MOVE | 必须存在 active segment |
| DIRECT FIRE | 由 weapon `can_fire_while_moving` 决定；转换中禁止 |
| GUIDED FIRE | profile/weapon 分别约束 guider 和 shooter 的停止/展开状态 |
| INDIRECT FIRE | 当前 capability 未统一约束 stopped/moving，规则是否要求停止需单独冻结 |
| BOARD/UNLOAD | carrier fully stopped；BOARD passenger 也 fully stopped |
| WEAPON_LOCK/UNFOLD | 必须 fully stopped 且无其他 transition |
| WAIT | 正常状态和 boarding lock 期间均允许 |

`DEPLOYED` 不能作为 `STOPPED` 同义词；`weapon_unfolded` 也不能作为 fire-ready
同义词。

## 6. Boarding 状态一致性

### 6.1 已正确闭合

- BOARD capability 与 request 都从 detached pre-state 重新验证；
- passenger 上车后 `deployed=false/current_hex=None`，不会作为独立 observer；
- `_source_valid_actions` 排除 undeployed 和 mounted passenger；
- visibility refresh 排除 undeployed observer/target；
- common action router 拒绝 mounted actor；
- 75 秒 transition、snapshot/restore/hash、事件和 strict replay 已覆盖；
- UNLOAD 后位置继承 carrier hex，并在同一成功 tick 后刷新 observation；
- carrier 被摧毁时 typed passenger 在 working state 同步 destroyed；
- carrier 普通受损不会通过 typed Boarding cascade 自动伤害 passenger。

### 6.2 需要继续加固

- `ON_BOARD` 仍同时依赖 `state`、`mounted_on`、`deployed`、`current_hex` 四个字段；
- carrier/passenger 双向关系只能容纳一个 passenger，符合当前 v1，但不是容量模型；
- completed transitions 长期保存在 WorldState，属于审计历史而非 active state；
- live typed relation 与历史 `raw_attributes.passenger_ids/car/on_board` 的规范化边界
  尚未统一；
- carrier 被毁后 relation 是否应清除、completed/active transition 如何终止，目前
  仍保留关系供证据和 cascade 使用，没有独立 cancellation state；
- 上车乘员不独立观察、不生成 action、不作为 deployed target；这符合当前冻结语义。

## 7. 八因子兼容性

`EightFactorAnalyzer` 是 GREEN frame 上的 detached、非权威分析器。契约读取字段：

| Factor | 读取字段 | 当前 Unit State 支撑 |
|---|---|---|
| Readiness | `weapon_cool_time`, `weapon_unfold_state` | 基本可用；未纳入 transition、guided recovery、ammo |
| Mobility | `move_state`, `stop`, `can_to_move`, `on_board` | 部分可用；exporter 输出 mode/stop/on_board，但 `can_to_move` 可能沿用 source raw 值，未显式映射 movement_locked/transition/suppression |
| Information | `see_enemy_bop_ids` | 可用；来自每 tick observation refresh |
| Status | `keep`, `tire`, `lose_control`, `close_combat` | **PARTIAL/UNKNOWN**；Native suppression/fatigue 没有完整映射到 keep/tire，lose_control 无 Native 状态机，close combat 只读无转换 |

因此当前字段足以提供历史兼容的八因子候选分析，但不足以宣称完整、统一的 Native
Unit State -> Reward/Factor 语义。缺失字段按现有分析契约会标为 UNKNOWN；不应从
相邻字段推断。

## 8. 缺失状态

以下按证据分级，而不是按军事经验补齐：

### 明确实现缺口

1. Guided recovery countdown：字段已授权并参与 capability 阻塞，但没有推进逻辑。
2. Canonical state invariant validator：缺少跨字段统一校验和明确错误码。
3. Typed concealment authority：现有 `raw_attributes.concealed` 已影响规则，却没有
   版本、来源或生命周期契约。
4. Eight-factor export alignment：新的 suppression/fatigue/boarding lock/recovery
   没有完整映射到分析输入。

### MISSING（代码中没有）

- 全局 `UnitState` enum 或由正交轴组成的 typed lifecycle view；
- per-side detection ledger/history（当前只有 current per-observer contact list）；
- concealment transition/event/timer；
- disabled/abandoned/repair/rout 状态；
- boarding capacity/multiple passengers；
- close-combat Native transition；
- unload interruption/cancellation runtime。

### UNKNOWN（规则未冻结）

- MOVE/FIRE/BOARD/UNLOAD/受击是否解除 concealment；
- detected 是否持续记忆以及遗忘时间；
- indirect fire 在 suppression/moving 状态下的 execution blockers；
- carrier destruction 后 relation 和 transition 的最终清理策略；
- CLOSE_COMBAT 的进入、退出和同 tick conflict 语义；
- damage tier 和恢复规则。

## 9. 推荐下一阶段实现顺序

### Phase 1 — Freeze and test existing invariants

不改规则，只把现状锁定为可验证契约：

1. 增加只读 `OperatorLifecycleView`（或等价 derived model），从权威字段生成
   operational、movement、combat、boarding、weapon、observation posture 六个轴；
2. 增加跨字段 invariant audit，不自动修正状态；
3. 为每个现有转换增加表驱动测试，特别是 suppression、boarding、destroyed；
4. 增加 guided recovery countdown 的失败回归，确认期望 timer 后再修复。

### Phase 2 — Normalize state vocabulary without replay rewrite

保留旧 `state` 字符串序列化，新增内部 typed/derived enum；旧 Replay 仍按旧字段读取。
不要直接替换 `OperatorState.state`，也不要改变历史 action/state hash。

### Phase 3 — Concealment authority contract

先从规则证据冻结：姿态来源、解除事件、持续时间和事件证据。然后将 typed
concealment posture 作为权威单位属性，Observation 继续保持 per-observer 派生；
不得建立全局 VISIBLE 状态。

### Phase 4 — Capability state integration

逐项审计 Direct/Guided/Indirect/Boarding 对同一 derived lifecycle view 的依赖，
只消除不一致，不改变已冻结裁决结果。优先处理 guided recovery 和 indirect fire
状态 blocker。

### Phase 5 — Analysis projection alignment

在不改变 EightFactorAnalyzer 非权威地位的前提下，为 Native 状态提供显式、版本化
GREEN projection。UNKNOWN 继续 fail closed，不实现 Reward Model。
