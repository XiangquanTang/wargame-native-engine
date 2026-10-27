# 八因子优势曲线：数据边界与展示规则

## 1. 本文范围

本文审计现有复盘分析模型及其输入证据，不修改引擎裁决、训练奖励、八因子公式或历史 sidecar。以下结论来自代码与随 v6 验证产生的 Map123 样本，不等于已经读取用户实例中的 2547 帧复盘。

现有分析身份：

- model_id：`native-eight-factor`
- model_version：`0.1.0-contract`
- model_hash：`77e0113c896c813033528d43bc6c132e296cb91eacf9a153d436cb9fe20bc577`
- perspective：`GREEN`，即完整事实复盘分析，不是红蓝任一方可获得的决策观测。
- 定义文件：`src/wargame_native_engine/analysis/semantic_identity.py`。

这是历史 demo 兼容的 Native 分析候选模型，不是官方规则、胜率模型或对真实装备效能的校准。不得把曲线直接解释为获胜概率，也不应称为已经实现经典兰彻斯特平方律。

## 2. 当前计算口径

单算子有效值：

`E_i = V_i × H_i × W_i × A_i × R_i × M_i × I_i × S_i`

固定按 V、H、W、A、R、M、I、S 顺序相乘。任一因子不可用时，单算子有效值为 `None`，不是零。明确的零值（如已毁单位的健康因子为零）与未知不同。

八个因子的含义与当前输入：

- V 基础价值：`value`，下限为 1。
- H 健康：`blood`、`max_blood`；摧毁为零，存活时按既定健康比例映射。
- W 武器：`carry_weapon_ids`，取现有配置权重中的最大值，不把多武器权重相加。
- A 弹药：`remain_bullet_nums`，按总量进行既定对数映射。
- R 战备：`weapon_cool_time`、`weapon_unfold_state`。
- M 机动：`move_state`、`stop`、`can_to_move`、`on_board`。
- I 信息：`see_enemy_bop_ids`，按可见敌方数量映射并封顶。
- S 状态：`keep`、`tire`、`lose_control`、`close_combat`。

完整公式常量和来源分别位于 `analysis/demo_compat_factors.py` 与 `analysis/semantic_identity.py`；本次不重新设定权重。

阵营有效值为该阵营所有有效算子的 E_i 之和。未知算子被跳过，并记录覆盖率。该阵营存在算子但没有一个有效算子时，阵营值为未知；空阵营为零。

- 原始优势：`red_power - blue_power`。
- 归一化优势：`(red_power - blue_power) / (red_power + blue_power)`。
- 正值表示红方相对优势，负值表示蓝方，范围为 [-1, 1]。
- 双方有效值均为零时，现有模型返回零；任一阵营有效值未知时，优势也为未知。
- `delta_advantages` 是相邻记录的归一化优势差，不是原始值差、时间变化率或按秒计算的导数。
- `confidence` 尚无定义，保持未知，不能填成 100% 或用覆盖率冒充置信度。

代码：`analysis/effective_power.py`、`side_aggregation.py`、`advantage.py`、`timeline.py`。

## 3. 已复现的 Map123 数据缺口

v6 的公开接口验证样本含 49 个算子、11 个事实帧，对应 100 个引擎 tick。检查该样本的 `frame_analysis.native.jsonl`：

- 全部 539 个“算子×帧”记录均只有 5/8 个因子可用。
- A 未知：缺少 `remain_bullet_nums`。
- M 未知：缺少 `can_to_move`。
- S 未知：缺少 `tire` 与 `lose_control`。
- 两方完整八因子有效单位覆盖率均为零，11 个事实帧的优势均为未知。

因此，这个样本没有可诚实绘制的完整八因子优势曲线。前端不能把未知替换为零，也不能用默认 1 补齐后仍标注为原模型实测结果。

复现方式：运行 `checks/verify_map123_moving_fire.py CACHE_DIR OUTPUT_DIR`，读取其发布目录中的分析 sidecar，统计 `unit_analyses[].observed_factor_count`、缺失字段与 `advantage.normalized_advantage`。这项审计没有据此推断用户未上传复盘的全部内容。

## 4. 为什么不能直接补旧字段

### 弹药

当前 Source 投影合同为 `source-frame-compat/1.1`。`compat/source_frame/exporter.py` 仅在旧弹药槽与已绑定弹药池完整对应时输出 `remain_bullet_nums`。

Map123 样本保留了旧输入槽 0、100、101、102，而部分算子的武器仅绑定池 0。即使未绑定旧槽为零，投影仍将旧字段标为未知，避免把无法映射的 Source 槽当成已验证当前库存。这是显式兼容策略，不是允许前端猜测的缺省值。

当前 Native 库存确实存在于：

- `extra.native.weapon_inventory.weapons` 的武器、资源政策与池绑定；
- `extra.native.weapon_inventory.ammunition_pools` 的当前池存量。

若另建 Native 估算口径，可以按绑定的 `AMMUNITION_KEY` 唯一键读取库存，共享池只计一次；但这与旧字段求和不是完全相同的输入语义，必须具有不同的模型身份和来源说明。

### 疲劳与机动许可

`extra.native.fatigue_level` 是当前运行时疲劳事实，可供新的 Native 输入适配使用，但不能将旧 sidecar 中缺失的 `tire` 偷改成已观测。

当前发布帧没有与 `can_to_move` 完全等价的权威字段。停止、存在路径、没有阻塞原因都不等于某个动作当前一定可执行；不能用这些条件悄悄推断 `can_to_move=True`。

### 压制与失控

`keep` 在当前 Source 导出器中由输入模板保留，没有随 `OperatorState.suppression_remaining_ms` 同步更新。因此，仅字段存在并不证明它是当前压制事实。后续 Native 模型若需要当前压制，应有明确的实时投影字段及测试；不能将模板值当作动态真值。

旧版导出没有稳定记录可供该分析使用的实时压制状态。v9 的独立 Native 模型明确将当前火力压制解释为 `lose_control`，只应用一次对应惩罚，并排除含义未确认的旧 `keep` 项；这不改变 Source 模型或声称两套数据完全等价。缺失不等于 False，历史复盘也不能由未来帧补推为已观测。具体入口、证据和版本边界见 [双生成流程](analysis_generation.md)。

### 信息

`see_enemy_bop_ids` 由引擎当前可见性刷新，并由导出器投影。代码位置为 `core/engine.py` 的可见性更新与 `compat/source_frame/exporter.py`。此项与上述模板保留的 `keep` 不同，不应误报为静态输入。

## 5. 可以直接修正的展示问题

这些修正不改变分析数学口径：

1. 红蓝阵营有效值必须共享同一纵轴。旧页面分别对两条曲线做自身 min/max 缩放，可能使实际不等的力量曲线完全重合；常数曲线也会被画在相同底部。
2. 归一化优势使用独立的 [-1, 1] 纵轴或独立图，不与任意有效值量纲混用。
3. 未知值保留空缺，不插零、不跨缺口连线。
4. 显示阵营覆盖率、可用单位数和缺失因子原因。只显示 frame 级 warnings 不够，还要展示 side 与 advantage 的告警。
5. 部分覆盖率下的阵营和优势应显式标注“仅已知单位”，不能默认为全军完整指标。
6. 帧索引与时间分开标注；事实帧数不等于引擎 tick 数，也不自动等于秒数。
7. 复盘曲线仅用于 GREEN 全知分析，不能加入红蓝 Agent 的隐藏信息观测。

回归建议：两方数值成比例但不相等、常数序列、全部未知、中间缺失、单方部分覆盖率、双方已知零、极值、单帧、快速拖动和慢网络下连续播放。

## 6. 可选 Native 估算政策提案（尚未实施）

如需在当前缺失字段条件下额外显示一条可用估算曲线，应作为独立、明确标注的 Native 游戏分析模型，而不是修补原始 sidecar：

- 保留原模型、原始 UNKNOWN 和训练奖励路径不变。
- 为新政策定义独立 model_id/version/hash、输入来源、缺失处理与已声明假设。
- 事实来源区分“当前观测”“Native 派生”“明确假设”“仍未知”；不得把假设统计为观测覆盖率。
- A 可来自唯一绑定的当前库存池；疲劳可来自 Native fatigue_level。
- 机动许可、失控以及旧复盘缺失的当前压制只能保持未知，或在用户明确接受后采用公开、版本化的假设。
- 展示八因子力量、相对优势及数据覆盖率。
- 不将估算结果写回引擎事实、胜负判定、命中伤害或既有训练奖励。
- 既有 `training/ppo_benchmark_config.py` 中的显式补全政策只适用于其冻结研究协议，不构成给一般复盘自动补值的授权。

该提案需要单独确认具体假设。本文及本次数据审计没有实现或启用新数学模型。
