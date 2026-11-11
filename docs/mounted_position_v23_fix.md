# 乘员跟车位置修正（0.4.23.2）

## 根因与最小修改

旧默认引擎没有开启已有的乘员坐标同步；旧同步选项也仅更新格中心 x/y，不携带子格进度。Source 导出和录像显示随后各自读取乘员旧位置，造成载具开走而乘员图标留在上车格。

新对局默认 `mounted_position_policy = native-mounted-position-sync/1.1`：

- 引擎提交每步时同步存活乘员 x/y 和 `cur_pos_millionths`；初始已载员场景也同步。
- `mounted_on` 是权威承载关系；乘员仍 `deployed=False`、`current_hex=None`，表示没有独立地图占位。不能把载具路线或执行中的路段复制给乘员，避免二次移动或独立观测/被攻击资格。
- Source 新帧导出同帧载具的精确 `cur_hex`、`cur_pos`；乘员 `move_path=[]`，`extra.native.mounted_position` 标明承载位置及目标路段。下车完成仍由已有规则落在载具实际所在格。未改变上下车时长、移动、射击、视野或毁伤规则。
- 共享显示适配只从已包含在当前视角中的同阵营、双向明确承载关系派生位置。乘员不另画一个地图/点击图标；载具保留搭载人员信息。实时界面原有的上车过渡显示不变。

## 历史数据与身份

位置修正改变了新对局的权威状态/哈希，由独立策略版本记录在快照、对局元数据和录像头中；不是声称所有状态与旧版等价。裁决语义继续 `native-joint-prestate/1.11`。历史缺失策略（`None`）和旧 `native-mounted-position-sync/1.0` 显式保留原引擎/导出行为，严格重演不会自动迁移。

旧录像无需重录即可修正地图显示：仅 `viewer_units` 使用当前帧明确载具关系，记录 `position_source=RECORDED_CARRIER_RELATION`。原始 JSON、compact 解码、文件与哈希完全不改；此适配不修写旧历史权威状态，也不反推出历史动作或不可见敌方关系。缺少、矛盾、跨阵营或未包含的关系不猜测。

## 回归范围

`checks/test_mounted_position_consistency.py` 验证公开 BOARD→MOVE（多帧子格）→STOP→UNLOAD 全链、实时 render 数据、权威快照、原始 Source、compact 树等价、新档严格重演、旧显示只读/视角边界及载具毁伤级联。旧版 0.4.23.1 真实生成的 None/1.0 档案另行严格重演核对，不作为新录像重写。
