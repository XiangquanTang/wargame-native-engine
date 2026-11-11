# 统一对战入口：规则 / 冻结 PPO / 未来 LLM / LLM+JEV

新入口 `python -m scripts.run_match`，不改变训练入口，不重训、不改 checkpoint。
红蓝独立选择 `rules`、`ppo`、`random-legal`、`no-op`、`llm`、`llm-jev`。
这里是**同步决策窗口**对战，不是原 realtime 多算子并行下令模式；双方每窗口最多一个指令，使用相同引擎时间步，随后一起推进正常游戏时钟。

## 服务器直接开局

在项目根目录、已安装 PyTorch 的 `jzj` 环境：

```bash
conda activate jzj
PYTHONPATH=src:. python -m scripts.run_match \
  --input local_data/maps/inbox/map_221.zip \
  --scenario-input local_data/maps/inbox/2201010101.json \
  --red ppo \
  --red-checkpoint local_data/rl/ray-u30-bc10-e1500-221-resumed-jzj/final.pt \
  --blue rules \
  --seed 42 --decision-ms 1000 --candidate-limit 256 --max-steps 1500 \
  --output local_data/matches/ppo-red-vs-rules-221-001
```

输出目录必须不存在。先冒烟可用 `--max-steps 8`，这不代表完整对局或策略强度。
交换双方：改为 `--red rules --blue ppo --blue-checkpoint .../final.pt`，并换新输出目录。
默认 PPO 用 CPU 确定性 argmax；可显式 `--red-device cuda`，不同设备/库版本不保证逐比特一致。
原生想定输入也可用 `--scenario-bundle path.json [--map-runtime-data runtime.json]`。
导入源地图时创建 `<output>-inputs` 专属目录，不覆盖现有地图、想定、模型或结果。

复盘：

```bash
PYTHONPATH=src:. python -m scripts.run_replay_viewer \
  --replay-dir local_data/matches/ppo-red-vs-rules-221-001/replay --port 8000
```

浏览本机 `http://127.0.0.1:8000`；远端服务器使用 SSH 端口转发。compact ZIP 内含地图/想定资产，另有 Native 八因子分析 JSONL。

## 结果与公平性边界

- `match.json`：真实原生胜负/终局、是否仅到 runner 上限、参数、模型 SHA256、训练与对战设置差异、错误/清理状态。
- `decisions.jsonl`：每方每窗口动作 ID、候选目录哈希、状态和墙钟耗时；不保存插件 prompt、配置、原始错误响应。
- `replay/replay.zip`：原生 compact 复盘；`replay/frame_analysis.native.jsonl`：观察员 Native 分析。**这些结果文件是双方全局观察员资料，不能回送 actor。**
- 达到 `--max-steps` 只是截断，不把八因子领先者宣布为赢家。引擎终局规则未修改。
- PPO 严格验证既有 minimal PPO schema 和权重形状，只载入冻结模型；既有训练恢复/评估的 side/opponent 检查不被修改。这里是明确独立的跨对手/跨座位对战，差异在 `match_transfer` 留痕，不等于证明跨地图泛化。
- `rules` 复用真实 `SimpleCaptureTeacher`，轮转算子、每窗口一个指令；不是原实时 `simple-capture` 的整席批量指令。它可由引擎规划规则目的地；PPO 从有界空间候选子集选择，两者规划策略本身不同。
- 每窗口重新获取座位投影、己方库存、当前己方武器资料与掩码（包括延迟增援到达后新增的武器），容量随当前可见实体变化；不会把隐藏敌军数量作为固定模型输入。候选容量不足会明确失败，不悄悄截断。目前复用既有 provider 的硬界限：候选最多 256、每方每窗口规划请求最多 128；大编制场景可能显式超限，需将来扩展 provider 而非盲目增大 CLI 参数。
- 固定种子与相同 CPU/版本/控制器可复现引擎动作与复盘内容；耗时、输出路径及 ZIP 容器字节不是复现指标。第三方插件是否确定性由其实现负责。

## 控制器契约（未来接口可实际测试，但没有假装接入模型）

`matches.controllers.ControllerContext` 只含：本方 `side`、种子、决策序号、受限 `observation`、当前 Decision-v2 `binding`（合法候选及掩码）、公开静态地图格 ID 和仅限当前己方库存的武器资料。没有运行时、完整世界、对方观察或 Native 八因子分析句柄。每次传入独立副本；控制器不能修改权威状态。

本地插件以 `module:factory` 指定，工厂接收 JSON config 并返回对象：

```python
class MyController:
    def reset(self, context): ...
    def decide(self, context):
        # 只能返回当前 binding.action_space 中 mask=True 的 action_id，或 None 空过。
        return action_id
    def close(self): ...
```

LLM+JEV 使用两个明确插件：planner 的 `propose(context)` 返回非空合法 action ID 列表；evaluator 的 `evaluate(context, proposals)` 只能返回其中一个 ID 或 None。JEV 的真实语义、provider、模型与业务评估逻辑由未来插件实现，本包不宣称提供真实 LLM/JEV。

可离线验证接口：

```bash
PYTHONPATH=src:. python -m scripts.run_match --scenario-bundle YOUR_NATIVE_SCENARIO.json \
  --red llm --red-plugin examples.match_plugins:first_legal --blue rules \
  --max-steps 8 --output local_data/matches/offline-plugin-001

PYTHONPATH=src:. python -m scripts.run_match --scenario-bundle YOUR_NATIVE_SCENARIO.json \
  --red llm-jev --red-plugin examples.match_plugins:planner \
  --red-evaluator examples.match_plugins:evaluator --blue rules \
  --max-steps 8 --output local_data/matches/offline-hybrid-001
```

这两个示例只是“首个合法动作”离线 stub，**不调用任何模型或网络**。不配置插件时 `llm` / `llm-jev` 明确报错，不自动降级伪造回答。`--red-config config.json` 可提供用户主动配置的参数；不把配置复制到结果。只加载自己信任的代码：进程隔离是超时/状态隔离，**不是恶意 Python 的安全沙箱**，插件仍具有执行者账户权限。无需也不默认添加真实外部调用。

## 错误、时限及生命周期

默认 `--failure-policy noop`：非法动作本窗口空过；异常/超时控制器停用，之后空过；reset 失败亦停用。均在报告中记录。严格验证用 `--failure-policy abort`：立即停止并尝试发布截断诊断复盘，命令失败退出。引擎/目录构造错误不伪装成控制器空过。

`--decision-timeout-seconds 30` 约束每方；插件运行在持久 spawn 子进程，RPC 超时后终止，不让挂死插件卡住比赛。内置规则/PPO 是可信同步代码，仅在返回后测超时、丢弃超期指令，不承诺抢占式硬取消。墙钟速度不影响引擎时间；这个入口不测实时响应优势。reset 在首个决策前调用，close 在收尾调用；失效插件不反复重启。

## 验证

```bash
PYTHONPATH=src:. python -m unittest checks.test_match_controllers -v
PYTHONPATH=src:. python devtools/run_checks.py
```

测试使用合成小场景和小型实际 checkpoint，无用户地图长训练；包含规则/PPO 换边、冻结权重、复盘读回、受限可见性、种子复现、插件/混合 stub、非法动作、异常、超时、原生结束及 runner 截断。真实服务器的现有 final.pt 和真实地图仍需用户执行上述命令确认；本地通过不等同于在用户 GPU 上运行过。
