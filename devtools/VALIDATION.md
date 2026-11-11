# 开发检查与验收边界

从完整源码根目录执行：

```bash
python -m pip install -e '.[dev]'
python -m devtools.health release
python -m devtools.health schemas
python devtools/run_checks.py
```

`dev` extra 为 Python 3.10 提供 TOML 读取依赖；基础游戏运行不需要该开发依赖。
`run_checks.py` 执行 `devtools/checks` 与 `checks` 两套维护中的回归，不训练模型。
Node.js 用于前端逻辑检查；缺失工具引起的跳过必须如实记录，不能算成通过。

开发工具检查入口文件、导出、配置和路径边界；它本身不能证明游戏、训练和浏览器行为正确。
游戏与训练 smoke 见 [训练验证](../checks/TRAINING_VALIDATION.md)，
性能测量规范见 [基准说明](../benchmarks/README.md)。

历史发布包的测试数字和覆盖方式不代表 v22 结果。本轮验证报告应记录源版本、命令、退出码、
通过/跳过/失败数量和未验证范围。实机浏览器、不同 Python 版本、Windows、独立 wheel 和远程部署
只有实际执行后才能标记通过。源码检查通过不意味着只装一个 wheel 就具有全部外置规则和地图资源。
