# v23：原始 Source JSON 无损精简复盘

软件版本 **0.4.23**；裁决语义仍为 `native-joint-prestate/1.11`。
新的容器版本为 `native-source-compact-replay/1.0`，不是旧 Native 严格回放格式。
最新生成帧的 Source 投影协议为 `source-frame-compat/1.3`，动作兼容见
[source_action_export_v23.md](source_action_export_v23.md)。旧协议按原版本读取。

## 正常运行默认行为

`ScenarioMatchFactory`、`WargameFacade` 与 `scripts/run_realtime_dual_control.py`
默认使用 `compact`。停止一局后输出 `replay.zip` 与**包外** `replay.zip.sha256`；
正常只附简短运行摘要，不落逐命令、逐 tick 审计侧文件。未进入引擎的拒绝/取消/替代请求
以简洁 `command_outcomes` 保存在包内；已进入引擎的动作与实际结果由帧记录。
不额外输出逐帧完整 JSON、逐帧 hash 清单、分析 JSONL、严格重执行报告或控制审计副本。
实时数据记录采用压缩增量 spool，成功启动器退出后只清理本次新建的 scratch；
失败时保留本次诊断目录。不会删除旧局、用户缓存、原始地图或想定。

compact 会话只保留最新的在线分析对象，**不在临时文件中暗存完整分析历史**。
如研究接口请求历史分析，可从已提交 compact 帧按需重新计算；计算结果不写回原始帧。
引擎内部状态/授权身份不改动。仅**新生成 Native 普通录制**在存储边界采用明确的
`native-source-playback-projection/1.0`：不保存逐帧 `state_hash`，不保存
`expected_state_hash`、ordered_actions/action_sequences 逐步审计副本。
静态规则/policy/版本/seed/session 身份只放 header 一次；header 中政策/资产身份 hash
不是多份包完整性校验，完整性校验仅为包外一个 SHA256。
合法候选和部分实际动作里的 `authority_hash`、`ammo_mapping_hash`、`damage_policy_hash`、
`execution_policy_hash`、`indirect_fire_authority_hash`、`observation_policy_hash` 是真正用于
提交授权/策略绑定的业务参数，不是逐步状态审计校验；原键和值保留，仍由增量压缩去重。
不能为了“所有 hash 字样为零”而破坏 `valid_actions` 的可复用格式。
既有原始 Source JSON 导入没有这项投影，原字段（包括原有 hash 命名字段）无损保留。

## 一个包里有什么

- `compact_manifest.json`：真实容器版本、帧名顺序、检查点索引、来源与结果。
- `assets.json`：运行时地图与想定各一份。`native-scenario-split/1.0` 下，
  `map` 是**完整 NativeMap 数据**，`scenario` 是完整 NativeScenario 去掉 map；
  `{**assets['scenario'], 'map': assets['map']}` 可还原原始规范想定。
  显示地图由这份原始数据派生，不额外保存另一份网格。它不冒充外部 LOS/机动查询表。
  通用原始 JSON 导入未提供地图时，地图保持未知，不补配其他场景。
- `chunk-00000000.jsonl.gz` 等：每段首帧为完整原始 JSON 检查点，其后是无损树增量。
  默认每 128 个实际存在的帧设检查点；seek 最多重建这一段，不加载整局帧树。
  每条记录单独 gzip 压缩并组成连续成员，写入失败可回滚到已提交前缀。
- 普通 Native 运行不写逐步审计 `evidence`。**状态变化、命中裁决、位置、上下车、
  占领结果仍由实际帧与 judge_info 保存**，不是仅保存命令再推测结果。
  已核实 Source action 的完整重复 Native command 被去重；原 wrapper、admission、业务 ID
  以及 wrapper 未涵盖的 Native 参数（例如锁定 weapon_id、特殊移动模式）仍保存。
  未知/未映射动作保留完整 Native 扩展，不能猜数字码。
  裁决事件的静态 `source_rule_set` 去重为 header 中 `adjudication_sources`，事件保留
  `source_ref`，完整来源/版本不同即使用不同引用；实际 effect 参数不变。

容器不调用 `SourceFrame.to_dict()`，不会借兼容模型归一化原始 JSON。
须区分：历史 raw 导入保证原树；新 Native normal 先进行上述明确去审计投影，再保证
投影后的业务树无损。不能宣称 normal 与完整 debug GREEN 的 canonical hash 一致。
原始未知字段、null、空列表/字典、字符串字典键、数组顺序、int/float 区别、浮点值
及负零均保留。保证的是**解析后的 JSON 树**，不是原始空白、缩进或浮点文字写法。
`valid_actions` 完整保留，通过增量与压缩去重，绝不因为“体积大”就删除。
不接收非 JSON 值、NaN/Infinity、重复键、越界路径或模糊类型转换。

帧文件名、`time.cur_step`、动作的 `cur_step` 是独立记录：文件序号不被用来重写游戏时间。
原文件号有缺口时仅保留实际给出的帧；播放滑块使用 ordinal，显示/请求使用真实帧号。
只有两帧的输入是稀疏快照，不会伪造为一整局过程。

## 使用

```bash
# 正常 Web 对局：默认 compact，启动与保存信息都会显示模式
PYTHONPATH=src python scripts/run_realtime_dual_control.py \
  --scenario-bundle examples/source_movement_scenario.json \
  --output-dir /tmp/my-new-run --run-namespace my-new-run

# 明确需要研究/排查用的旧严格回放与完整分析时
# 追加：--replay-mode legacy-debug

# 原始编号 JSON 文件打包；只读输入，不覆盖已有输出
PYTHONPATH=src python -m wargame_native_engine.cli.compact_replay pack \
  --output /tmp/source-snapshots.zip /path/to/100.json /path/to/145.json

# 完整重建检查；包外 hash 存在时验证，缺失时明确 hash_verified=false
PYTHONPATH=src python -m wargame_native_engine.cli.compact_replay check /tmp/source-snapshots.zip

# 恢复原始 JSON 树和原始文件名；输出目录必须不存在
PYTHONPATH=src python -m wargame_native_engine.cli.compact_replay unpack \
  /tmp/source-snapshots.zip --output /tmp/restored-source-snapshots
```

Python 入口为 `replay.compact.CompactReplayWriter` 与 `CompactReplayReader`。
`raw_frame(ordinal)`、`iter_raw_frames()` 返回可独立修改的原始树；`evidence(ordinal)`
返回可选独立证据。`append(raw_frame, name=..., evidence=...)` 不推断字段意义。
只有调用者确实提供时才保存 LLM 的 phase、stance、brief_summary、tool_calls 等记录；
缺失表示未知。没有根据 MOVE/SHOOT 反推出“正在攻坚”之类模型意图。
命令 `PENDING`/`APPLIED` 不是行动已完成；实际完成须查看对应状态和结算证据。

## 播放与旧格式

`load_replay_view` 自动选择真实的 compact/旧 Native reader。compact 无需分析侧文件、
逐帧校验链或重新跑规则即可播放；外部单包 hash 也不伪装成严格重执行证据。
请求 RED/BLUE 时必须有当时真实记录的可见性与匹配协议；缺少证据则 unavailable，
不会回退到 GREEN。敌方私有属性、全局动作与裁判信息不泄漏到阵营视角。

原旧 reader 的严格协议不放宽。`legacy-debug` 必须在录制开始时明确选择，才记录完整 debug GREEN 与 Native evidence，
恢复旧编号帧/steps，生成原严格 manifest 与分析并验证。普通已经推进的录制不能
事后伪称有完整 strict 证据；没有暗存另一份审计历史来支持这种转换。
`NativeTrainingEnv`、训练完整包、multi-seat 研究发布继续显式选择旧严格路径；
本次不是训练奖励协议或裁决语义升级。

## 校验与测量边界

正常发布在 staging 内流式重建所有帧并验证包 hash 后才暴露整个输出目录。
中途失败不发布半套结果，可从仍在的 compact spool 重试。读者不解压到文件系统，
拒绝重复/未知 ZIP 成员、异常检查点、增量路径错误、超限记录与异常 JSON 数值。
内存随单帧/检查点段解码工作量增长，不随所有帧树增长；索引仅随帧数线性增长。
单帧最大 64 MiB、资产 256 MiB、嵌套 128 层；超限明确报错，不静默截断。

压缩率应分别报告：原始全帧 ZIP 对 compact ZIP（同数据、同口径）、包外文件、
在线工作目录与显式 debug 产物。不把 JSON 对 ZIP 的倍数冒充 ZIP 对 ZIP 收益。
少量稀疏输入可能只省一点，甚至变大；长局静态重复、候选动作重用程度决定实际收益。
用户自己的稀疏案例与真实运行案例单独交付，不嵌入通用源码/升级包。

## 八因子查看器修复

软件补丁 **0.4.23.1** 已恢复按需单帧八因子与流式完整曲线，支持明确标注的
Source/Native 口径切换；仍不保存分析 sidecar 或逐帧 hash。
详见 [v23 八因子修复](compact_eight_factor_v23_fix.md)。
