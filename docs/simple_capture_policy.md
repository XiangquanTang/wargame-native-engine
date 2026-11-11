# 最简红蓝规则策略（0.4.23.3）

这是用于观察现有 Native 游戏规则效果的确定性基线，不是训练模型或战术优化器。
不安装新依赖，不修改裁决语义 `native-joint-prestate/1.11`。

## 直接在上传的地图/想定上运行

与人工启动相同，地图 ZIP 和想定 JSON 分开放在 `local_data/maps/inbox/`。
在完整源码根目录运行（文件由使用者提供）：

```bash
PYTHONPATH=src:. python -m scripts.run_maps start --input local_data/maps/inbox/map_221.zip --scenario-input local_data/maps/inbox/2201010101.json --red-policy simple-capture --blue-policy simple-capture --max-ticks 600
```

9601：

```bash
PYTHONPATH=src:. python -m scripts.run_maps start --input local_data/maps/inbox/map_9601.zip --scenario-input local_data/maps/inbox/201033029601.json --red-policy simple-capture --blue-policy simple-capture --max-ticks 600
```

终端打印红蓝网页地址；每秒推进一帧。自动席位网页是只读观察，服务端也拒绝该席位的
preview/execute/submit 写入，避免人工和规则互相覆盖。只设置一方策略即可人机对战；
未设置的席位仍由人操作。省略 `--max-ticks` 可运行到场景终止/截断或 Ctrl+C。

上述命令追加 `--fast` 则不启动 HTTP、不等待墙钟，使用同样的每帧模拟时间快速运行。
`--fast` 必须同时指定两个自动席位和正数 `--max-ticks`。加速不缩短武器锁定、冷却、
移动或停止转换的模拟时长。`--tick-interval-ms` 是模拟帧间隔，不是独立播放倍速。

每次 `start` 都新建想定和对局目录，不覆盖旧记录。到达帧数上限、场景时间上限或 Ctrl+C
均按实际状态保存 unfinished/truncated；绝不把上限当作获胜。运行结束打印 `REPLAY` 和
`POLICY SUMMARY` 路径；compact 回放仍按现有工具打开。

## 决策规则

每帧分别给红蓝独立实例提供其自己的受限观察，先完成双方决策，再共同推进同一帧：

1. 只处理存活、已部署且未搭载的地面人员/车辆。无人机、空中单位、无可用普通地面直射
   武器的单位保持不动，并记录原因；不自动下车、引导、间瞄、切换武器或补弹。
2. 只从本方真实携带的、公开配置支持普通地面直射的武器中选择。已有合适锁定优先；
   否则按最大射程降序、武器 ID 升序选一件。先提交显式武器 ID 的锁定，观察锁定完成才机动。
3. 看到敌人不等于马上有合法射击。仅对当前己方可见、存活地面目标，采用当前精确的
   actor/weapon/target 射击能力，且自己的弹药、冷却、公开射程均满足时发 `SHOOT`。
   目标按距离、ID 排序，明确传 `target_id` 和 `weapon_choices`，不对目标格盲射。
   已锁定的机动中射击沿用引擎规则；没有合法目标就继续靠近夺控点，不陷入超距空射循环。
4. 非己方夺控点按六角格距离、点 ID 排序，由现有服务端路径预览跳过不可达的点。
   不根据隐藏敌人绕路。正在走的路线保持原任务，避免每帧重发 MOVE 重置进度。
5. 到点或相邻格请求一次停止，等实际停稳和转换完成；只有当前合法 CAPTURE 能力存在才夺控。
   敌军争夺导致 CAPTURE 不可用时留在附近等候/射击；已属己方的点不重复夺控。

每个单位每帧最多一项意图，全部组成一份 `SeatDecision`；不追加 WAIT 覆盖其他单位。
策略不读取 GREEN、原始敌方世界或敌方场景模板。本方武器资源和可见目标当前格均来自
已有席位绑定的只读投影；武器目录是公开定义，移动预览也是席位绑定的现有接口。

## 能力不等于已结算成功

提交/预览通过不是命中、毁伤或最终执行成功。`policy-summary.json` 区分提交的操作与命令级
执行结果。若一个自动命令在实际 apply 时被拒绝，宿主停止继续自动提交，保存部分回放并
返回非零退出码，同时保留原始错误，避免对同一无效组合无限重试。一个组合命令被拒绝时，
其中个别单位可能已经合法执行；不要把命令级计数当作逐单位毁伤统计，实际效果以回放为准。

公开的 `public_close_combat_scenario.json` 旧示例含与既有直射表不一致的配置（例如人员
strength=5，以及轻武器对车辆距离4而对应表只到3）。这不是本策略修改了规则；该示例可能
触发明确拒绝。不要通过补造敌方护甲/血量或扩表来绕过。本策略的基础生命周期测试使用
有效的 `source_movement_scenario.json` 派生夹具；真实地图效果需以各自实际回放验证。

## Python 接入与验证

`agents.simple_capture.SimpleCaptureSeatAgent` 实现现有 `LocalSeatAgent`；宿主示例在
`scripts/rule_policy_host.py`，不需要大型 Agent 框架或模型服务。

```bash
PYTHONPATH=src:. python -m unittest discover -s checks -p 'test_simple_capture*.py' -v
PYTHONPATH=src:. python -m unittest discover -s checks -p 'test_rule_policy_host.py' -v
python -m devtools.health release
python devtools/run_checks.py
```
