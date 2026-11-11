# 本地模型服务

## Qwen

运行入口随本包附带于`tools/agent_services/`，权重另行准备。本次使用模型`Qwen/Qwen3-4B-Instruct-2507`，本地模型目录自行传入：

```bash
python tools/agent_services/serve_qwen_strategy.py --model-dir /path/to/Qwen3-4B-Instruct-2507
```

另一个终端：

```bash
python tools/agent_services/test_qwen_strategy.py
```

默认绑定`127.0.0.1:8766`，FP16、SDPA、总上下文8192。服务只读本地权重，不进行网络模型调用。生成返回文本，统一控制器再严格校验战略JSON。

## NanoJev

使用[NanoJev官方源码](https://github.com/TianyuCodings/NanoJev)及`C-Tianyu/NanoJev`的`unified-games-v1`权重。源码与权重未随本包复制。请记录自己实际下载的源码提交和模型修订，便于复现；本发布没有虚构一个未核实的源码提交。

从NanoJev仓库目录启动：

```bash
python -u scripts/serve_decisions.py --checkpoint-dir /path/to/NanoJev-unified --web-root web --host 127.0.0.1 --port 8765 --precision fp32
```

TITAN RTX已验证FP32路径。不要在Torch2.4.1添加`--disable-native-triton`，其新Torch内部接口不可用。旧Transformers4.51.3读取该模型时需正确处理tokenizer的特殊token配置，以及backbone的RoPE参数。根目录下`tools/agent_services/fix_nanojev_compat.py`提供保守、备份式兼容修复；仅对NanoJev目录使用，不对Qwen4B应用。

先停NanoJev服务，再使用修复工具的`--help`检查参数。工具只修复配置，不改权重，不安装依赖。

## 对局约束

- 每方每个仿真决策窗口一条命令。
- Qwen默认每30个仿真秒或重要事件重规划，缓存战略。
- Jev输入包含战略和全部合法候选。候选255是协议上限，不保证所有255长文本组合都能放入24GB显存。
- 模型HTTP错误、超时、非法JSON、无效选择都会中止测试，不自动切换规则。
- HTTP客户端超时不终止模型服务正在计算的请求；不要立即自动重试。
- 两个服务常驻显存，对局结束不自动卸载。结束全部工作后在服务终端按Ctrl+C退出。

服务在当前示例中仅绑定回环地址。外部访问控制、TLS和多用户鉴权不属于此本地研究部署。
