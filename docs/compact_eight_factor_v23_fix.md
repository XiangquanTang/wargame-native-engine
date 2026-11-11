# v23 八因子复盘修复（0.4.23.1）

修复 compact 录像查看器没有接入既有分析器的问题。裁决语义仍为
`native-joint-prestate/1.11`，八因子公式、训练奖励与 Source/Native 输入合同均未改变。

- 打开 compact 录像即可计算当前帧八因子，完整曲线在独立 HTTP 请求中流式计算。
  曲线计算时显示加载提示，地图、单位与逐帧播放无需等待整局分析。
- 八因子继续采用 `V/H/W/A/R/M/I/S` 对应基础价值、健康、武器、弹药、战备、
  机动、信息、状态；复用既有分析器，不由页面猜值。
- header 显式 `analysis_model=source|native` 时优先使用该口径。新录制把已知的
  分析选择写 header 一次，不写进每帧。自定义分析器不冒充这两种模型。
- 对未声明分析模型的旧 compact 包，只有匹配的 Native 投影协议、完整版本化
  `analysis_facts` 且初帧 Native 八因子完整时，默认展示 **Native 记录事实派生**。
  页面明确说明原录制选择未知；这不是声称恢复原在线模型。其余默认 Source。
- 页面可在 Source 与 Native 间切换（Native 需要匹配的记录协议）。模型固定后
  全曲线使用同一口径；缺字段的后续帧保持 UNKNOWN，不再次换模型或填默认值。
  Source 缺少旧字段时仍为 UNKNOWN，不用 Native 值混填成“Source 实测”。
- Native 机动采用记录中的 `movement_locked`，不是完整 MOVE 合法性，也不是
  `command_lost`；状态按现有合同使用当前压制且只惩罚一次，不叠加旧 `keep`。
- RED/BLUE bundle 只带符合可见性投影的己方因子，不带敌方八因子或全局分析。
  既有明确标注的全局曲线/全局汇总仍是裁判复盘功能，不成为 Agent 观测。
- 阵亡单位维持当前贡献 0 的显示和可展开原始因子；历史缺失数据保持 UNKNOWN。

## 存储、缓存与验证

原包只读，不生成分析 sidecar、不写全帧分析历史、不增加逐帧 hash 链。
按需单帧缓存同时限制 **16 项 / 8 MiB**，键包含模型、版本、模型身份与真实帧号。
完整曲线每种模型只流式解码一遍，仅保留数值数组和当前相邻两帧的分析，
不反复从检查点 seek 导致二次复杂度；后续曲线请求复用内存结果。
已有 HTTP 响应缓存继续限制 **128 项 / 32 MiB**。

回归入口：

```sh
PYTHONPATH=src python -m unittest checks.test_compact_analysis checks.test_compact_replay_viewer -v
python devtools/run_checks.py
```

修复只改变复盘分析接线、模型选择显示和一次性 header 身份；不改原始输入包，
不承诺旧 compact 录像具有严格重执行证据。未提供包外 SHA256 时仍如实标记未验证。
