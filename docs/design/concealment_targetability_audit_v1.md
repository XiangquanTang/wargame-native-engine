# Concealment Targetability Audit v1

## 1. 审计结论

**结论：PASS。**

当前 Native simplified 实现符合本次待确认的核心语义：

> 隐蔽是单位姿态（posture），不是不可见状态，也不是免疫攻击状态。

`CONCEALED` 会参与观察距离和裁决修正，但不会直接产生“不可发现”、
“不可选为目标”或“免疫伤害”的短路结果。只要目标在当前观察者的 LOS、
修正后观察距离以及武器合法性约束内，它仍可被发现并接受直瞄或引导射击；
间接火力则按落点占用关系纳入单位，不因隐蔽姿态跳过。

本轮是只读规则一致性审计。除本文档外未修改 Engine、规则、Action、Replay
或测试。

## 2. 审计范围与判定边界

审计覆盖：

- `ConcealmentPosture`、`OperatorState.concealed` 及旧字段兼容投影；
- `evaluate_visibility()`、观察关系刷新及 RED/BLUE observation 投影；
- DIRECT FIRE capability、请求验证、修正和伤害应用；
- GUIDED FIRE capability、guider 可见性、shooter/guider 暴露语义；
- INDIRECT FIRE scatter、impact occupant selection、damage effect；
- Source Replay 的 ordered action stream 和 strict replay。

不在本次审计中补充或推断以下 UNKNOWN 规则：被发现自动暴露、受伤自动暴露、
间接火力导致发射方暴露、地形隐蔽等级、夜间/天气、接触记忆和重新隐蔽时间。

## 3. 当前 Concealment 数据流

### 3.1 权威姿态

`src/wargame_native_engine/core/concealment.py:9-13` 定义：

```text
ConcealmentPosture.EXPOSED
ConcealmentPosture.CONCEALED
```

该枚举的注释明确说明：姿态是 authoritative unit posture，而 visibility
仍然是 observer-relative。

`src/wargame_native_engine/core/operator.py:68` 在 `OperatorState` 上保存 typed
`concealment_posture`；`operator.py:180-203` 通过只读属性 `concealed` 暴露布尔
语义，并由 `set_concealment_posture()` 同步旧兼容字段：

```text
concealment_posture == CONCEALED
        ↓
OperatorState.concealed == True
        ↓ compatibility projection
raw_attributes["concealed"] == True
```

`operator.py:150-165` 仅在构造边界将历史
`raw_attributes["concealed"]` 提升为 typed posture。运行时规则读取
`target.concealed`，没有继续以 raw 字段作为授权来源。

### 3.2 姿态变化

`ENTER_CONCEALMENT` 经 action stream 进入 Engine 的 posture staging；成功提交后
设置 `CONCEALED` 并产生 `UNIT_CONCEALED`。成功 MOVE 和发射平台主动开火通过
`_expose_operator()` 变为 `EXPOSED`。

这一路径修改的是姿态，不修改生命周期 `state` 为 `CONCEALED`，因此没有把
“隐蔽”和 `ACTIVE`、`SUPPRESSED`、`ON_BOARD`、`DESTROYED` 混成同一状态轴。

## 4. Visibility / Detection 数据流

当前 Source-compatible 发现链为：

```text
WorldState OperatorState
  ↓ target.concealed
NativeEngine._refresh_source_observation()
  ↓ evaluate_visibility(observer, target)
LOS + observation profile + concealment modifier + terrain modifier
  ↓ VisibilityResult.observable
observer.raw_attributes["see_enemy_bop_ids"]
  ↓ side-owned union/projection
SideObservation.visible_enemies
```

关键实现：

- `visibility/observation.py:41-58`：先取得 LOS evidence；
- `visibility/observation.py:59-61`：取得 observer/target domain 的基础观察距离；
- `visibility/observation.py:67-74`：隐蔽通常将观察距离减半；高位观察车辆时按
  已有规则忽略该隐蔽修正；
- `visibility/observation.py:75-79`：森林/居民地是独立的距离修正；
- `visibility/observation.py:80-90`：最终仍以 LOS、修正后距离和目标域共同计算
  `observable`，不是 `concealed => false`；
- `core/engine.py:5435-5475`：逐 observer/target 调用同一个 evaluator，仅在
  `result.observable` 时写入当前发现关系；
- `core/engine.py:358-374`：可见敌方投影为 `visible_enemies`，敌方 raw attributes
  被剥离，不泄露隐藏的内部字段。

### 4.1 “未发现”与“已发现但隐蔽”的区分

当前模型在权威语义上有明确区分：

```text
target.concealment_posture    = 目标自身姿态
VisibilityResult.observable  = 某 observer 对该目标的本次发现结果
see_enemy_bop_ids             = 每个 observer 当前发现关系
```

因此 RED 发现 BLUE 不会把 BLUE 的 `concealment_posture` 全局改成 EXPOSED。
同一目标可以保持 CONCEALED，同时被某个近距离/高位观察者发现、被另一个观察者
漏报。SideObservation 不向敌方观察者披露目标 raw `concealed` 字段；这属于信息
隔离，不等于系统无法区分两种事实。

### 4.2 Tactical Overlay 边界

`core/engine.py:3226-3254` 的 tactical visibility field 显式传入
`target_concealed=False`，其 schema 语义为
`observable_unconcealed_ground_target`。它表达未隐蔽假想目标的 Sensor Coverage，
不是当前真实 contact 集，不应被用来判断某个隐蔽敌方已经被发现。

**Visibility 判定：PASS。** 隐蔽是可观察性修正，不是全局 invisible flag。

## 5. Direct Fire 数据流

当前链路：

```text
shooter.see_enemy_bop_ids
  ↓
NativeEngine._source_valid_actions()
  ↓ SHOOT(actor, target, weapon_id)
NativeEngine._direct_fire_request()
  ↓ recompute evaluate_visibility(target_concealed=target.concealed)
DirectFireAdjudicator.validate()
  ↓ target_observed + range + weapon/state checks
DirectFireAdjudicator stage/resolve
  ↓ table-driven target_concealed roll modifier
aggregate effects
  ↓
working WorldState / atomic commit
```

关键证据：

- `core/engine.py:5312-5341`：SHOOT 目标来自 shooter 当前发现列表；循环没有
  `concealed` 排除条件；
- `core/engine.py:4863-4875`：请求阶段重新调用权威 visibility evaluator，并传入
  `target.concealed`；
- `adjudication/direct_fire.py:42-106`：合法性检查包含 alive、敌我、武器、弹药、
  target domain、`visibility.observable`、射程及发射方状态；不存在独立的
  concealed 拒绝项；
- `adjudication/modifiers.py:18-50`：目标隐蔽只生成机器表定义的
  `target_concealed` roll modifier；
- `core/engine.py:4684-4757`：聚合损失、压制和摧毁应用不检查目标是否隐蔽。

所以隐蔽目标只要已经被 shooter 合法发现，仍会生成对应 weapon/target 的 SHOOT
动作，进入验证和伤害裁决。

**Direct Fire 判定：PASS。**

## 6. Guided Fire 数据流

当前链路：

```text
guider.see_enemy_bop_ids
  ↓
_guided_fire_capabilities_from_pre_state()
  ↓ guider + shooter + weapon + target
_guided_fire_visibility()
  ↓ evaluate_visibility(target_concealed=target.concealed)
Guided validation / resolution
  ↓ shared DirectFire effect model
aggregate effects / atomic commit
```

关键证据：

- `core/engine.py:2754-2818`：能力生成使用 guider 自身的发现列表；目标筛选没有
  concealed 排除条件；
- `core/engine.py:2820-2842`：精确证据使用同一个 visibility evaluator，并传入
  目标姿态；
- `core/engine.py:2735-2752`：验证要求 guider 的 `visibility.observable`，但不要求
  shooter LOS，也不以 concealed 单独拒绝；
- `core/engine.py:4591-4610`：GUIDED 成功后只将实际发射平台 shooter 暴露，guider
  仅提供目标获取证据并保持原姿态；
- Guided 的命中/毁伤复用现有 DirectFire 效果模型，因此被发现的隐蔽目标仍会
  接受裁决。

目标隐蔽可能通过 guider 的观察距离修正使其“未被 guider 发现”；这属于发现阶段
失败，不是攻击免疫。如果修正后仍然 observable，GUIDED capability 可以形成。

**Guided Fire 判定：PASS。**

## 7. Indirect Fire 数据流

当前链路：

```text
FIRE(INDIRECT, planned_hex)
  ↓ mission / scatter
impact_hex
  ↓ alive + deployed + current_hex == impact_hex
affected_operator_ids (operator_id ascending)
  ↓ IndirectFireTargetEvidence(concealed=target.concealed)
resolve_indirect_fire_effects()
  ↓ table-driven target_concealed modifier
EffectBundle
  ↓ atomic commit
WorldState / events / strict replay
```

关键证据：

- `core/engine.py:1765-1771`：首次 impact 选取该 hex 内所有 alive、deployed 单位，
  不按 concealed 过滤；
- `core/engine.py:1863-1910`：D-2A 再验证 occupants，并把姿态作为 target evidence，
  而不是剔除条件；
- `adjudication/indirect_fire.py:693-790`：按 operator ID 顺序处理所有
  `affected_operator_ids`；
- `adjudication/indirect_fire.py:993-1029`：concealed 只产生表驱动
  `target_concealed` modifier；
- `core/engine.py:4454-4504`：ACTIVE 区域对首次进入 impact hex 的 alive、deployed
  单位执行 effect pipeline，没有隐蔽免疫分支。

Scatter 不读取目标姿态；impact occupant selection 不读取姿态；damage resolution
读取姿态仅用于明确的裁决修正。因此隐蔽单位仍属于 affected units 并接受伤害
裁决。

**Indirect Fire 判定：PASS。**

## 8. 发现、瞄准、伤害三个阶段的分离

| 阶段 | 权威输入 | Concealment 用途 | 是否构成免疫 |
|---|---|---|---|
| 发现 | LOS、domain observation profile、距离、地形、target posture | 调整观察距离 | 否 |
| 直瞄目标资格 | shooter 的当前发现关系 + 服务端重新验证 visibility/range/weapon | 可导致本 observer 当前未发现；无独立拒绝 | 否 |
| 引导目标资格 | guider 的当前发现关系 + 精确 visibility evidence | 可导致 guider 当前未发现；无独立拒绝 | 否 |
| 间接落点筛选 | impact hex 内 alive/deployed occupants | 不参与 occupant selection | 否 |
| 伤害 | Native machine table、目标状态/地形等 evidence | 作为显式 roll modifier | 否 |

当前实现没有把“未发现”“不能瞄准”“不能受伤”压缩为一个 `concealed` 布尔短路。

## 9. Replay 数据流

`session/source_environment.py:119-130` 将每步真实 ordered actions 与期望 state hash
写入 step record。`io/replay.py:320-430` 在 strict replay 中：

1. 在 step 0 前检查 adjudication semantics（以及存在 INDIRECT action 时的相应
   authority hashes）；
2. 从 `ordered_actions` 逐项重建 `EngineAction`；
3. 调用正式 `step_views_ms()` 重新执行 Engine；
4. 比较 ordered actions、action sequences、state hash 和 GREEN frame hash。

`ENTER_CONCEALMENT` 因而通过 action stream 重新执行；之后的 MOVE、DIRECT、
GUIDED、INDIRECT 也走各自正式裁决链。未发现 replay step 通过直接恢复
`concealed=true` 来替代动作执行的分支。

历史初始状态可以在 `OperatorState` 构造边界从 raw `concealed` 提升 posture；这是
初始/历史状态兼容，不是对后续 `ENTER_CONCEALMENT` action 的静默推断。

**Replay 判定：PASS。**

## 10. 搜索到的反模式

本次对 Python 运行代码搜索了：

```text
concealment_posture
raw_attributes["concealed"]
concealed
visibility
see_enemy_bop_ids
observation
```

未发现以下反模式：

```text
if concealed: target invisible
if concealed: cannot detect unconditionally
if concealed: cannot fire
if concealed: skip impact damage
```

发现的 `concealed` 条件均属于：

- construction-boundary legacy promotion；
- visibility observation-limit modifier；
- Direct/Indirect table-driven roll modifier；
- posture capability/transition；
- replay/adjudication evidence serialization。

## 11. 发现的问题与风险

### P0

无。

### P1

无。未发现隐蔽导致目标全局不可见、不可攻击或免疫毁伤的规则断链。

### P2

1. **目标可攻击性的显式回归覆盖不均衡。** Guided 测试大量使用
   `target_concealed=True`，Visibility 也覆盖观察距离修正；但缺少一个集中测试将
   “隐蔽目标在修正后观察距离内”分别贯穿 Direct capability/伤害和 Indirect
   affected/damage。当前结论有明确静态代码证据，但未来重构时回归保护仍可加强。

2. **typed posture 的持久化仍通过 legacy raw projection 完成。**
   `OperatorState.to_dict()` 当前没有单独输出 `concealment_posture`，而
   `set_concealment_posture()` 会同步 `raw_attributes["concealed"]`，反序列化再从
   raw 提升。现有 snapshot/hash/replay 行为一致，但 typed authority 的序列化
   可审计性弱于显式字段。由于新增显式字段可能改变历史 world hash，本轮不建议
   在没有版本与兼容评估时修改。

3. **SideObservation 不提供“已发现但仍处于隐蔽姿态”的公开标记。** 可见敌方的
   raw attributes 被有意剥离。这不影响合法动作或裁决，也符合信息隔离；如果未来
   UI 需要展示 contact quality，应先冻结独立、非泄密的观察证据契约，不能直接
   暴露 GREEN posture/raw 数据。

4. **Tactical Visibility Field 与真实 contact 的语义容易被误读。** Overlay 是
   未隐蔽假想目标的理论 Sensor Coverage；真实 contact 仍需读取 observation。
   当前 schema 已声明该语义，文档/UI 应继续保持区分。

## 12. 是否需要修改

**当前核心规则无需修改。** 本次审计目标已经满足。

若后续进行最小加固，建议仅按以下顺序处理，且单独评估历史 hash/replay 影响：

1. 增加“隐蔽但在修正观察距离内仍可见并可被 Direct Fire”回归测试；
2. 增加“隐蔽 occupant 仍进入 Indirect affected units/damage”回归测试；
3. 若确需显式序列化 typed posture，先建立版本化兼容策略，禁止直接改变旧 replay
   的初始/最终 hash 解释；
4. UI 若展示姿态证据，新增经过 side-policy 约束的 contact evidence，不暴露敌方
   raw attributes。

上述均是测试/契约加固建议，不代表当前 targetability 语义 FAIL，也不应在本轮
实施。
