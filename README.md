# Native Wargame: RL / Rules / LLM + Jev

可审计的简化兵棋引擎与模型控制器。此源码快照冻结于 **2026-11-02**，引擎版本为 **0.4.24.1**，加入独立决策样本与片段导出。没有修改为0.4.25。

## 两条对战入口

1. **RL 对战规则**：红方冻结 PPO，蓝方规则控制器。
2. **LLM + Jev 对战 RL**：红方冻结 PPO，蓝方 Qwen 战略计划 + NanoJev 合法动作选择。

两个入口调用同一个 `scripts.run_match`，同一决策窗口每方最多提交一条命令，候选上限255，开发验证默认遇错停止。LLM计划作为Jev输入中的战略意图，Jev从引擎提供的合法候选中选择。没有用规则静默替代模型，也没有声称已完成兵棋数据微调。

## 文件与资产

- `src/`、`scripts/`、`checks/`：引擎、运行入口与检查。
- `config/`：原生规则、类型表和训练配置。规则控制器没有学习权重文件。
- `examples/map123/`：地图123原生想定、压缩通视表、移动代价与清单。
- `release_assets/maps/221/`、`release_assets/maps/123/`：获准附带的原始地图ZIP。
- `release_assets/scenarios/221/`、`release_assets/models/ppo/`：待用户补充的想定和本轮PPO权重。
- `tools/agent_services/`：已验证接口的本地Qwen服务和测试工具。
- `release_assets/MISSING_FILES.zh-CN.md`：缺失文件清单和准确放置位置。
- `SOURCE_FILES.sha256`：源码快照完整性清单，只用于文件校验，不是决策数据的业务索引。

**发布时尚缺2个文件**：地图221的`2201010101.json`想定，以及指定capture-v3 PPO权重。不能以旧v2权重替代。补齐后再执行两条真实模型对战。

## 环境

Python 3.10+。核心引擎使用标准库，PPO运行需要PyTorch。Ray只在并行训练时需要。

本次真实部署环境记录：Python3.10、Torch2.4.1+cu121、Transformers4.51.3、huggingface_hub0.30.2、safetensors0.5.3、accelerate1.6.0；TITAN RTX24GB；Qwen使用FP16，NanoJev使用FP32。这是已验证组合，不会由源码安装器自动替换你的Torch。

开发包安装（按自己的环境决定是否执行）：

```bash
python -m pip install -e '.[dev]'
```

PPO、Qwen与NanoJev权重是独立输入。Qwen/NanoJev权重未打入此源码包。NanoJev源码来自其[官方仓库](https://github.com/TianyuCodings/NanoJev)，具体下载和旧版Transformers兼容说明见`docs/local_model_services.md`。

## 先补齐资产

按照缺失清单复制文件，然后在仓库根目录执行：

```bash
python release_assets/check_assets.py
```

所有条目必须为`OK`。检查不会加载模型或反序列化地图中的pickle。

## RL 对战规则

```bash
python release_assets/run_workflow.py rl-vs-rules --max-steps 20
```

短局通过后：

```bash
python release_assets/run_workflow.py rl-vs-rules --max-steps 1500
```

## LLM + Jev 对战 RL

先保持两个本地服务运行：Qwen端口8766，NanoJev端口8765。

```bash
python release_assets/run_workflow.py llm-jev-vs-rl --max-steps 20
```

短局通过并核对决策记录后：

```bash
python release_assets/run_workflow.py llm-jev-vs-rl --max-steps 1500
```

输出写入自动创建的`local_data/matches/`子目录。不同运行不会覆盖。1500步结束可能是运行上限截断，`winner:null`不等于自动平局裁决。

需要使用其他地图/权重路径时，查看`python -m scripts.run_rl_rules_match --help`和`python -m scripts.run_llm_jev_match --help`（源代码方式运行时设置`PYTHONPATH=src:.`）。保留的旧默认路径只适配原部署目录；上面的发布入口使用本包的可移植资产路径。

## 地图123

原生数据已包含，不需要地图221的补充文件。首次准备会从压缩通视数据生成约1.1GB本地缓存；缓存位于`local_data/`，不进入Git。

```bash
PYTHONPATH=src:. python -m scripts.run_map123 --prepare-only
```

启动原生交互演示：

```bash
PYTHONPATH=src:. python -m scripts.run_map123 --port 8003
```

上述为地图123加载/演示入口，不将地图221训练的PPO效果自动视为地图123效果。

## 决策数据与复盘

新记录将当前可见态势、完整战略计划、局部编号和完整候选动作、选择及执行反馈放在同一条记录中。动作哈希保留在引擎技术层，不作为业务样本的必需外键。片段可以独立阅读和用于后续数据筛选。

自主模型输入只使用该方可见信息；执行结果与后续状态单列。使用训练样本时，应排除执行后信息和观察者数据，避免未来信息泄漏。片段可读不等于可以仅凭观察量完全恢复原始隐藏世界；确定性引擎重放仍使用原生复盘包。

具体记录文件、查看与导出参数见`docs/self_contained_decisions.md`。

## 验证范围

旧数据格式下，用户已在服务器完成RL对战规则和1500步LLM+Jev对战RL。新版数据格式的验证范围以`VALIDATION.json`为准。发布时没有在云端重新运行真实GPU、真实PPO权重或1500步全局，不能把模拟HTTP测试标成真实模型评测。

## 上传GitHub

解压本上传包，把**解压后的仓库文件**上传或提交到自己的仓库；不要把整个交付ZIP当成源码目录提交。没有在此流程中自动推送仓库。

资产中最大已包含文件是地图123压缩通视数据，约22.3MB；未包含1GB展开缓存。GitHub限制见[官方大文件说明](https://docs.github.com/en/repositories/working-with-files/managing-large-files/about-large-files-on-github)。补充自己的文件后先检查大小，不要上传模型缓存、代理配置、访问凭据或无关个人数据。

根目录MIT许可证沿用原引擎代码许可证。附带地图与用户补充模型属于单独的数据资产；发布者应确认其数据发布权，不将本文件视为替第三方授予许可。
