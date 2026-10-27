# 训练接口与公开场景验证

这些脚本仅依赖标准库和当前引擎，不运行学习器，不需要 PyTorch。
在完整源码根目录使用普通 Python（不要用 `python -O`，脚本包含断言），输出目录必须尚不存在。

## 接口 smoke

```bash
PYTHONPATH=src python checks/verify_training_contracts.py \
  "$PWD" /tmp/training-validation-current > /tmp/training-current.json
```

固定使用公开六格移动场景和种子 42，覆盖 Decision-v1/v2、红蓝两侧，共四组：

- reset 与三步绑定动作；观察和动作目录一致性。
- 拒绝上一窗口的旧绑定，不额外推进时间。
- 返回目录不能修改内部冻结目录。
- 运行截断与游戏终局区别。
- 发布回放并使用 `strict=True` 重执行。

固定选择器优先选 WAIT，否则使用目录索引零；这不是策略能力或完整训练质量测试。
恢复 checkpoint 和跨版本结果比较必须使用匹配规则、观察和动作协议。
v22 的裁决语义为 `native-joint-prestate/1.10`。

## 公开场景 reset / 空决策 / 严格回放

```bash
PYTHONPATH=src python checks/verify_public_scenarios.py \
  "$PWD" /tmp/public-validation-current > /tmp/public-current.json
```

脚本覆盖 17 个非 Map123 公开场景，每例重置后推进一个空决策 Tick，发布并严格回放。
这验证通用序列化及席位投影，不表示每种场景命名机制均被触发；Map123 和用户地图需单独验证。

## 与基线比较

基线保留在另一份完整目录，以相同脚本和匹配规则分别运行：

```bash
BASE=/实际的基线源码目录
PYTHONPATH="$BASE/src" python checks/verify_training_contracts.py \
  "$BASE" /tmp/training-validation-baseline > /tmp/training-baseline.json
cmp /tmp/training-baseline.json /tmp/training-current.json
```

公开场景可用同样方式比较。会话随机身份和路径按脚本定义排除，不得额外删除失败字段来制造一致。
生成报告和回放保存在指定本地目录，不随精简源码重复分发；本次通过与否以实际执行记录为准，
不沿用旧版本结论。完整回归入口为 `python devtools/run_checks.py`。

此验证不覆盖所有种子、长时训练、模型质量、网络提供方集成或浏览器实机行为；
席位权限和观察泄漏另由专门适配器测试覆盖，不能用一次 smoke 代替完整验收。
