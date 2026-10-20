# 熊猫智脑cola v3 — 开发工作日志

> 工作区：`D:/熊猫开发项目/熊猫智脑cola v3`（由「熊猫智脑cola v2」完整复制而来，独立工作区）。
> 注：本工作区不是 Git 仓库，无 `git` 版本历史。

## 待处理事项

- P16-T2：登录页功能定版 V0.3 接线本地控制面板（三区登录页 / /events 火情管理 / 名册配置 / 成员刷码入岗闭环）——进行中。

## 已完成

### 2026-08-24 — P16-T11：沙盘地理坐标标注叠加（03 号 §6 点/线/面上真图）

- map_annotations：MapAnnotation 新增可选 geo_points（纬度,经度元组），与示意顶点一一对应；validate_geo_points 校验范围（纬 ±90/经 ±180）与顶点数一致——不做示意→地理自动投影，未录坐标的标绘绝不上真图。
- workspace.set_annotation_geo：补录/更新坐标，留 AnnotationGeoSet 审计事件；MapAnnotation.with_geo_points 与 with_status 同模式。
- panel：parse_geo_points 解析「纬度,经度」分号分隔表单值（非法显式报错）；handle_sand_annotate 增 geo 字段；新 POST /sand/annotate-geo（指挥员作业，403 拒成员）；标绘作业卡新增地理坐标输入框与逐条补录列。
- sandview._real_map_card 升级：按角色可见性收集带坐标标绘注入 Leaflet 数据（JSON 转义防 </script>），点 circleMarker/线 polyline/面 polygon，未发布虚线半透明；事件无坐标但有带坐标标绘时以标注定位并如实标注「事件未登记坐标」；overlay_note 明示已叠加数、不可见数与「仅显式录入者上真图」纪律。

验收：test_p16_sand_page.py 扩至 19 用例。全量回归 448 通过。

### 2026-08-24 — P16-T10：复盘问询引擎（05 号 §11）

- review_domain 新增 ReviewAnswer 七字段回答（结论/数据范围/引用事件/证据链接/统计口径/未纳入数据/置信度）；关键词路由六大复盘主题：问题/整改/经验/报告版本/共性对比/时间线。
- 命令意图确定性拦截：意图动词（下达/签发/派出等）且无疑问标记 → 一律不执行，只答「复盘页为分析视图，不支持下达命令」；「哪些命令是自动下达的？」这类分析性提问正常路由。
- 沿用沙盘问询引擎：非复盘主题落空时委托 sandtable.answer_inquiry，同数据同权限过滤；两边都不命中则如实列出可问主题。
- 权限过滤：成员只见已确认/已修订/已转整改/已关闭问题与已确认经验、已发布报告；待核验候选计入「未纳入数据」字段明示。共性对比要求 ≥2 事件有记录，否则如实答「样本不足」。
- 只读不变式：问答不写台账、不改任何状态（测试断言 reviews.json 字节不变）；留痕仅内存 cap 20。
- reviewview 新增底部复盘问询卡（七字段渲染，全角色可见）；panel POST /review/ask 置于指挥员门禁之前（指挥员+成员可问，匿名 303 登录），重定向 msg 走 URL 编码避免 latin-1 头错误。

验收：test_p16_review_page.py 扩至 22 用例。全量回归 444 通过。

### 2026-08-24 — P16-T9：复盘报告人工编辑与版本状态机（05 号 §12 后半）

- review_domain 新增 ReviewReport + 状态机：AI 草稿→人工编辑→待审核→已发布→已归档；审核可退回修改；发布/归档后内容不可变（编辑拒 409，提示走新版本）；新证据 → 生成新版本（同事件 version 递增），旧版快照永不静默覆盖；流转全程留痕 history。
- panel：POST /review/report-{generate,edit,status}（指挥员限）；GET /review?tab=report&rid=… 预览指定版本。
- reviewview：新增「报告版本」第七标签页——版本清单（状态章/最后变更/操作按钮按状态出现）+ 版本预览（可编辑 textarea / 只读 pre）+ 状态机说明；生成按钮按当前实时数据产出 AI 草稿全文快照。
- 视觉接口已恢复但仅能输出结构描述、坐标不可信：像素锚点标定仍摘置（后续改用确定性模板匹配方案），首页叠加维持合规侧栏。

验收：test_p16_review_page.py 扩至 13 用例。全量回归 435 通过。

### 2026-08-24 — P16-T8：沙盘增强——真实底图 + 无人机影像导入配准（03 号 §6/§9）

真实底图（§6）：
- 事件登记可选经纬度（FireEventRecord.lat/lon，建事件表单新增坐标输入，parse_coords 显式校验范围）；
- 沙盘页新增「真实底图定位」卡：有坐标 → Leaflet + OpenStreetMap 免费瓦片（无密钥），事件位置标记；无坐标 → 诚实空态引导录入；瓦片连续失败或脚本不可用 → 显式「底图不可用」横幅（不把空白当正常）；
- 纪律：示意标注与地理坐标的配准关系未建立，不做假叠加；标绘仍在示意沙盘。

无人机影像（§9）：
- 新模块 `panda_brain/imagery.py`：DroneImage 全字段记录（来源/采集单位/双时间/类型/空间范围/精度/状态/关联标注）；状态机已上传→待配准/已定位→已叠加→分析中→已生成候选→已确认/已驳回；**未配准硬拦截分析**（双重防线：状态机+registered 守卫）；驳回保留原图与记录；JPG/PNG 魔数嗅探+文件名清洗防穿越+8MB 上限；ImageryLedger 独立 JSON 分库；
- panel.py：极简 multipart 解析器（二进制安全）；POST /sand/image-{upload,status,analyze,decision} 全指挥员限；GET /uploads/{path} 仅指挥员可看原图（doc 03 §12）且路径归一防穿越；演示智脑识别产出【演示】前缀候选标注走既有核验链，确认→标注转已核验、驳回→标注撤回；
- do_POST 改为字节读入再按路由分发（修复 multipart 被文本解码破坏）；upload_dir 可注入测试隔离。

map_annotations 补充：pending_review→withdrawn 流转边（doc 03 §10.2 智脑候选「已驳回」支线）。

验收：test_p16_sand_page.py 扩至 15 用例。全量回归 432 通过。

### 2026-08-24 — P16-T7：复盘与经验页首期（05 号定版上线）

新模块：
- `panda_brain/review_domain.py` 域层：①时间线回放（从审计链确定性重建；审计流无时钟源，时间列如实标「未记录」，不事后编造）；②事件摘要与计划/实际对比（数据状态三态：系统记录/已发生·时间未记录/缺失未补写；差异只表偏差不等同责任）；③问题卡片（标识/事实/判断/状态四组字段，规则驱动候选生成 R1异常命令/R2未关闭缺口/R3岗位未解析/R4草稿标绘，全部待核验+证据+保守置信度；审核状态机待核验→已确认/已修订/不成立→已转整改→已关闭+新证据重开；严重度→范围→编号稳定排序；置信度与核验状态互相独立）；④整改任务（五态机+验证未通过回炉+超期高亮+重开留痕+复现链）；⑤经验三态（候选/已确认/已失效）。ReviewLedger 独立 JSON 分库（impl/data/reviews.json）。
- `panda_brain/reviewview.py` 渲染层：六标签页（本次复盘/时间线回放/问题分析/共性规律/整改闭环/经验沉淀），共性规律在单事件时如实「样本不足」不产出。

panel.py 接线：GET /review 三档角色门禁（匿名 303；成员只读；projector 公共屏摘要）；POST /review/{generate,issue-status,issue-reopen,rect-new,rect-status,exp-new,exp-status} 全部指挥员限；智脑硬约束落地：不定责/假设不当事实写/不自动发整改/经验不直入正式库。

已知边界：问题/整改/经验暂不在哈希审计链内（同 T2 二维码签发级别限制），WORKLOG 已记。

验收：test_p16_review_page.py 10 用例 HTTP e2e（含 Markdown 报告导出）。全量回归 427 通过。

追加：复盘报告导出（doc 05 §12）——`export_review_markdown` 固定九章章节（事件概况/组织到场/态势变化/决断与命令时间线/异常与计划实际差异/AI 候选问题/人工确认结论按指挥作战物资通信分桶/整改任务含超期标记/经验沉淀），头部标「AI 草稿」与审计链完整性，尾部声明定责仅负责人手工形成；GET /export/review 指挥员专属（其余 404）；页面 commander 视图附下载入口。

### 2026-08-24 — P16-T6：指挥调度沙盘页首期（03 号定版上线）

新模块：
- `panda_brain/sandtable.py` 域层：①八阶段状态机真实信号推导（每阶段附可核对数量证据，推导不出如实标「—」，受阻/异常命令触发「异常处置」覆盖态）；②自由区域测算（鞋带公式，示意比例尺 4m/单位与 map_annotations 同口径，结果强制带「示意值·非实测」标注；临时测算不落库）；③态势问询引擎（关键词路由到队伍/火线火点/水源/物资/面积/决策/命令/缺口/待决策九类真实数据，回答六字段：结论/数据时间/来源/位置/置信度/未确认内容；查不到明说无法回答并列出支持主题）；④按角色过滤可见性（指挥员全量/成员可看已核验+智脑候选/公共屏只见已发布）。
- `panda_brain/sandview.py` 渲染层：顶部条（事件/编号/等级/状态/阶段/数据截至/登录人/通信待接入/告警通道待接入）+ 左列（阶段证据表/总体目标/待决策/决策历史）+ 中间示意 SVG 地图（等高线+河流+网格，标绘按类型着色、草稿与智脑候选虚线半透明）+ 右列（态势计数/告警/信息缺口/方案比较/命令与执行反馈/智脑候选确认卡）+ 标绘作业卡（11 类工具表单+核验链流转表）+ 自由测算卡 + 问询卡。

panel.py 接线：GET /sand 三档角色门禁（匿名 303；?projector=1 公共屏模式只渲染已发布）；POST /sand/{annotate,annotate-status,candidate,ai-candidate,measure,ask}——前五者限指挥员，ask 成员可用且按角色脱敏；「模拟智脑识别」按钮生成显式【演示】前缀的 ai-suggestion 候选（pending_review 入库，走与真实候选相同核验链）；问答留痕仅内存会话内。

验收：test_p16_sand_page.py 10 用例 HTTP e2e。全量回归 417 通过。

### 2026-08-24 — P16-T5：人员与物资页接线（04 号定版上线）

panel.py 改动：
- PanelState 扩展 ledger（MaterialsLedger，独立 JSON impl/data/materials.json，路径可注入）；成员确认入岗后按所属中队自动生成到场快照（doc 04 §4，无中队归属不伪造清单）；
- `/materials` 页三段：①长期底账维护（物资档案/中队/携带清单/物资点，表单仅指挥员可见；库存留空显式「未知 · 等待物资系统接口」）②本次到场快照（中队/人数/登记/可用/消耗/缺失/状态/差异原因 + 现场核对表单）③缺口与补给建议（与参谋测算卡同口径：花名册折算分队→物资基线→按名称对齐档案，未建档需求如实列出，最近补给点推荐）；
- POST 路由 /materials/{item|squad|carry|point|check|consume} 全部 command_mode 门禁，成员只读。

验收：test_p16_materials_page.py 5 用例 HTTP e2e（门禁重定向/建账全流程含库外人员拒收+JSON 落盘/入岗自动快照+缺口渲染/现场核对短缺转缺失/成员只读 403）。全量回归 407 通过。

### 2026-08-24 — P16-T3：首页路由接线（02 号定版上线）

panel.py 改动：
- `/` 切换为组织架构原图首页（homepage.render_homepage_view 渲染：原图完整底图 + 到场聚合侧栏 + 成员本人卡）；旧态势总览迁至 `/overview`，导航同步更新；
- 首页门禁 home_gate（01 号 §9）：未登录/仅登录未刷码一律重定向 /login，指挥员与已入岗成员可进；到场统计只信当前打开的事件任务空间，跨空间时显式标注不读数；指挥员未进场显示诚实空态卡引导去 /events；
- 静态资源白名单服务 /static/org_chart.png（image/png，拒绝任意路径）。

测试：新增 test_p16_homepage_route.py 8 用例（门禁三分支/原图服务 PNG 魔数/白名单拦截/成员本人卡完整号码/总览迁移既有断言迁移至 /overview）。全量回归 402 通过。

### 2026-08-23 — P16-T2：面板接线（登录页定版 V0.3 全链路）

新增/改动（impl/panda_brain/panel.py）：
- PanelState 扩展：registry（EventRegistry，默认落盘 impl/data/event_registry.json）、成员会话（member_sessions，内存）、active_event_id；每次注册表变更即落盘 JSON。
- 路由：GET 三区登录页、/events 火情管理页（command_mode 否则 404）；POST /login/member、/logout/member、/checkin/scan、/checkin/confirm、/events/create、/events/action（start/upgrade/close/issue-qr/revoke-qr/enter）、/events/{id}/roster/{add|all-add|assign}、/events/{id}/library-import。
- 入岗闭环：member_scan（无副作用判定，会话丢失幂等恢复）→ 确认卡「即将进入：工作组—岗位」→ member_confirm（ensure_person_in_workspace → confirm_checkin 走 scan_check_in 唯一门 → 会话绑定+到场时间）。
- 浅色 CSS（.lz-*，00 号色板）追加，既有视图零改动；handle_login 加 username=admin 校验（旧语义不变）。

修复（过程中发现）：fire_events.py 的 qr_expiry_iso 缩进破坏类体（已修为 @staticmethod）；_light_wrap format 漏传 body；handle_event_action 补 enter 分支。

验收：tests/test_p16_panel_events.py 6 用例 HTTP e2e——建事件→登记中拒签码→启动→签发→名册→进场→成员登录→刷码确认卡→入岗落账（花名册+到场时间+审计版本递增）→重复刷码幂等不重记→跨事件冲突转人工→成员访问 /events 得 404→错误口令/库外账号显式拒绝。全量回归 394 通过。

已知限制（如实标注）：QR 签发审计仅在注册表 JSON 留痕未入哈希链；外部 HTTP 端点待对接；成员会话仅存内存（重启需重登，符合 01 号 §4.2 幂等补发语义）。

### 2026-08-23 — P16-T3 前置 + P16-T4 领域层（并行完成）

- impl/panda_brain/homepage.py（02 号定版渲染层，21 测试）：原图完整底图显示、到场聚合（应到/实到/点名）、名册外绑定如实呈现、四级电话脱敏矩阵、当前登录人详情卡、通信状态诚实标注「待接入」。路由接线待做（/ 切换为首页，旧总览迁 /overview）。
- impl/panda_brain/materials.py（04 号定版领域层，25 测试）：物资档案/中队/携带清单/物资点底账；到场快照自动载入清单、核对/消耗/损坏差异只记本次不改长期档案；缺口四档分级（无速率数据不虚构「即将不足」）；库存未知显式不用 0 冒充；独立 JSON 持久化。UI 接线待做。

### 2026-08-23 — P16-T1：火情事件域模块（登录与多火情管理地基）

对齐口径：页面设计定版 01_登录页_V0.3 + 增强版PRD §13.3 + 对外数据接口规范 §2/§4/§5/§11。

新增：
- `impl/panda_brain/fire_events.py` — 纯领域层：EventRegistry（多事件/名册/二维码/成员凭证/长期人员库，JSON 原子持久化）、事件生命周期（登记中→进行中→已升级→已关闭，登记中可直关，关闭终态）、QR 规则（仅进行中/已升级可签发、默认 72h 现算过期、撤销留痕）、成员账号（手机号=账号、初始口令 123456 pbkdf2 哈希、连错 5 次锁定、指挥员解锁）、resolve_checkin 十分支匹配判定（幂等已在岗/跨事件冲突转人工/名册未配置等）、confirm_checkin 经注入回调走工作区唯一门 scan_check_in、ingest_fire_event（FIRE_EVENT 信封校验/业务幂等 DUPLICATE/VERSION_CONFLICT/未知枚举待映射不静默）；HTTP 端点本期标注「待对接」。
- `impl/tests/test_p16_fire_events.py` — 60 用例全绿；全量回归 342 通过。
- `impl/static/org_chart.png` — 首页组织架构原图静态资源落地（源：用户附件）。

纪律要点：入岗落账仍走工作区唯一写入口；一切无法验证状态 fail-closed 转人工；长期人员库在注册表层维护，事件空间库仅作桥接副本（doc 04 §9 分离原则）。

### 2026-08-23 — v1 路径引用改写为 v3

原文档从 v1 复制而来，内部大量引用指向 v1 工作区路径（即「熊猫智脑cola」目录，无 v3 后缀），已全部改写为 v3 自身路径（目录名「熊猫智脑cola v3」），使 v3 自洽。仅改 v3 目录内文档（.md），未触碰其他目录，未改动代码文件。

替换的 .md 文件及次数：
- `README.md` — 7（将目录名「熊猫智脑cola」→「熊猫智脑cola v3」）
- `docs/09_熊猫智脑将军参谋与多Agent协作重新对齐基线_V1.0.md` — 1
- `docs/10_熊猫智脑参谋部动态协同与多Agent任务编排规格_V1.0.md` — 2
- `docs/11_熊猫智脑整体架构与分阶段落地路线_V1.0.md` — 5
- `docs/research/女娲蒸馏人物圆桌机制调研笔记_V1.0.md` — 1

另修正 3 处「适用工作区」头部（无斜杠的工作区根路径 + 链接文本）为 v3：
- `docs/10_...` line 3
- `docs/11_...` line 3
- `docs/research/女娲蒸馏人物圆桌机制调研笔记_V1.0.md` line 3

验证结果：
- 全 v3 目录 .md 无残留「熊猫智脑cola」名录（即 v1 工作区路径）引用。
- `熊猫智脑cola v2`、`熊猫智脑cola v3`、`熊猫智脑minimax` 等名称原样保留。
- `.py/.yaml/.json` 等源码文件不含 v1 路径引用（grep 无命中）。
- 已知非问题项：`impl/panda_brain/__pycache__/*.pyc` 编译字节码缓存内嵌旧绝对路径字符串（构建产物，非源码，自动再生成；grep 默认忽略 __pycache__）。

### 备注
- 本文档为 v3 首次创建。
