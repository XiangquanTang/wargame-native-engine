# Native 兵棋裁决知识库（Final）

> 适用项目：当前 `wargame_native_engine` 源码仓库
> 机器主表：`native_adjudication_tables_final.json`  
> 完整原文：`推演规则_完整纯文本_Final.md`  
> 本文件是 Codex 的首读说明；实现时不再读取原 PDF。

---

## 1. 使用顺序

Codex 必须按以下顺序读取：

1. `Native兵棋裁决知识库_Final.md`：规则语义、实现流程、字段映射和边界；
2. `native_adjudication_tables_final.json`：唯一机器可读裁决表来源；
3. `推演规则_完整纯文本_Final.md`：仅在需要核对上下文时读取。

禁止：

- 使用兰彻斯特八因子参与命中、伤害或战损修正；
- 使用连续 `damage = base_damage × modifier` 替代裁决表；
- 对表格插值、平滑、拟合或静默补值；
- 通过历史复盘反推缺失规则；
- 将 `96.json.valid_actions` 当成规则真值；
- 未确认的 Source 数字码不得按单位名称猜测。

来源优先级：

```text
上传的完整规则文本
> 用户人工确认的表格勘误
> 明确标记的 Native 实现约定
> PDF OCR / 文本层
```

---

## 2. 引擎基本模型

### 2.1 时间与执行方式

原规则采用实时制：

```text
红蓝双方连续观察、连续下达命令
→ 引擎同时接收命令
→ 先到先裁
→ 动作计时完成后进入事件队列
```

Native 的 Source-compatible 模式应采用：

```text
CommandScheduler
→ ActionTimer
→ EventQueue
→ TableAdjudicator
→ StateMutation
→ NativeReplay evidence
```

不要用同步回合毁伤替代该模式；原有 `native_simplified` 同步模式可继续独立保留。

### 2.2 车/班数是主战损变量

```text
blood      = 当前剩余车辆数或班组数
max_blood  = 初始/最大车辆数或班组数
```

裁决结果是：

```text
NO_EFFECT
SUPPRESS
LOSS_N
DESTROYED
```

而不是连续浮点 HP。

### 2.3 关键计时

| 状态或动作 | 时长 |
|---|---:|
| 地面机动转停止 | 75 秒 |
| 空中机动转悬停 | 75 秒 |
| 武器锁定 | 75 秒 |
| 武器展开 | 75 秒 |
| 直瞄武器冷却 | 75 秒 |
| 上车/下车 | 75 秒 |
| 转掩蔽 | 75 秒 |
| 引导射击后恢复 | 75 秒 |
| 压制持续 | **150 秒** |
| 间瞄飞行 | 150 秒 |
| 间瞄爆炸区持续 | 300 秒 |
| 间瞄冷却 | 300 秒 |
| 同格交战重复射击 | 25 秒 |

再次压制：

```text
人员：额外损失 1 班，继续压制，计时重置为 150 秒
车辆：重新计满 150 秒
```

车辆被压制时，车内人员不继承压制，但不能下车。

---

## 3. Source 标准帧对齐

标准帧是兼容契约，不是隐藏规则来源。

### 3.1 必须保留的关键字段

**身份与类别**

```text
obj_id, color, type, sub_type, name, owner
```

**位置与机动**

```text
cur_hex, cur_pos, altitude, basic_speed, speed,
move_state, stop, move_path, move_to_stop_remain_time
```

**战损与状态**

```text
blood, max_blood, armor, stack, keep,
keep_remain_time, close_combat, alive_remain_time
```

**武器与观察**

```text
carry_weapon_ids, remain_bullet_nums,
weapon_cool_time, weapon_unfold_time,
weapon_unfold_state, observe_distance, see_enemy_bop_ids
```

**关系**

```text
passenger_ids, car, launcher, launch_ids,
on_board, fort, in_fort
```

### 3.2 关键解释

```text
cur_hex  = 当前裁决格
cur_pos  = 当前路径段中的格内进度
move_path = 后续路径

blood/max_blood = 离散车/班数
observe_distance = Source 索引数组，进入 Native 后转为具名 Profile
valid_actions = 兼容输出，Native 必须自行重新计算
judge_info = 兼容槽位，Native 输出版本化裁决证据
```

当单位离开上一格中心但尚未到达下一格中心时，射击或被射击仍以上一格为裁决依据。

### 3.3 尚缺的数字码映射

这些字段不影响纯表单元测试，但会阻塞 `96.json → Native WorldState` 的完全自动转换：

```text
armor 数字码 → 复合/重型/中型/轻型/无装甲
完整 type/sub_type 枚举
弹药槽 0/100/101/102 → 弹药类别
keep/keep_remain_time → 具体状态语义
observe_distance 索引 → 目标类型
```

未确认前统一输出 `UNKNOWN`。

---

## 4. 通视与观察

### 4.1 通视

```text
观察格中心与目标格中心连线
→ 两端高程线性插值
→ 高处观察点增加 1 个高程等级
→ 中间居民地/丛林地增加 1 个高程等级
→ 中间格修正高程高于视线则阻挡
→ 相邻格始终通视
```

高看低原则仅用于通视观察点高度；直瞄攻击等级高差修正另行处理。

### 4.2 观察距离

基础矩阵：

| 观察方 \ 目标 | 步兵 | 车辆 | 直升机 | 无人机/巡飞弹 |
|---|---:|---:|---:|---|
| 步兵 | 10 | 25 | 25 | 当前/相邻格 |
| 车辆 | 10 | 25 | 25 | 当前/相邻格 |
| 直升机 | 10 | 25 | 25 | 当前/相邻格 |
| 无人机/巡飞弹 | 2 | 2 | 不可观察 | 不可观察 |

修正：

```text
目标处于掩蔽：被观察距离减半
目标位于居民地/丛林地：被观察距离减半
车辆低于观察者：车辆掩蔽无效
```

通视和观察必须分开返回证据。

---

## 5. 直瞄动作合法性

按顺序检查：

```text
攻击方和目标存在、存活
→ 敌对阵营
→ 武器属于攻击方
→ 弹药可用
→ 武器支持目标类型
→ 攻击方观察到目标
→ 目标在射程内
→ 武器已展开
→ 冷却结束
→ 当前单位状态允许射击
→ 无行进间能力的地面单位必须停止
→ 空中单位必须悬停
→ 同格交战的外部直瞄限制
```

输出：

```text
legal
reason_code
distance
observation_evidence
weapon_evidence
state_evidence
```

不能只返回布尔值。

---

## 6. 直瞄裁决

### 6.1 高差攻击等级修正

人工确认的最终规则：

```text
低处打高处：直接使用高差表中的负修正
高处打低处：0
双方等高：0
表格空白：0
高度差 7 / 距离 12：-1
```

表外组合不得外推。

### 6.2 对人员

```text
武器类型 + 攻击方 blood + 距离
→ 基础攻击等级
→ 低打高时修正攻击等级
→ 2D6 查基础战果
→ 另投 1D6
→ 累计射击方状态、目标地形、目标状态修正
→ 得到结果修正 -1 / 0 / +1
→ 修正基础战果
```

数字战果：

```text
final_losses = clamp(base_losses + result_modifier, 0, target_blood)
```

例：

```text
基础损失 2，修正 -1 → 1
基础损失 2，修正  0 → 2
基础损失 2，修正 +1 → 3
```

符号战果采用显式 Native 约定：

```text
NO_EFFECT < SUPPRESS < LOSS_1 < LOSS_2 < LOSS_3 < LOSS_4
```

按 `-1/0/+1` 移动并在边界截断。该顺序必须标记：

```text
NATIVE_IMPLEMENTATION_CONVENTION
```

### 6.3 对车辆

```text
武器类型 + 距离
→ 基础攻击等级
→ 低打高时修正攻击等级
→ 目标 blood + 攻击等级选择车辆基础战果列
→ 2D6 得到基础损失
→ 另投 2D6
→ 累计射击方状态、目标地形、目标状态修正
→ 按装甲查损失修正
→ final_losses = clamp(base_losses + armor_loss_correction, 0, target_blood)
```

车辆基础战果表空白：

```text
0 辆损失
```

数字损失同时对车辆施加压制。

车辆搭载人员时：

```text
车辆损失 N 辆
→ 搭载人员同步损失 N 班
→ 均不得低于 0
```

### 6.4 步兵轻武器对车辆

必须使用三张表：

```text
攻击等级：步兵轻武器对车辆专表
基础结果：直瞄武器对人员/步兵轻武器对车辆结果表
损失修正：车辆战损结果修正表
```

步兵轻武器在声明射程内的空白攻击等级：

```text
攻击等级 0
```

超过声明射程才是 `OUT_OF_RANGE`。

---

## 7. 间瞄裁决

### 7.1 计划与时间

```text
下达计划
→ 150 秒飞行
→ 散布裁决
→ 300 秒爆炸区
→ 进入爆炸区的单位受到一次裁决
→ 300 秒冷却
```

对己方同样有效。

### 7.2 校射等级

```text
无校射：无法观察目标格
格内校射：能观察目标格，但不能观察格内目标
目标校射：能观察目标格和目标
```

### 7.3 散布结果

```text
命中：命中目标格和格内目标
散布：命中目标格，但未命中格内目标
散布 N 格：实际命中格距离计划格 N 格
```

JSON 已包含三套散布表：

```text
indirect_fire.unobserved_dispersion
indirect_fire.grid_corrected_dispersion
indirect_fire.target_corrected_dispersion
```

散布方向在原文中未定义，必须作为独立策略配置，不得假装为原规则。

### 7.4 战果

JSON 已包含：

```text
indirect_fire.hit_target_result
indirect_fire.off_target_result
indirect_fire.result_modifiers
```

修正因素：

```text
目标地形
目标状态
装甲防护
火炮数量
```

---

## 8. 防空裁决

```text
防空武器 + 距离
→ 攻击等级
→ 2D6
→ 歼灭 / 未歼灭
```

一次防空动作的独立裁决次数等于当前 `blood`。

JSON：

```text
anti_air.weapon_attack_levels
anti_air.combat_results
```

防空高炮：

```text
射程 20 格
每车最多射击 10 次
冷却 15 秒
```

防空导弹小队：

```text
射程 20 格
每班最多射击 4 次
冷却 75 秒
```

车载防空导弹：

```text
射程 50 格
每车最多射击 4 次
冷却 15 秒
```

---

## 9. 雷场裁决

触发条件：

```text
未沿已开辟通路进入雷场
```

对敌我双方均有效。

JSON：

```text
minefield.base_losses
minefield.vehicle_armor_corrections
```

基础结果以 2D6 查车辆/人员损失；车辆再按装甲修正。

横杠单元当前按 `0` 修正保存。装甲修正是否复用同一次 2D6，在完整原文中未完全明确，必须保留配置开关和审计记录。

---

## 10. 武器 ID 映射

| 武器 ID | 武器名称 | Canonical type |
|---:|---|---|
| 1 | 防空高炮 | `anti_air_cannon` |
| 2 | 便携防空导弹 | `portable_anti_air_missile` |
| 3 | 车载防空导弹 | `vehicle_anti_air_missile` |
| 4 | 速射炮（对地） | `rapid_fire_gun_ground` |
| 5 | 便携导弹（对地） | `portable_missile_ground` |
| 29 | 步兵轻武器 | `infantry_light_weapon` |
| 35 | 火箭筒 | `rocket_launcher` |
| 36 | 大号直瞄炮 | `large_direct_fire_gun` |
| 37 | 中号直瞄炮 | `medium_direct_fire_gun` |
| 43 | 车载轻武器 | `vehicle_light_weapon` |
| 54 | 小号直瞄炮 | `small_direct_fire_gun` |
| 56 | 速射炮 | `rapid_fire_gun` |
| 69 | 车载导弹 | `vehicle_missile` |
| 71 | 便携导弹 | `portable_missile` |
| 72 | 重型炮 | `heavy_artillery` |
| 73 | 重型导弹 | `heavy_missile` |
| 74 | 中型导弹（便携） | `medium_missile_portable` |
| 75 | 小型导弹 | `small_missile` |
| 76 | 巡飞导弹 | `loitering_munition` |
| 83 | 中型导弹（标准） | `medium_missile_standard` |
| 84 | 炮射导弹 | `gun_launched_missile` |
| 88 | 中型炮 | `medium_artillery` |
| 89 | 轻型炮 | `light_artillery` |

---

## 11. JSON 主要路径

```text
metadata
runtime_rules
implementation_scope
source_rule_summary
source_frame_alignment
source_mappings
known_ambiguities
design_notes

direct_fire.personnel_attack_levels
direct_fire.vehicle_attack_levels
direct_fire.infantry_vs_vehicle_attack_levels
direct_fire.elevation_attack_level_modifiers
direct_fire.vehicle_base_combat_result_chart
direct_fire.vehicle_result_modifiers
direct_fire.personnel_and_infantry_vs_vehicle_results
direct_fire.personnel_result_modifiers

indirect_fire.unobserved_dispersion
indirect_fire.grid_corrected_dispersion
indirect_fire.target_corrected_dispersion
indirect_fire.hit_target_result
indirect_fire.off_target_result
indirect_fire.result_modifiers

anti_air.weapon_attack_levels
anti_air.combat_results

minefield.base_losses
minefield.vehicle_armor_corrections
```

---

## 12. 每次裁决必须记录的证据

```text
adjudication_id
tick / event_time
attacker_id
target_id
weapon_id
weapon_type
target_domain
distance
attacker_blood
target_blood_before
target_blood_after
target_armor
base_attack_level
elevation_modifier
final_attack_level
dice
raw_roll
roll_modifiers
modified_roll
table_id
table_coordinates
base_outcome
final_outcome
ammunition_consumed
cooldown_started
source_rule_set
native_convention_flags
```

相同状态、动作和 seed 必须复现同一结果。

---

## 13. 尚未完全确定但已显式隔离的项目

- `U01` source_numeric_code_mapping：**PARTIAL**。armor numeric code -> armor class；complete type/sub_type enum；ammo slot 0/100/101/102 -> ammunition class；keep/keep_remain_time semantic mapping；observe_distance index -> target type
- `U02` indirect_scatter_direction：**UNRESOLVED**。table defines hit/scatter distance but not scatter direction
- `U03` indirect_result_correction_application：**UNRESOLVED**。numeric correction application to NO_EFFECT/SUPPRESS/numeric results must be explicit
- `U04` vehicle_anti_air_distance_20_overlap：**SOURCE_LAYOUT_AMBIGUITY**。retain distance 20 value and 20-50 band; both currently equal attack level 6
- `U05` minefield_dice_reuse：**UNRESOLVED**。confirm whether armor correction uses the same 2d6 or a second 2d6
- `I01` symbolic_personnel_result_order：**NATIVE_IMPLEMENTATION_CONVENTION**。NO_EFFECT < SUPPRESS < LOSS_1 < LOSS_2 < ...

这些项目不得静默猜测；不影响对应表格时可继续实现，影响裁决时必须拒绝或使用显式配置策略。

---

## 14. Codex 最小执行指令

```text
先读取 Native兵棋裁决知识库_Final.md。
加载并校验 native_adjudication_tables_final.json 的 schema、revision 和 content_sha256。
所有裁决只通过 JSON 查表，不读取 PDF，不使用连续伤害公式。
完整原文只在需要核对语义时读取。
为每张表建立边界测试和 FixedDiceRoller 测试。
对 Source 数字码保持 UNKNOWN，直到获得明确映射。
```

---

## 15. 文件校验

- JSON Schema：`native-adjudication-tables/1.2`
- JSON Revision：`1.2.1-final`
- JSON SHA-256：`2d0d03fa7ac5f1072b550378c20347b3e35e088a1348b459ae05967c0bab11d6`
- 完整原文 SHA-256：`96ab643eb7451d6507cbf48e6ed806ea447233eb91c8038526130b72feabc9af`
