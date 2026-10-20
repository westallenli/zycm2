# 交接文档：P16 系列 → T23 起（2026-08-24 深夜定版；2026-08-25 五页口径重对齐后更新）

> 交接口径与 `v3/docs/TRANSFER_P16_T12.md` 同构：新对话按本文档开工，**每轮一个有界任务**（跨项目规则见 `D:/熊猫开发项目/熊猫智脑cola v4/../AGENTS.md`，即 `D:/熊猫开发项目/AGENTS.md`：开工先读 WORKLOG，收工写 WORKLOG）。

## 0. 一句话现状

**产品口径已重对齐为固定五页**（火情登记｜人员架构｜沙盘指挥｜人员与物资｜复盘；参谋/LLM/Multi-Agent 全部后台运行，前台不出现 AI/参谋自述），规划见 `docs/DEVELOPMENT_PLAN_PAGES_V1.md`。**P0–P6 六个阶段已全部执行完毕**：前台口径纠偏与五页导航冻结；外部报警收件箱确认闭环（同构建档/拒绝/重复关联，哈希入链）；首页数据驱动自绘（零图片依赖）；沙盘四类信息分区+后台任务语义；人员物资页长期库入口+八态卡+观看者身份权限；复盘 A-2 严格分离（六类结论级动作仅负责人本人会话）；八步验收主线 E2E 一条龙钉死——全量回归 **620/620** 绿（2026-08-25 演示增强轮：图上取点标绘 + 一键满载演示场景，见 WORKLOG）。后续工作全部为外部条件项（§4.B 七项清单）与用户浏览器实测验收（§4.C）。

**⚠ 口径冲突裁定（2026-08-25 用户明示，优先于一切旧文档）**：
- 前台固定五页；组织架构必须系统自绘、不能贴图（旧 02 号 V0.2「原图不可重绘」条款已废止，V0.3 已定版）；
- 参谋部/AI/LLM 的工作全部后台完成，前台不出现「LLM 增强层」「智脑建议」「参谋部正在」等自述或品牌化区域；T23 的前台 LLM 卡片因此已按本口径移除（后台能力保留）；

## 1. 工作区与运行硬约束（不可回退）

| 项 | 值 |
|---|---|
| 工作区 | `D:/熊猫开发项目/熊猫智脑cola v4`（**非 Git 仓库**；v3 为只读历史基线，勿改） |
| 页面设计唯一口径 | `docs/页面设计定版/`（v4 内副本，7 文件；原 v2 目录 2026-08-27 已归档删除，SHA-256 逐一相同已核）——用户明示「根据这里的文档在开发」，任何页面功能先对文档 |
| Python | `C:\Python314\python.exe`（3.14） |
| 测试跑法 | **只能** `Set-Location "D:/熊猫开发项目/熊猫智脑cola v4/impl"; python -m unittest discover -s tests`。原因：site-packages 有第三方 `tests` 包遮蔽本地目录，`python -m unittest tests.test_x` 会报错 |
| 全量基线 | **657 测试** ≈ 200–300 秒；`OK` 文本为准（勿重定向外部 exe 的 stderr/stdout） |
| Shell | 含 `D:\` 反斜杠根路径的命令会被安全护栏拦截；惯用模式：脚本写入 `$env:TEMP\x.py` 再 `python $x`。命令文本含 `shutdown` 等词会被误拦（如 `server.shutdown()` 直接写在命令行时） |
| 前台 bash | 600 秒上限；长审计勿依赖长程子代理（20 轮上限会中断） |

**项目纪律红线**：权力链 `AdvisorProposal → GeneralDecision → ExecutionOrder → Policy/Approval → Executor`；`workspace.append_event` 是哈希链唯一写入口；fail-closed（校验失败整体降级，禁止猜测定位）；演示数据/待接入接口必须显式标注；不伪造地理配准与距离排序；~~组织架构原图完整显示不用 HTML 重绘~~（2026-08-25 起改为**数据驱动自绘**，见 §0 口径裁定）。**LLM 是增强层不是依赖且无前台出口**：只许改写措辞（P7-T3 边界：证据编号必须原样复述，越界即 `LLMEnhancementRejected` 审计事件 + 回落规则参谋部），联网必须过 `settings.network_enabled` 门，凭证只走环境变量绝不入文件，自检仅限后台调用（`handle_llm_selfcheck`，无路由无卡片）。

## 2. 本日完成任务（P16-T16 → T22，全部已写 WORKLOG/README）

| 任务 | 内容 | 关键文件 | 验收 |
|---|---|---|---|
| T16 | 沙盘阶段条 off-by-one 修复 | `sandtable.py` `SEQUENTIAL_STAGES` | `test_p16_stage_derive.py` 4 用例 |
| T17 | 沙盘交互三件套（03 §5 双向联动/§7 图层开关/§11 测算存草稿） | `sandview.py`、`panel.py` | `test_p16_sand_interact.py` 6 |
| T18 | 文件导入流水线（04 §8：五类对象 CSV/TXT、六态状态机、哈希留痕、回滚） | `data_imports.py`、`panel.py` | `test_p16_data_import.py` 8 |
| T19 | 到场人员八态状态机（04 §4.1：派生态读链上事实+人工判定入链，执行中须挂在效命令） | `personnel_states.py`、名册页 | `test_p16_personnel_states.py` 10 |
| T20 | 复盘权限细化（05 §9 负责人/维护人员/硬约束六条上页）+ 登录小项（01 §7 落点 events、§8 TTL/解锁、公共屏 /public） | `panel.py`、`reviewview.py`、`review_domain.py` | `test_p16_review_roles.py` 8 |
| T21 | 十项验收主线演练（11 §8）：对象级取证 + 验收记录落档 | `acceptance.py` | `test_p16_acceptance.py` 4；记录在 `docs/验收记录_十项主线_汉江72h_P16.md` |
| T22 | **联网 LLM 传输层实装**：`providers.py` 三槽真实 HTTP（纯标准库 urllib，OpenAI 兼容，temperature=0）；`settings.py` 受审新键；`panel._build_enhancer` 按配置自动装配 | `providers.py`、`settings.py`、`panel.py` | `test_p16_llm_providers.py` 9（本地假服务器，不碰外网）；**真实冒烟通过**（DeepSeek 改写措辞+证据复述校验） |
| T23 | **LLM 小闭环（待办 A-1）**：火情事件页「LLM 增强层」卡片（装配失败/未启用原因如实上页、启用显 provider·model·端点）；「LLM 连通自检」POST /llm/selfcheck → 最小调用成败均入审计链（LLMSelfCheck，载荷不含凭证）；另修正 tests/__init__ 假安全（discover 跑法下不执行，涉网用例 setUp 自摘凭证） | `panel.py`、`tests/__init__.py` | `test_p16_llm_selfcheck.py` 9；e2e 首轮即真实打通 DeepSeek（523ms OK） |
| A3 | **首页零散小项（待办 A-3）**：名册面板叠加离场/撤离与判定态（八态推导单一事实源，芯片带依据，「未到场」词汇不变）；离场者剔出实到计数+名单后缀；角标灰 #8A97A5→#647382 改齐色板（选择改色而非豁免） | `homepage.py` | `test_p16_homepage_states.py` 8；既有 homepage 三套件零改动通过 |

测试基线轨迹：448（迁移）→461→484→490→498→502→508→516→526→534→538→547→556→564→570→581（P17-P1：收件箱新域 + 接入语义重写）→581（P17-P2：自绘验收 15 用例替代归档锚点套件 15，总数不变）→**591**（P17-P3：沙盘业务化收口 +10）→**598**（P17-P4：人员物资页收口 +7）→**599**（P17-P5：A-2 严格分离 +1 专项钉，22 处既有用例改走负责人会话）→**606**（P17-P6：八步主线 E2E +7）→**612**（修复轮：五页页壳结构钉 +6）→**620**（演示增强 +8）→**633**（P18 指挥工作台第一批：新壳/图上绘制/决断路由 +13）→**637**（P18 第二批：首屏收敛/代绘/推演 +4）→**646**（P18 第三批：时间轴推演+目标圈 +9）→**657**（P18 第四批：全中文+离线瓦片包 +11）。

## 2.0 P18 沙盘指挥页重排（2026-08-26，设计定版 `docs/18`）

| 批次 | 内容 | 状态 |
|---|---|---|
| 第一批（已落地） | 页面壳（地图主区+工具栏+参谋对话框/方案区+底部条）、Leaflet 真实地图三分层、九类图上直接绘制（geo 权威标绘）、目标/缺口/自提/决断五条将军路由入链、演示场景带坐标满载上图 | 633 OK |
| 第二批（已落地） | 首屏收敛（单行状态条/图标栏/水平工具条/双页签/抽屉单行）、无坐标标绘（旁侧信息窗）、参谋代绘候选链、方案地图推演层（sim_shapes+查看推演） | 637 OK |
| 第三批（已落地） | 时间轴推演（T+2/6/12h 预测火线+富余判定上卡上图）、目标圈附加图例、公共屏隔离推演载荷（docs/18 第 19 章） | 646 OK |
| 第四批（已落地） | 前台全中文（预置库/模板/异常文本/单位，测试钩同步）；底图四源切换（高德默认）+本地离线瓦片包（tileserve+docs/19） | 657 OK |
| 后续批次 | 本地 MBTiles 底图、影像拖拽粗覆盖、命令拆解与执行人任务收件箱、时间滑杆、移动端完整抽屉 | 待做 |

## 2.1 五页重对齐后已完成（P17，2026-08-25）

| 任务 | 内容 | 关键文件 | 验收 |
|---|---|---|---|
| P17-P1 | 外部报警收件箱：FIRE_EVENT 消息一律先落「待确认」，指挥员核对后确认建档（复用手动登记同构 create_event，fire-{n} 编列、来源标「外部报警·系统名」）/拒绝（原因必填）/重复关联；报文 SHA-256 与关联经镜像写入事件哈希链；`ingest_fire_event` 改为收件箱门面，旧直通建档废止；真实端点如实标注待契约评审，演示入口带显式演示标注 | `alarm_inbox.py`（新）、`fire_events.py`、`panel.py` | `test_p17_alarm_inbox.py` 11（含重复关联审计镜像钉）；`test_p16_fire_events.TestIngestFireEvent` 重写 11 |
| P17-P5 | 复盘权限收紧（A-2 严格落实）：六条 `_OWNER_ONLY_ROUTES` 改仅负责人本人会话（指挥员一律 403）；前台表单 owner_mode 门控（假设核实/整改流转/经验确认/报告编辑定版），指挥员只读+归属提示；权限卡删「代行」注记改严格声明 | `panel.py` 门禁、`reviewview.py` 表单门控 | 新增六路由逐条 403 钉；22 处既有用例迁移至 `_owner_session()`；报告页断言翻转 |
| P17-P4 | 人员物资页收口：长期人员库与编制概览首卡（人数/单位分布/32 组编制合计，取自 org_model）；本次到场人员八态卡（只读+依据悬停+本人行高亮+脱敏矩阵）；读写权限改按观看者身份判定（修复成员在指挥员登录期间看到维护表单的显示层越权）；缺口卡去后台来源名词改业务测算口径 | `panel.py`（render_materials_page 重构+路由传 viewer 身份） | 新增 `test_p17_materials_biz.py` 7（编制概览行/八态+脱敏/已撤离依据/空名册诚实态/业务措辞/全角色禁词/成员零维护表单） |
| P17-P3 | 沙盘指挥页业务化收口：右栏统一为事实/判断/建议/指令四类分区（doc 03 §4）；公共屏不见建议区（候选/方案/演示入口全不出）；方案比较升级为可比较方案卡（风险/禁用条件/待决断标注）；图像分析与候选生成为后台任务结果语义，演示按钮改「模拟后台送达」；语音保留待接入声明、无参谋运行状态 | `sandview.py`（右栏重构+影像卡措辞）、`test_p16_sand_page.py` 两处按钮断言更新 | 新增 `test_p17_sand_biz.py` 10（四类分区各一次/判断块/方案卡/公共屏隐藏/禁用词三角色扫描） |
| P17-P2 | 首页组织架构数据驱动自绘（02 号 V0.3）：结构唯一来源 org_model；`homepage.py` 删除原图叠加与锚点机制；确定性布局纯函数 `org_layout()` + SVG 连线；组卡内直显编制上限/到场角标（无名册/未排班如实标注）；登录人按签到绑定/名册指派入组高亮「姓名（我）」；点击节点右侧全字段名单；移动端滚动+四档缩放；原图静态路由下线 | `homepage.py` 重写、`panel.py` 静态路由、新定版 `02_首页组织架构自绘_V0.3.md` | 新增 `test_p17_homepage_selfdraw.py` 15；翻转 route 三处旧图钉；锚点老套件归档至 `impl/tests/_archived_p17_p2/` |
| P17-P0 | 删除 `/events` 前台 LLM 卡片+自检路由（`_render_llm_card`/`llm_status`/`POST /llm/selfcheck` 移除；`handle_llm_selfcheck` 留后台）；主导航冻结五页（指挥员含火情登记，成员不含；旧 /map /overview /timeline /drill 兼容退出导航；沙盘正文不再引用总览页）；成员登录落「/」（未刷码回登录门禁）；首页标题改「人员架构」；五页去 AI/参谋品牌化（智脑候选→待核验候选、AI 草稿→系统草稿〔枚举不变〕、智脑硬约束→自动分析硬约束、智脑分析中→处理中等） | `panel.py`、`sandview.py`、`imagery.py`、`sandtable.py`、`workspace.py`、`reviewview.py`、`review_domain.py` | `test_p17_fivepage_p0.py` 9 钉（导航定序/旧路由兼容/成员导航/登录落点/标题/全五页禁用词扫描）；`test_p16_llm_selfcheck.py` 重写为 6（前台出口不存在 + 后台审计四路径）|

## 3. LLM 接入现状（T22 详情，新对话必读）

- **配置**（`impl/config/settings.yaml`，已入库）：
  ```yaml
  network_enabled: true
  llm_provider: openai_compatible
  llm_model: deepseek-chat
  llm_base_url: https://api.deepseek.com
  llm_timeout_seconds: 30
  ```
- **凭证**：DeepSeek key 已 `setx LLM_API_KEY` 持久化到用户环境变量（**不在任何文件里**）。`panel._build_enhancer` 只从 `LLM_API_KEY`（nvidia 槽另认 `NVIDIA_API_KEY`）读取；无 key → 工厂拒装 → `enhancer=None` 规则参谋部原样运行，`state.llm_notice` 承载说明。
- **测试防护**：`impl/tests/__init__.py` 会 `os.environ.pop("LLM_API_KEY")`——自动化套件**永不打真外网**（doc 11 §6.4 可复现性）。新增测试若需要 provider 行为，用 `test_p16_llm_providers.py` 的本地假 OpenAI 服务器模式。
- **消费点**：目前唯一消费方是 `StaffCouncil(enhancer=...)` 的评估措辞增强（`llm_enhance.py`）。改 `panel.chief` 属性即换绑定处。
- ~~**已知的两个小开放环**~~ 均已于 P16-T23 闭环：① `llm_notice` 与实时装配状态已渲染进 /events 页「LLM 增强层」卡片（`_render_llm_card`，装配失败红卡+未启用原因原文）；② 「LLM 连通自检」按钮已上线（POST /llm/selfcheck，最小调用成败均回显并入审计链）。
- **旧测试契约更新**：`test_p7_providers` 惰性实例断言、`test_p8_network_gate` 的「仓库配置=已拍板在线」断言（含「配置文件中永不允许出现 `sk-` 凭证」检查）——改 `settings.yaml` 时注意这两个钉。

## 4. 待办队列（按建议优先级）

**A. 无外部依赖，按五页规划顺序开工（详细任务书见 `docs/DEVELOPMENT_PLAN_PAGES_V1.md` §6）**

0. ~~**P1 火情登记页双来源闭环**~~ 已完成（P17-P1，见 §2.1）；
0.1 ~~**P2 自绘人员架构页**~~ 已完成（P17-P2，见 §2.1；「不引用 org_chart.png/org_anchors.json」钉已启用）；
0.2 ~~**P3 沙盘指挥页业务化收口**~~ 已完成（P17-P3，见 §2.1；四类分区+公共屏收紧+后台任务语义）；
0.3 ~~**P4 人员与物资页五页化收口**~~ 已完成（P17-P4，见 §2.1；长期库入口+八态上页+观看者身份权限+业务口径）；
0.4 ~~**P5 复盘页权限与前台语言收口**~~ 已完成（P17-P5，见 §2.1；A-2 六类结论级动作仅负责人本人会话，非负责人 POST 全部 403）；
0.5 ~~**P6 五页端到端验收与外部接入准备**~~ 已完成（P17-P6，见 §2.1 与 §4.B 清单；八步主线 E2E 一条龙钉死，规划 P0–P6 全部执行完毕）。
1. ~~**LLM 小闭环**~~ 已被五页口径部分推翻（P16-T23 → P17-P0）：前台卡片/按钮已删，后台自检保留（见 §2.1）；
2. **严格角色分离**＝规划 P5，不再单独立项：指挥员不再代行结论级动作（hyp/rect/exp/report 六路由仅负责人），需同步改造 `test_p16_review_page.py`/`test_p16_root_cause.py` 约 15 处夹具（先指派负责人再动作）；
3. ~~**02 首页零散小项**~~ 已完成（P16-A3，2026-08-25）；其贴图实现将在 P2 被数据驱动自绘替代；
4. **趋势六项无源指标**（需拍板）：给订单加 deadline 解锁「任务按时完成率」、沙盘加成员回执解锁「命令接收率」——动域模型，P7 纪律要求先证明解决已观测问题。

**B. 外部条件卡住（不要主动推进，等用户给条件）**
- docs/17 v1.5 平台对接（webhook/派发 API + 订单「已接收」态一并补）——等契约评审；收件箱链路已就绪，接端即可用；
- ASR 语音输入源（T13 分流层已就绪，缺音频入口，页面已标注待接入）；
- 物资系统实时库存接口（库存「未知·等待物资系统接口」诚实边界已在页）；真实无人机/气象/水文数据源（docs/15 口径）；
- 影像分析后台任务真实引擎（当前演示按钮模拟「后台候选送达」，链路与真实一致）；
- 通信状态数据源（全站统一「通信状态：待接入」，不虚构在线/离线）；
- 04 §8 Word/Excel/PDF/OCR 解析引擎（导入流水线架构已格式无关，接上引擎即可插解析器）；
- 在线底图网络依赖：OSM 瓦片需外网，离线部署需本地瓦片方案（失败横幅已如实提示「底图不可用」）；

**C. 用户手工事项**
- ~~扑救二组角标锚点标定~~ 已作废（P2 起组织架构为数据驱动自绘，无图无锚点，无需标定）；
- 浏览器实测验收（§7.1 最后一条）：双击 `启动熊猫智脑.bat`（端口 8642）桌面+移动端各过一遍五页，检查节点重叠/文字溢出/按钮可点/页面不空白；静态快照在 `demo_snapshot/` 可先目检。

## 5. 本轮踩过的坑（新对话免重蹈）

- **页面壳 > 内容断言**：业务面 GET 必须经 `_wrap_business_page` 套壳返回整页；直接返回视图函数裸片段＝无样式乱页，而内容子串断言的测试永远抓不到（`test_p17_page_shell.py` 已钉：DOCTYPE 开头、`</html>` 结尾、含 style/顶栏/本页高亮）；
- **复用他页 class 前先查规则存在性**：`.sand-grid/.sand-left/.sand-right` 曾在全部样式表里零定义，沙盘三栏从未生效；布局类统一归页壳 `_CSS` 定义，视图模块不重复写；
- **write 工具默认 LF**：.bat 必须转 CRLF 且避免多行 `if ( )` 块，否则双击闪退（cmd 对裸 LF 批处理解析崩溃）；
- **沙箱进程树随工具调用结束回收**：跨调用存活的本地服务用 `Invoke-CimMethod Win32_Process Create` 脱离启动；`bash_background` 工具 Windows 不可用（找 /bin/bash）；
- **浏览器验证优先 act evaluate 取几何数值**（坐标/尺寸/overflow），截图会被地图瓦片等外部请求拖到超时；
- `f-string` 内嵌 HTML 属性丢闭合花括号（`value="{esc(x)}"` 写成 `value="{esc(x)">`）——本窗口已犯两次，写 HTML 拼接后**必先 py_compile**；
- 成员/负责人会话测试：`_NoRedirect` opener 下 **303 重定向会以 HTTPError 抛出**，取 Set-Cookie 要在 `except HTTPError` 分支里拿；登录成功判定不能只看 200；
- 事件载荷属性名是 `Event.payload` 不是 `.data`；
- `normalize_phone("")` 会抛 ValueError——调用前必须判空；
- 导入/回滚快照必须是发布前**深拷贝**（`vars(v)` 逐对象），存活引用会被后续 mutation 污染；
- 新增面板路由前先看它落在哪个前缀门里（`/events/` 前缀块外层条件会吞掉非 events 路径——`/account/unlock` 曾因此 404）；
- workspace 事件流对 `PersonCheckedOut` 这类**非标记类**联动（清旁挂态）要在回放循环里显式处理。
- `python -m unittest discover -s tests` **按文件路径加载用例，`tests/__init__.py` 不执行**——T22 的「包入口摘除 LLM_API_KEY」防护实际无效（2026-08-25 T23 实测坐实：环境里 key 真其实在位，首轮 e2e 直接打通 DeepSeek）。凡涉 LLM 的测试模块必须在 setUp 里自行摘钥+恢复（参照 `test_p16_llm_selfcheck.py` 的 `_strip_llm_credentials`）；tests/__init__.py 注释已改口如实说明。

## 6. 收工要求

完成任务后：`impl/docs/WORKLOG.md`（实为 `v4/docs/WORKLOG.md`）补条目（做了什么/验收用例数/全量测试数/遗留与理由），`v4/README.md` 进展列表加一行，全量回归必须绿。向用户汇报：改动文件绝对路径链接 + 验证命令与结果 + 剩余风险，如实直说。
