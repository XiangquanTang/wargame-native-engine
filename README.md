# Wargame Native Engine · v22

Python 兵棋引擎，支持红蓝双方推演、智能体接入、训练与确定性回放。版本 **0.4.22**，采用 Native 简化规则。

[![Map123 双方行动](assets/map123-500-550-preview.gif)](assets/map123-500-550.mp4)

Map123 历史对局（2026-10-15），第 **500～550 帧**，红蓝同屏，2 倍速、25.5 秒。画面由真实回放数据渲染。

[观看视频](assets/map123-500-550.mp4) · [查看图片](assets/map123-500-550-poster.png)

## 快速启动

需要 Python 3.10+，在项目根目录执行：

```bash
python -m pip install -e .
python -m scripts.run_maps run river-valley --port 8003
```

打开终端打印的 RED / BLUE 地址。按 Ctrl+C 正常结束并保存回放，结果保存在 `local_data/`。

## 地图

包含 18 项公开地图资源、19 个场景变体，详见[地图清单](examples/maps/catalog.json)。

```bash
python -m scripts.run_maps list
python -m scripts.run_maps verify
python -m scripts.run_maps run map123 --variant original --port 8003
```

Map123 为 100×100 六角格、每格 200 米；首次展开视线缓存需约 1.1 GB 磁盘空间。
`--variant artillery-ammo2` 可切换至独立炮兵演示。上述启动命令分别运行，避免占用同一端口。

## 导入自己的地图与想定

将地图 ZIP 和想定 JSON 分别放入 `local_data/maps/inbox/`：

```bash
python -m scripts.run_maps start --input local_data/maps/inbox/map_221.zip --scenario-input local_data/maps/inbox/2201010101.json
```

自备地图不随仓库分发。追加 `--prepare-only` 仅转换检查；追加 `--port 8004` 可另开服务。

## 接口与使用说明

- [模型与训练接入](docs/engineering_guide.md)、[游戏工具调用](docs/game_tool_calling.md)
- [地图导入](docs/multi_map_workspace.md)、[想定转换](docs/source_game_start_v17.md)
- [架构与数据流](docs/architecture.md)、[云端运行](docs/notebook_map123.md)
- [回放视角](docs/replay_side_visibility.md)、[v22 迁移](docs/v22_migration.md)

基础运行无第三方 Python 依赖；PPO 学习器另需 `pip install -e '.[learner]'`。
请保留 `src/`、`scripts/`、`config/`、`examples/` 和 `docs/rules/` 的目录关系。
本地输入、缓存和运行结果不提交 Git。素材来源见 [provenance.json](assets/provenance.json)。

许可证：[MIT](LICENSE)。
