# 熊猫智脑 Cola v3 转接说明

## 当前工作区

- 路径：`D:/熊猫开发项目/熊猫智脑cola v3`
- Windows PowerShell
- 本目录不是 Git 仓库，无 Git 历史可依赖
- 唯一开发口径：`docs/页面设计定版/00~05`，上游契约锚定《增强版PRD》§13.3 与《对外数据接口规范》
- 项目硬纪律：权力链 `AdvisorProposal → GeneralDecision → ExecutionOrder → Policy/Approval → Executor`；`append_event` 唯一写入口；fail-closed；演示数据/待接入接口显式标注；不伪造地理配准；原图结构不重绘

## 已完成基线

- P16-T1~T11 已完成：事件域、首页路由、人员物资、沙盘、真实底图/无人机影像、复盘、报告版本、复盘问询、地理坐标标注叠加
- 最近一次完整回归：`python -m unittest discover -s tests` → **448 tests OK**
- P16-T11 测试：[test_p16_sand_page.py](<D:/熊猫开发项目/熊猫智脑cola v3/impl/tests/test_p16_sand_page.py>) 已扩至 19 用例
- P16-T10 测试：[test_p16_review_page.py](<D:/熊猫开发项目/熊猫智脑cola v3/impl/tests/test_p16_review_page.py>) 已扩至 22 用例
- 工作日志：[WORKLOG.md](<D:/熊猫开发项目/熊猫智脑cola v3/docs/WORKLOG.md>) 已记录 T1~T11，但文件顶部「待处理事项」仍有旧的 P16-T2 文案，后续可顺手修正

## 当前任务

### P16-T12：组织架构原图像素锚点标定与首页框外动态叠加

用户已明确批准引入图像库。当前选择：

- 开发期标定工具使用 Pillow + numpy；当前环境已安装 `PIL 12.1.1`、`numpy 2.4.2`
- 运行时不导入 Pillow/numpy，只读取静态 `org_anchors.json` 并用 HTML/CSS/原生 JS 叠加
- 原图完整显示、不裁切、不用 HTML 重绘组织树
- 锚点哈希锁定原图；图像变更、锚点文件缺失、结构不完整时全部降级当前合规侧栏，禁止猜测定位
- 动态层只放在原图外部或确认的空白区，不遮挡原图文字

02 号定版验收口径：

- 框外角标：到场人数/编制人数、到场/未到场状态、通信状态（通信源未接入则明确「待接入」）
- 点击组织框后右侧显示该组人员列表：姓名、单位、固定岗位、工作组、中队、到场状态、到场时间、通信状态、联系方式脱敏、当前任务、数据更新时间
- 当前登录人员需要额外高亮和本人详情
- 岗位结构永远由原图呈现，叠加不重排、不改写岗位

## 本轮已做但未完成

已创建开发期工具：[calibrate_org_anchors.py](<D:/熊猫开发项目/熊猫智脑cola v3/tools/calibrate_org_anchors.py>)。

已执行：

```powershell
cd "D:\熊猫开发项目\熊猫智脑cola v3"
python tools/calibrate_org_anchors.py detect
```

结果：

- 原图：[org_chart.png](<D:/熊猫开发项目/熊猫智脑cola v3/impl/static/org_chart.png>)，尺寸 1266×640
- 确定性矩形检测得到 **40 个框**，与原图人工计数一致
- 调试图：[ _anchors_debug.png](<D:/熊猫开发项目/熊猫智脑cola v3/impl/static/_anchors_debug.png>)（文件名实际为 `_anchors_debug.png`，链接中的前导空格仅为排版说明）
- 原始矩形：[ _detected_rects.json](<D:/熊猫开发项目/熊猫智脑cola v3/tools/_detected_rects.json>)
- 尚未创建 `tools/_anchor_names.json`
- 尚未创建 `impl/static/org_anchors.json`
- 尚未修改 homepage.py，首页目前仍是原图 + 现有合规右侧栏

### 必须先修的工具 bug

`calibrate_org_anchors.py` 的 `cmd_build()` 当前归一化表达式：

```python
[round(v / w, 5) for v in rect[::2]] + [round(v / h, 5) for v in rect[1::2]]
```

这会生成 `[x1, x2, y1, y2]`，而运行时应统一使用 `[x1, y1, x2, y2]`。应改为：

```python
[
    round(rect[0] / w, 5),
    round(rect[1] / h, 5),
    round(rect[2] / w, 5),
    round(rect[3] / h, 5),
]
```

另外，工具的 `build` 目前只生成 `rect`，尚未计算确定性安全的 badge 空白位置。建议为每个框生成 `badge_rect` 或 `badge_position`：优先检查框上方、下方、右侧的原图墨迹密度，只有空白区域才允许角标；三处都被占用就不输出角标（fail-closed）。

## 40 个检测框编号映射

编号来自 [_anchors_debug.png](<D:/熊猫开发项目/熊猫智脑cola v3/impl/static/_anchors_debug.png>)，坐标按图中从上到下、同一行从左到右的检测排序。不要重新调用视觉模型取坐标；直接使用 `_detected_rects.json` 的编号与矩形。

| index | 类型 | code / label |
|---:|---|---|
| 0 | node | 前线总指挥（原图文本：前线总指挥（属地行政首长、总负责）） |
| 1 | node | 联合指挥部/工作组 |
| 2 | slot | `news-publish` / 宣发材料组 |
| 3 | slot | `news-media` / 媒体服务组 |
| 4 | node | 新闻发言人 |
| 5 | node | 调度长（原图文本：调度长） |
| 6 | slot | `xz-coord` / 综合协调组 |
| 7 | slot | `news-sentiment` / 舆情研判组 |
| 8 | node | 副总指挥·专业技术支持 |
| 9 | node | 副总指挥·灭火指挥调度 |
| 10 | node | 副总指挥·后勤综合保障 |
| 11 | node | 副总指挥·火场维稳管控 |
| 12 | slot | `log-water` / 供水组 |
| 13 | slot | `log-medical` / 医疗组 |
| 14 | slot | `pjjz-1` / 扑救一组（一分区） |
| 15 | slot | `tech-expert` / 专家支持组 |
| 16 | slot | `tech-plan` / 方案编制组 |
| 17 | slot | `air-op` / 空中作业组 |
| 18 | slot | `sec-case` / 火案侦查组 |
| 19 | slot | `tech-assess` / 灾情评估组 |
| 20 | slot | `log-power` / 供电组 |
| 21 | slot | `log-stay` / 食宿组 |
| 22 | slot | `tech-resource` / 资源调配组 |
| 23 | slot | `pjjz-2` / 扑救二组（二分区） |
| 24 | slot | `mobile-support` / 机动支援组 |
| 25 | slot | `sec-order` / 秩序维护组 |
| 26 | slot | `tech-info` / 信息收集组 |
| 27 | slot | `tech-monitor` / 火情监测组 |
| 28 | slot | `log-comm` / 通讯保障组 |
| 29 | slot | `guide` / 向导组 |
| 30 | slot | `log-public` / 群众生活组 |
| 31 | slot | `pjjz-3` / 扑救三组（三分区） |
| 32 | slot | `sec-guard` / 现场警戒组 |
| 33 | slot | `tech-weather` / 气象支持组 |
| 34 | slot | `tech-record` / 行动记录组 |
| 35 | slot | `log-material` / 物资供应组 |
| 36 | slot | `log-site` / 场地组 |
| 37 | slot | `pjjz-4` / 扑救四组（四分区） |
| 38 | slot | `supervisor` / 扑救督导组 |
| 39 | slot | `sec-traffic` / 交通保障组 |

原图共 40 框：32 个 `POSITION_SLOTS` 槽位 + 8 个上级/副总指挥节点。`org_model.py` 中的 `POSITION_SLOTS` 为 32 项；`COMMAND_NODES` 为上级节点文本来源。

## 建议的新对话执行顺序

1. 读取本文件和 `tools/calibrate_org_anchors.py`，先修 rect 归一化 bug。
2. 创建 `tools/_anchor_names.json`，严格使用上面的 0~39 映射；检查 code 与 `org_model.POSITION_SLOTS` 一致。
3. 让 `build` 生成 `impl/static/org_anchors.json`，内容至少包括：版本、原图文件名、原图 SHA-256、尺寸、40 个 box、归一化 `rect`、经过原图墨迹检查的 `badge_rect`。
4. 新增纯标准库运行时加载/校验函数，推荐放在 `homepage.py`：
   - 读取 `/static/org_chart.png` 对应本地文件并计算 SHA-256；
   - 校验版本、尺寸、哈希、rect 0..1、code 唯一、数量/字段完整；
   - 任一失败返回 `None`，首页继续现有合规侧栏；不要猜测或部分静默叠加。
5. homepage.py 接入叠加层：
   - 原图外层使用 `position: relative`，图片保持完整、可缩放拖动；
   - 每个有效 slot 渲染框外角标和透明点击区域；不要在图框内覆盖文字；
   - 服务端将每个 slot 的脱敏 roster 数据预渲染到右侧隐藏面板，原生 JS 点击切换，不增加写接口；
   - node 框没有长期人员槽位时只保留锚点语义，不伪造人数；
   - 通信字段缺失/待接入时明确显示「通信待接入」，不得显示正常。
6. 新增专门测试（建议 `test_p16_org_anchors.py`）：加载器哈希/结构/越界/重复 code fail-closed、badge 位置不遮挡检测、首页 fallback、首页 overlay、点击 panel 字段脱敏。
7. 运行：

```powershell
cd "D:\熊猫开发项目\熊猫智脑cola v3\impl"
python -m unittest discover -s tests
```

预期基线至少保持 448 全绿，并在此基础上增长。

## 注意事项

- `_anchors_debug.png` 和 `_detected_rects.json` 是当前标定中间产物，可以保留供新对话使用；不要把调试图作为用户界面资源。
- `_scratch_probe.py` 是本轮临时探测脚本，若新对话清理，删前确认只包含本轮临时内容。
- 当前没有执行 homepage 接线，因此不要误报 P16-T12 已完成。
- 不要把 Pillow/numpy 导入 `impl/panda_brain` 运行时模块；依赖仅限 `tools/` 标定脚本，避免改变线上运行依赖。
- WORKLOG 顶部旧的「P16-T2 进行中」事项与实际状态不符，完成 T12 时一起更新为当前待处理项。
