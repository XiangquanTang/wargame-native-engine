# 上传包补充文件

两个文件需要从你自己的现有项目复制到本上传包，不能用旧 v2 checkpoint 替代。

|现有项目中的文件|本上传包中的目标位置|
|---|---|
|`local_data/maps/inbox/2201010101.json`|`release_assets/scenarios/221/2201010101.json`|
|`local_data/rl/capture-v3-221-20261101-152137-42733/final.pt`|`release_assets/models/ppo/capture-v3-final.pt`|

上述目标目录已创建。复制实际文件，不要改成路径字符串或 Git LFS 指针。

补齐后在本上传包根目录执行：

```bash
python release_assets/check_assets.py
```

期望所有行显示 `OK`。PPO 文件校验值来自已完成的真实对局记录，若校验失败，请确认是否复制了另一轮 checkpoint。

已包含：地图221原始ZIP、地图123原始ZIP及 `examples/map123` 原生想定、通视和移动代价数据。规则控制器为代码和规则配置，没有训练权重。

Qwen3-4B 与 NanoJev 权重不在本包。两者按服务安装说明另行下载；本包对接你已运行的本地端口8766和8765。

实际历史复盘及两模型训练数据未随源码发布。运行入口可重新生成对局和自包含决策记录。
