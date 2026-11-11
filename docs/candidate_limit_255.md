# 候选预算 255 与控制器接口核对

## 本次修改

- `scripts.run_match`、`run_match()`、`MatchRuntime` 默认 255，并在载入地图/创建输出/运行控制器之前拒绝大于 255 的预算。
- 新训练、warm-start 默认 255，拒绝 >255。`MinimalTrainingEnv` 默认 255。
- 已有 checkpoint 的权重、元数据、编码器 schema 和参数维度不改。
- strict resume 未显式传 `--candidate-limit` 时，用 `torch.load(..., map_location='cpu', weights_only=True)` 读取旧 checkpoint 的真实候选容量。显式参数仍走原来的严格匹配检查。
- 单独 `run_rl evaluate` 继续继承 checkpoint 原始配置。旧 256 的 low-level encoder/env 显式参数保留以保证历史实验重现。这是兼容入口，不是新对局的默认配置。
- 不把全引擎所有 256 替换成 255。库存条目容量等无关 256、旧编码器契约 hash、原始执行层容量不动。
- PPO standalone match 可从旧训练 256 迁移到新对局 255，报告中 `match_transfer.candidate_limit` 如实记录 training=256、match=255、changed=true。

## 无静默截断

候选空间由引擎枚举合法动作和 bounded spatial provider 共同形成；provider 计算剩余规划预算。若已有原生动作或最终 catalog 仍超过容量，编码器抛出显式容量异常，不切掉末尾候选，不把两个候选合并，不隐藏失败。0 个合法动作允许编码为空；1 个候选可由未来模型适配器直接选；255 个完整映射；256 个对 255 编码器显式失败。

## 现有控制器协议

`ControllerContext` 提供 side、seed、decision_index、detached observation、binding、public_map、weapon_catalog。没有 world/runtime/gateway 句柄。binding 的 action_space.actions/action_ids/mask 是本窗口权威候选，编号随窗口绑定。

`PluginController` 的 local factory 为 `module:factory(config)`，对象实现 reset(context)、decide(context)、close()。decide 返回权威 action ID 或 None。主机重新校验、映射并执行。CLI 已有 --red/--blue llm 和 --*-plugin/--*-config。

`HybridController` 现有协议是 planner.propose(context) -> 非空合法 ID 序列，再 evaluator.evaluate(context, proposals) -> proposals 中的一个 ID 或 None。它没有战略意图传递字段；两个工厂不能自动共享 Qwen 计划。

建议后续最小协同插件在同一个对象内持有 Qwen 计划缓存和 Jev HTTP 客户端，使用已有 PluginController 协议。插件一次 decide 完成战略刷新判断、候选选择、返回 ID。不要为了传战略而重写原生对局循环，也不要暗中依赖跨进程全局变量。需要新增 llm-jev 标识时另行显式扩展，不把未实现的适配器称作已接通。

Qwen 实际服务 POST /api/plan 返回 text/input_tokens/output_tokens/finish_reason/generation_seconds，text 还需解析并校验策略 JSON。NanoJev POST /api/evaluate 用 states[].questions 的 choice/criteria，支持 2–255 候选；只有 0/1 候选由适配器明确处理。

首版模型错误/超时/策略 JSON 不合法/未知候选 ID 一律 abort，不用隐式规则兜底。Plugin RPC 杀死 worker 不等于取消 Qwen 服务里已经开始的 GPU generate；超时后不能盲目重试。同步仿真墙钟等待不推进模拟时间。

## 验证边界

本包包含 stdlib 候选边界、原生短对局和安装回滚测试；真实 Qwen/Jev 联调、旧 PPO checkpoint GPU 推理、Ray 训练和完整 1500 步对战不属于本次已执行验证。
