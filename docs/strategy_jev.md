# 本地 Qwen 战略 + NanoJev 决策控制器

本增量包在现有 `run_match` 上增加一个统一插件，不替换推演循环、不训练模型。
适用已安装候选上限 255 补丁的 0.4.24.1、0.4.25。

## 调用链

1. 引擎创建该方可见的观测和合法候选快照。
2. 插件把紧凑态势发送至 Qwen，严格读取战略 JSON。
3. 插件缓存战略意图，把相同窗口的可见态势、战略意图和全部合法候选发送至 NanoJev。
4. NanoJev 返回 `c0`、`c1` 等本次请求内编号；插件检查概率、快照与编号映射，再返回引擎原始动作 ID。
5. 原生对局宿主完成动作合法性校验和执行，每方每窗口一条命令。

统一插件以现有 `--blue llm` 加 `--blue-plugin wargame_native_engine.matches.strategy_jev:create` 接入。
因此主对局元信息中的类别仍是 `llm`，插件模块和独立控制日志识别真实 Qwen+Jev 链路。
旧 `llm-jev` 模式是两个独立工厂的候选提议/评估协议，本包没有使用它隐式共享战略。

## 战略 JSON

Qwen 只输出 `schema`、`stance`、`objective_priorities`、`assignments` 四个字段。
`schema` 为 `native-strategy/1.0`；`stance` 为 `capture`、`hold`、`attack` 之一。
夺控点优先级引用实际夺控点 ID。每条分配含 `unit_ids`、`task`、`target_id`，最多 16 条；
单位必须为当前己方存活单位，单位不能重复分配。
夺控/守点目标必须是实际夺控点；攻击目标必须是当前可见敌人。
空分配合法，模型不生成引擎命令或坐标，不提供补给等未实现能力。

默认每 30,000 毫秒仿真时间更新一次；己方存活名单、敌人可见名单或夺控点归属改变后，
在距上次规划至少 5,000 毫秒时触发更新。旧计划引用失效时立即更新。
零候选返回空动作；只有一个候选直接执行，两者都不调用模型。

## 本地服务契约

- Qwen：`http://127.0.0.1:8766/api/plan`；请求只含 `messages` 和 `max_new_tokens`。
  插件默认输出预算 512 tokens。成功外壳中的 `text` 还须解析为完整、严格的战略 JSON，
  `finish_reason` 必须为 `stop`；不会修补代码围栏、半截 JSON 或非法目标。
- NanoJev：`http://127.0.0.1:8765/api/evaluate`；一个状态、一个 choice 问题，2～255 个候选。
  每个候选保留原生命令参数；返回完整概率表，编号必须精确匹配本次请求。
- 仅支持明确的 IPv4 回环 HTTP 地址，禁止重定向，不使用环境代理，不接收凭据。
- 默认每次 HTTP 调用超时 60 秒，启动脚本把外层控制器期限设为两次 HTTP 期限再加 30 秒。
  模型服务器在客户端超时后可能仍继续计算；超时会中止该局，不自动重试。

## 显存和上下文

255 是协议容量上限，不是 24GB 显卡满载实测结果。
原 NanoJev 服务会同时计算同一问题的候选路径，没有前缀共享；候选数和态势长度都影响显存。
插件采用可见态势字段白名单，不传完整观察者复盘；默认限制态势序列化长度为 12,000 字符，
超限报错，不静默裁剪。字符限制不等于 token 限制；两个服务保留各自的上下文检查。
不要把候选分组后直接比较各组 softmax 概率，也不要为了跑通偷偷删掉合法候选。

## 启动与检查

保留已验证的 Qwen 8766 与 NanoJev 8765 服务，在引擎项目目录、新终端的 jzj 环境执行：

```bash
PYTHONPATH=src:. python -m unittest checks.test_strategy_jev -v
```

先运行 20 窗口联调：

```bash
PYTHONPATH=src:. python -m scripts.run_llm_jev_match --max-steps 20
```

短局确认无 failures，查看控制日志中的计划、Jev 选中动作及执行复盘，再运行完整局：

```bash
PYTHONPATH=src:. python -m scripts.run_llm_jev_match --max-steps 1500
```

默认红方 PPO 使用 `local_data/rl/capture-v3-221-20261101-152137-42733/final.pt`，CPU 推理。
蓝方为统一插件，地图 `local_data/maps/inbox/map_221.zip`，想定 `local_data/maps/inbox/2201010101.json`，
seed 42。每次自动生成新的时间戳目录，不覆盖旧结果。
启动脚本打印 `MATCH OUTPUT` 和 `CONTROLLER LOG`。控制日志记录战略版本、计划、候选 ID 映射、
动作和耗时；对局输出下的 replay 记录实际执行结果。

默认异常策略固定为 `abort`；超时、模型错误、非法编号、错误概率、过长状态均不切换规则策略。
本包验证的是本地假 HTTP 服务、插件进程和原生短局接口；真实双模型同卡联调及 1500 步对局须由服务器实测。
