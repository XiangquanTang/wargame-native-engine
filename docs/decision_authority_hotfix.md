# 决策记录授权字段兼容修复

适用：已安装 self-contained-decisions-addon 的 0.4.24.1 和 0.4.25。

## 原因

初版记录器直接复制引擎候选动作，但读取校验器拒绝动作参数中的技术哈希。
真实 BOARD / UNLOAD 含 `authority_hash`，间接 FIRE 含五个规则校验哈希，
因此即使这一回合没有选中这些动作，只要候选列表中出现也会触发错误。
旧的小场景测试没有覆盖这些候选，未发现记录器与校验器不一致。

## 修复范围

- 业务动作保留动作类型、单位、目标、位置、武器、路径、profile_id、规则版本等决策参数。
- 只从业务副本中去掉已知授权指纹：BOARD / UNLOAD 的 authority_hash；
  FIRE 的 indirect_fire_authority_hash、execution_policy_hash、ammo_mapping_hash、
  observation_policy_hash、damage_policy_hash。
- 引擎绑定中的原动作、动作 ID、授权校验和技术复盘不变。模型仍选本窗口 c0…，
  控制器使用映射提交原始引擎动作。业务动作不是可直接提交给引擎的授权命令。
- Jev 收到的候选与新业务记录一致，不再携带上述授权指纹。此变化可能影响真实模型
  的概率输出；没有声称真实模型逐步选择完全不变，也没有重新训练。
- 读取器兼容初版已生成的完整 native-decision-sample/1.0 记录，只在已知动作位置
  做上述投影，然后执行严格校验。原 JSONL 文件不改写，提取的新片段采用修复后的格式。
- 缺少状态/策略的老式 hash-only 日志仍不接受；不补造历史、观测或策略。
  未知技术字段、错误位置、选择不在候选列表等仍报错。报错列出字段路径，不打印字段值。

## 使用

安装后重启对局 Python 进程。已有 decisions.jsonl 不需要重跑才能查看或截取：

```
PYTHONPATH=src:. python -m scripts.inspect_decisions --input 你的对局目录/decisions.jsonl --side blue --limit 20
PYTHONPATH=src:. python -m scripts.inspect_decisions --input 你的对局目录/decisions.jsonl --side blue --start-window 0 --end-window 19 --output 新片段.jsonl
PYTHONPATH=src:. python -m unittest checks.test_decision_record_authority checks.test_decision_records checks.test_strategy_jev -v
```

不会覆盖已有输出片段。原文件、地图、权重和复盘均保留。
