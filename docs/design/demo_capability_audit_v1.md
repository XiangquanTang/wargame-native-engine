# Demo Library / Capability / Benchmark 组合能力审计 v1

审计日期：2026-08-25

审计范围：

- `examples/demos/`
- `config/capabilities/native_capability_registry_v1.json`
- `config/benchmarks/native_capability_benchmark_v1.json`
- `scripts/run_demo.py`
- `scripts/run_benchmark.py`
- `src/wargame_native_engine/demo_registry.py`
- `src/wargame_native_engine/capability_registry.py`
- `src/wargame_native_engine/capability_benchmark.py`
- 对应 Demo、Capability、Benchmark 测试

本审计没有修改 Engine、WorldState、Action、ActionRouter、火力裁决、
Boarding 规则或 Replay schema。本文所称规则均为 Native simplified rules。

## 1. 审计结论

Phase G 已在不修改 Engine 或 Replay 语义的前提下补齐组合编排层。当前状态是：

```text
Engine / action stream / strict replay foundation    READY
Single-capability Demo and Benchmark                 READY
Composite Demo orchestration contract               READY
Composite Benchmark aggregation                     READY
```

## 2. 当前 Demo、Capability、Benchmark 对照

| Demo ID | Demo features | Capability ID | Benchmark ID | 分类 |
| --- | --- | --- | --- | --- |
| `boarding_basic` | `BOARDING` | `boarding` | `boarding_basic` | 单能力验证 |
| `direct_fire_basic` | `DIRECT_FIRE` | `direct_fire` | `direct_fire_basic` | 单能力验证 |
| `guided_fire_basic` | `GUIDED_FIRE`, `MULTI_OPERATOR_COORDINATION` | `guided_fire` | `guided_fire_basic` | 单能力裁决 + 协同前置条件 |
| `indirect_fire_basic` | `INDIRECT_FIRE` | `indirect_fire` | `indirect_fire_full_cycle` | 单能力完整生命周期 |
| `mechanized_assault_basic` | `BOARDING`, `DIRECT_FIRE`, `OBJECTIVE_CONTROL`, `VISIBILITY` | `boarding`, `direct_fire`, `objective_control`, `visibility` | `mechanized_assault_basic` | 多能力组合验证 |
| `objective_control_basic` | `OBJECTIVE_CONTROL` | `objective_control` | `objective_control` | 单能力裁决；使用 MOVE 驱动 |
| `visibility_blocking` | `INCOMPLETE_INFORMATION`, `VISIBILITY` | `visibility` | `visibility_blocking` | 单能力观察验证 + 语义标签 |

注册项按 ID 确定性排序，所有路径受 `DemoRegistry._required_path()` 约束，不能逃逸
`examples/demos/`。

`MULTI_OPERATOR_COORDINATION` 和 `INCOMPLETE_INFORMATION` 是 Demo supporting feature，
不是 Capability Registry 中的独立 capability。`mechanized_assault_basic` 是当前唯一同时映射
多个已注册 Capability ID 的 Demo。

## 3. Capability Coverage Matrix

| Capability | Engine | Human | Agent | Replay | Demo | Benchmark |
| --- | --- | --- | --- | --- | --- | --- |
| `boarding` | IMPLEMENTED | PASS | PASS | PASS | `boarding_basic`: PASS | `boarding_basic`: PASS |
| `direct_fire` | IMPLEMENTED | PASS | PASS | PASS | `direct_fire_basic`: PASS | `direct_fire_basic`: PASS |
| `guided_fire` | IMPLEMENTED | PASS | PASS | PASS | `guided_fire_basic`: PASS | `guided_fire_basic`: PASS |
| `indirect_fire` | IMPLEMENTED | PASS | PASS | PASS | `indirect_fire_basic`: PASS | `indirect_fire_full_cycle`: PASS |
| `objective_control` | SIMPLIFIED_IMPLEMENTATION | PASS | PASS | PASS | `objective_control_basic`: PASS | `objective_control`: PASS |
| `visibility` | IMPLEMENTED | PASS | PASS | PASS | `visibility_blocking`: PASS | `visibility_blocking`: PASS |

这张表只代表 `native-capability-registry/v1` 的六个正式注册能力，不代表全部 ActionType。
当前 Human capability projection 还暴露 `MOVE`、`STOP_MOVE`、`WEAPON_LOCK`、
`WEAPON_UNFOLD` 和 `WAIT`，但它们没有独立 Capability Registry 条目、Demo 和 Benchmark，
因此不能在上述矩阵中宣称完整的七列覆盖。

## 4. 单能力与组合能力分类

### A. 单能力验证 Demo

六个基础 Demo 都有且只有一个可执行主 validator：

- `BOARDING`
- `DIRECT_FIRE`
- `GUIDED_FIRE`
- `INDIRECT_FIRE`
- `OBJECTIVE_CONTROL`
- `VISIBILITY`

它们验证的不是孤立函数，而是各自能力所需的完整权威链路，包括 Human/Agent 投影、
Realtime session、Engine、事件和 strict Replay。

### B. 已有组合证据

现有 Demo 已证明某些能力可以作为另一能力的必要支撑：

- `guided_fire_basic` 同时使用可见性、多个己方算子和 GUIDED_FIRE，但只对
  `guided_fire` 建立正式 capability/benchmark 结论。
- `objective_control_basic` 使用 MOVE 驱动占领、转移和争夺，但只对
  `objective_control` 建立正式结论。
- `boarding_basic` 贯穿 capability、BOARD/UNLOAD、75 秒 transition、观察刷新和
  valid-actions 刷新，但不是 movement + boarding 的战术任务场景。
- `indirect_fire_basic` 包含 capability、mission、scatter、ACTIVE area、damage、event
  和 Replay 生命周期，但仍属于一个 `indirect_fire` 主能力。

这些是“组合链路证据”。正式的多 Capability ID 组合验收由
`mechanized_assault_basic` 提供，其 phase 顺序是：

```text
boarding
→ movement
→ unloading
→ objective control
→ visibility/contact
→ direct fire
```

## 5. Demo Runner 能力审计

| 能力 | 状态 | 证据与边界 |
| --- | --- | --- |
| 单 feature validator | PASS | `_validator_for()` 按主 feature 路由到六个 validator 之一。 |
| 多 feature validator | PASS | v2 descriptor 的有序 phases 组成 validator pipeline。 |
| Scenario dependency | PASS | phase 以 `required_capability` 声明依赖，且不进入 Engine。 |
| Success condition | PASS | 每个 phase 返回 frozen `ValidationResult`，统一汇总为 checks。 |
| Event trace validation | PARTIAL | 各 Demo 检查所需事件；Boarding 检查完整顺序。统一有序事件子序列校验位于 Benchmark 层。 |
| Replay validation | PASS | `_publish_and_verify()` 对每个已实现 Demo 执行 `strict=True`，不匹配即拒绝 PASS。 |
| 输出契约 | PASS | 统一生成 `replay.zip`、`evidence.json`、`summary.json`、`validation_report.json`。 |

Runner 按 feature/phase validator 而不是 Demo ID 选择执行器；源码搜索未发现 Demo ID 进入
`src/wargame_native_engine/`。Demo 专用预期和 orchestration 留在 `scripts/run_demo.py`
及数据文件中，没有注入 Engine 规则。

## 6. Benchmark Layer 能力审计

| 能力 | 状态 | 说明 |
| --- | --- | --- |
| 单能力 benchmark | PASS | 六个 capability 均保留一个 benchmark。 |
| Engine/Human/Agent/Replay 分栏 | PASS | 每栏由命名 checks 计算，失败会生成显式失败报告。 |
| Scenario-level evaluation | PASS | 可运行组合 Demo，并验证 world hash、RNG hash、event trace。 |
| 组合任务 benchmark | PASS | v2 descriptor 支持排序的 `capability_ids` 与按能力命名的 checks。 |
| Event trace | PASS | `expected_events` 使用确定性有序子序列匹配。 |
| 确定性重执行 | PASS | 从 scenario + seed 新建 Engine，逐 step 执行 recorded `ordered_actions`。 |
| Traceability | PASS | Capability、Demo、Benchmark 路径和支持状态均交叉校验。 |

旧的单 capability benchmark 仍向后兼容；组合 benchmark 同时输出 scenario result 和逐能力结果。

## 7. Replay 一致性审计

结论：**PASS**。

1. 所有七个 Demo 的 `expected_behavior.json` 都声明 `strict_replay_matches=true`。
2. `run_demo.py::_publish_and_verify()` 发布 Replay 后调用正式 strict replay。
3. `run_benchmark.py` 再次运行 strict replay，并通过 `_reexecute_action_stream()`：
   - 从 manifest 读取 seed；
   - 从 scenario 创建新 `NativeEngine`；
   - 按记录顺序反序列化 `EngineAction`；
   - 使用每步 `delta_time_ms` 调用正式 `engine.step()`；
   - 比较重执行 world hash、RNG hash 和 event trace。
4. `io/replay.py` 的 strict 路径使用 `ordered_actions` 重走 Engine，并将生成的 step、snapshot、
   action trajectory、event trajectory 与记录证据比较。
5. 未发现 Demo runner 或 Benchmark 将记录的最终 WorldState restore 为裁决结果。
6. Runner 中的 `snapshot()` 只用于只读断言、hash 或前后状态比较；不替代 action execution。
7. Visibility Demo 是 observation-only 场景，其 Replay 输入是一个合法的空 action tick，仍通过
   正式 Engine 重执行验证。

## 8. 缺失能力与下一阶段建议

后续增加组合 Demo 时应保持 Engine 和 Replay 不变，只扩展编排数据与可复用 validator：

1. 继续使用排序后的 `capability_ids` 和有序 `phases`；不要把组合逻辑写入 Engine。
2. 新 validator 必须是可复用的确定性 check provider，只消费公开 session、observation、event
   和 Replay 证据。
3. 新 phase 应描述预期和依赖，不得授权或覆盖规则。
4. Benchmark 应继续使用 namespaced capability checks，并保留单 capability 字段的兼容读取。
5. 后续组合场景可在当前最小链路基础上扩展：

   ```text
   BOARD
   → 75s complete
   → MOVE
   → UNLOAD
   → visibility contact
   → objective occupancy
   → one explicit fire action
   → strict replay
   ```

6. 组合 Benchmark 必须继续比较完整 action stream、world hash、RNG hash 和 event order；不得通过
   恢复最终 snapshot 或 Demo ID 特判获得 PASS。

`mechanized_assault_basic` 可以作为正式 Composite Scenario；其他 supporting-feature 场景仍不应
自动视为多 Capability ID 组合验收。
