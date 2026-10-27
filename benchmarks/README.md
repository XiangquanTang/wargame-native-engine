# 性能测量与结果等价检查

此目录保留可复现脚本，不把旧版本生成结果随 v22 源码重复分发，也不据旧测量声称 v22 提速。
所有基准从完整源码根目录运行，输出使用新的本地路径。

## 小场景测量

```bash
PYTHONPATH=src python benchmarks/benchmark_runtime.py \
  --ticks 40 --repeats 100 --output /tmp/runtime-current.json
PYTHONPATH=src python benchmarks/check_move_equivalence.py \
  --output /tmp/move-current.json
```

`benchmark_runtime.py` 测量真实 `RealtimeAgentSession.advance_tick()` 空决策路径；包含裁决、
投影与记录，但不是模型推理、训练、战斗策略或 HTTP 网络性能测试。
其中 status 查询为进程内调用，不得把结果当作网络延迟或整个引擎提速。
`check_move_equivalence.py` 是固定 MOVE 序列的结果对照，不是路径规划器。

Map123 先准备运行表：

```bash
PYTHONPATH=src:. python -m scripts.run_map123 --scenario original --prepare-only --json
```

将 JSON 输出中的 `cache_dir` 实际缓存目录传给测量程序，不复制旧版文档中的硬编码哈希路径：

```bash
PYTHONPATH=src python benchmarks/benchmark_runtime.py \
  --scenario examples/map123/scenario.json \
  --map-runtime-data /实际输出的运行缓存目录 \
  --warmup 3 --ticks 10 --repeats 30 --history-sizes 0 1000 \
  --output /tmp/map123-current.json
```

## 版本对照要求

- 基线与候选版使用独立目录、相同规则语义、场景、种子、动作序列和运行数据。
- 依次运行，不与其他重负载测试并发；保留每个样本及机器/解释器信息。
- 除耗时外比较每 Tick 权威事实、双方受限观察和严格回放结果。规则版本变化不能冒充等价优化。
- 说明预热、样本量、p50/p95、磁盘缓存和共享主机波动；无实测不得写提速百分比。
- 输出不提交到公开源码，展开视线缓存也不提交。

其他脚本包括 `benchmark_snapshot_copy.py`（复制微基准）、`benchmark_public_reads.py`
（公开查询）、`benchmark_replay_loading.py`（回放加载）。使用各自 `--help` 确认参数；
复制微基准收益不等于全局引擎收益。`compare_runtime_results.py` 和 `manifests/` 为维护中的
比较/评测接口，保留以支持测试和现有调用。

正确性回归见 `checks/test_runtime_status.py`、`checks/test_serialization_runtime.py` 及
[训练接口验证](../checks/TRAINING_VALIDATION.md)。本轮实际完成的验证以交付报告为准。
