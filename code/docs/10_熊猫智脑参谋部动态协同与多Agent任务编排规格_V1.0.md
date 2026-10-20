# 熊猫智脑参谋部动态协同与多 Agent 任务编排规格 V1.0

> 适用工作区：[熊猫智脑cola v4](<D:/熊猫开发项目/熊猫智脑cola v4>)
>
> 本文是对 [09_熊猫智脑将军参谋与多Agent协作重新对齐基线_V1.0.md](<D:/熊猫开发项目/熊猫智脑cola v4/docs/09_熊猫智脑将军参谋与多Agent协作重新对齐基线_V1.0.md>) 的协同编排细化。它回答的不是“如何让多个 Agent 开会”，而是：**面对一个目标时，参谋长如何按需组织专业参谋，使他们真的能共同推进你的判断与行动。**

---

## 1. 核心结论

多 Agent 的价值不来自人数，也不来自让它们互相聊天；价值来自把一个复杂目标拆成不同性质的工作，并在正确的时刻让正确的参谋介入。

```text
将军给出目标、约束和现场判断
        ↓
ChiefAdvisor 判断当前缺口与风险
        ↓
选择最小必要参谋组合，签发明确任务契约
        ↓
并行收集 / 分析 / 质疑 / 评估，全部写入共享态势
        ↓
在证据、方案或风险达到交接条件时汇合
        ↓
参谋长展示材料、分歧与需要你取舍的点
        ↓
你修正方向或形成 GeneralDecision
        ↓
仅对已确认命令执行，并由独立岗位核验
```

系统不是“会议驱动”，而是**任务驱动、证据驱动、由将军掌控的参谋协同**。需要深入多视角推演时，可以按需调用人物/流派视角；它是一个可插拔的分析资源，不是常驻角色，更不是主流程。

---

## 2. 参谋长的真正职责

`ChiefAdvisor` 不是汇总器，也不是第二个将军。她的价值在于持续回答五个问题：

1. 当前目标推进到哪一步，尚缺什么信息、判断或条件？
2. 这一步需要哪些不同能力，而不是需要多少个 Agent？
3. 哪些工作可以并行，哪些必须等待前序证据或你的确认？
4. 哪些分歧应由参谋继续消化，哪些已经属于你的价值取舍或现场判断？
5. 何时应该停止分析、向你汇报、请求补充，或推进到下一阶段？

她只拥有编排权，不拥有业务决断权：

| 可做 | 不可做 |
|---|---|
| 把目标转成问题树、任务图和工作节奏 | 替你定义真实目标和优先级 |
| 选择最小必要岗位并分派任务 | 用 Agent 多数意见替你拍板 |
| 管理输入、输出、依赖、时限和状态 | 将未验证推断写成事实 |
| 汇合分歧、暴露假设和证据缺口 | 悄悄删除少数意见或你的修正 |
| 提议下一步和需你取舍的问题 | 生成或修改 `ExecutionOrder` |
| 监督执行、核验和复盘的交接 | 以“效率”名义跳过你的确认 |

---

## 3. 从目标到任务图，而不是从角色到闲置队伍

### 3.1 目标分解的四种工作类型

参谋长先识别工作的性质，再分派岗位。一个工作项可具有多个类型，但每个类型必须可见。

| 工作类型 | 要解决的问题 | 首选岗位 | 典型产出 |
|---|---|---|---|
| `fact_gap` | 我们缺少什么可信事实？ | ResearchAgent、VerificationAgent | Evidence、来源质量、缺口 |
| `interpretation` | 已有事实意味着什么？ | AnalysisAgent | Claim、假设、情景判断、置信度 |
| `option_design` | 有哪些现实可行的路径？ | OptionAgent、FeasibilityAgent | OptionSet、前置条件、成本与可逆性 |
| `risk_challenge` | 我们可能错在哪里、会怎样失败？ | RedTeamAgent | 反例、失败路径、No-go 条件 |
| `execution_preparation` | 命令能否被安全、完整地执行？ | FeasibilityAgent、ExecutionAgent | 参数、资源、动作契约、停止条件 |
| `verification` | 结果是否真的达成目标？ | VerificationAgent | 独立 Observation、VerificationResult |
| `learning` | 下次应保留什么经验或修正规则？ | MemoryAgent、ChiefAdvisor | Review、记忆候选、待验证规律 |

### 3.2 最小必要编制

默认不启动完整参谋部。参谋长要给出“为什么需要”和“为什么不需要”的理由：

```text
目标：...
当前阶段：...
待解决问题：...
建议启用：ResearchAgent + AnalysisAgent
未启用 RedTeamAgent 的原因：当前是低风险、可逆的信息整理
并行关系：Research 与已有约束提取可并行；Analysis 等待 Evidence Pack v1
停止条件：两条独立来源一致，或到达调研预算
下一次向将军汇报点：事实底稿完成后
```

最小组合示例：

| 场景 | 编制 | 原因 |
|---|---|---|
| 查询某个可核实事实 | ResearchAgent，必要时 VerificationAgent | 不把简单检索扩展成分析大会 |
| 梳理当前局势 | ResearchAgent + AnalysisAgent | 先有可信材料，再形成解释 |
| 比较两个可逆方案 | AnalysisAgent + OptionAgent | 重点是比较取舍，不必默认加入所有岗位 |
| 重大、难回滚或外部影响决策 | Research + Analysis + Option + RedTeam + Feasibility | 需要事实、路径、反证与执行前提共同闭合 |
| 已确认命令的执行 | ExecutionAgent + VerificationAgent | 执行与验收隔离，其他岗位不重复介入 |
| 执行异常 | ChiefAdvisor + VerificationAgent，按异常增派 Research / Feasibility / RedTeam | 先确认发生了什么，再决定是否改令 |

---

## 4. 任务契约：Agent 不是接一句自然语言就自由发挥

每个被派发的 `StaffTask` 必须有完整边界。没有任务契约的工作只能作为草稿，不能影响下一阶段。

```text
staff_task_id
workspace_id
goal_id
phase
owner_agent
work_type
question_to_answer
expected_deliverable
allowed_inputs[]
allowed_sources[]
forbidden_actions[]
dependencies[]
quality_checks[]
time_or_cost_budget
stop_conditions[]
escalation_conditions[]
output_schema_version
status
```

关键字段含义：

- `question_to_answer`：一个可验证、可交接的问题，不能是“全面研究一下”；
- `allowed_inputs`：明确可读取的工作区版本与上游产物，防止状态漂移；
- `allowed_sources`：信息源与工具的权限边界；
- `expected_deliverable`：规定是 Evidence、Claim、OptionSet 还是 VerificationResult；
- `quality_checks`：最低来源、反例、时间、置信度或计算检查；
- `stop_conditions`：何时停止继续搜集或推演，避免无限工作；
- `escalation_conditions`：何时不能自行推进，必须回到参谋长或你。

每个产物必须能反向链接到任务契约、输入项和产生者。这样你能知道一项结论是谁、基于什么、为了回答什么问题形成的。

---

## 5. 工作节奏：分散并行，受控汇合

### 5.1 四个协同阶段

```text
A. 定向：明确目标、约束、成功标准和本轮问题
B. 探索：按任务图并行收集、分析、质疑
C. 汇合：检查证据、冲突、依赖和完成条件
D. 交接：形成下一轮任务、决策材料、命令草案或复盘
```

参谋部不是“所有 Agent 同时跑”，而是围绕以下依赖关系执行：

```text
目标与约束
  ├─→ ResearchAgent ───→ Evidence Pack
  ├─→ FeasibilityAgent ─→ 资源与限制
  └─→ AnalysisAgent（只可基于已存在 Evidence / Constraint）

Evidence Pack + Analysis
  ├─→ OptionAgent ─────→ OptionSet
  └─→ RedTeamAgent ────→ ChallengeSet

OptionSet + ChallengeSet + Feasibility
  └─→ ChiefAdvisor ────→ DecisionBrief → 将军
```

### 5.2 并行的条件

只有同时满足以下条件才并行：

1. 两项任务的输出互不依赖；
2. 不会写同一个事实或方案对象；
3. 同一来源的访问不会造成重复、冲突或预算失控；
4. 结果可以按不同版本回收；
5. 即使其中一项失败，其他项仍有独立价值。

否则使用串行或扇出后汇合。参谋长要明确记录依赖，不能让 Agent 因为看到同一目标就互相覆盖工作。

### 5.3 汇合门（Rendezvous Gate）

只有到达汇合门，参谋长才可以整合产物。主要汇合门如下：

| 汇合门 | 进入条件 | 参谋长检查 | 后续动作 |
|---|---|---|---|
| `evidence_ready` | 任务已完成或到达调研停止条件 | 来源、时效、冲突、缺口 | 交给分析或向你报告信息不足 |
| `analysis_ready` | 事实与推断已分开 | 假设、置信度、证据覆盖 | 发起方案设计或反方审查 |
| `options_ready` | 每个方案有边界与前提 | 是否有伪选项、是否遗漏现实路径 | 组织可行性与风险审查 |
| `decision_ready` | 取舍已清楚，未知已显式保留 | 哪些仍需你决定、No-go 条件 | 生成 `DecisionBrief` |
| `execution_ready` | 已存在 `GeneralDecision` | 参数、范围、回滚、审批、停止条件 | 生成命令草案供你确认 |
| `verification_ready` | 执行结果与独立观察齐备 | 是否真正满足业务成功标准 | 汇报、复盘或异常升级 |

汇合门不是为了增加审批，而是防止“尚未有可信事实就讨论方案”或“尚未有命令就开始执行”。

---

## 6. 共享态势：让协同可见、可修正、可追责

所有岗位围绕一个版本化 `Workspace` 工作。共享态势不是聊天记录，而是明确区分对象类型的共同黑板：

```text
Goal / Constraint / SuccessCriterion
Evidence / Source / Observation
Claim / Hypothesis / Assumption
Risk / Challenge / InformationGap
Option / Prerequisite / NoGoCondition
GeneralInput / GeneralDecision
ExecutionOrder / ExecutionReport / VerificationResult
Review / MemoryCandidate
```

### 6.1 写入所有权

| 对象 | 可创建者 | 可修改者 | 可确认者 |
|---|---|---|---|
| Evidence | Research / Verification | 创建者可补充元数据，原文只追加 | Verification 或将军可标记可信度 |
| Claim / Hypothesis | Analysis / Option / RedTeam | 创建者；他人只能质疑或引用 | 不可自动确认成事实 |
| Risk / Challenge | RedTeam、Feasibility、Verification | 创建者；参谋长可关联处理项 | 将军决定是否接受业务取舍 |
| OptionSet | OptionAgent | OptionAgent；参谋长可整理对比格式 | 将军选择、修改或否决 |
| GeneralDecision | 将军 | 将军 | 将军 |
| ExecutionOrder | ChiefAdvisor 依据 GeneralDecision 起草 | 起草者；将军确认前可修订 | 将军确认，Policy 复核 |
| VerificationResult | VerificationAgent | VerificationAgent | 将军接受业务结果 |

### 6.2 分歧不等于阻塞

当两个 Agent 出现冲突，参谋长先分类：

| 分歧类型 | 处理方式 |
|---|---|
| 事实冲突 | 回到来源与核验；不以平均数解决 |
| 假设冲突 | 标明各自条件，设计最小验证或情景推演 |
| 预测差异 | 展示概率区间、影响范围和改变结论的信号 |
| 价值取舍 | 保留双方理由，升级给将军决断 |
| 任务边界冲突 | 参谋长重写任务契约，禁止继续重复劳动 |

只有事实冲突未消除且会影响安全或执行时，才阻塞后续；价值取舍不应被伪装成“需要更多数据”来拖延你的决断。

---

## 7. 何时请示将军

参谋长不是每做完一小步都向你打断，也不能在需要你决定时自己补完。以下事件必须形成清晰、短的 `GeneralQuestion`：

| 触发器 | 向你呈现的内容 | 默认动作 |
|---|---|---|
| 目标或成功标准有歧义 | 不同理解、影响、建议澄清问题 | 等待你的补充 |
| 信息源受限或证据不足 | 已查范围、缺口、继续成本、可能影响 | 由你决定补查、降级或暂停 |
| 出现真实价值取舍 | 方案、收益、损失、不可逆后果 | 等待你的选择或约束 |
| 风险等级上升 | 原命令、发现的新风险、无法覆盖部分 | 暂停并请示 |
| 命令超出原范围 | 已完成部分、拟新增动作、理由 | 不扩展执行，等待改令 |
| 参谋结论发生关键分歧 | 共同事实、分歧点、各自依据、可验证项 | 由你决定继续验证或取舍 |
| 达到分析预算仍无结论 | 已得到什么、为什么不足、可选下一步 | 等待你决定停止或追加资源 |
| 执行或核验异常 | 已观察到的结果、影响、回滚可能 | 停止受影响动作并请示 |

对将军的请示必须包含“我需要你决定什么”，不能只堆积 Agent 原文。

---

## 8. 把建议变成决策材料，而不是替你决策

参谋长提交的 `DecisionBrief` 采用固定结构：

```text
本次要决定什么
共同事实与证据范围
仍未解决的未知项
候选路径及各自前提
关键风险与 No-go 条件
参谋的一致判断
仍保留的不同意见
参谋长的推荐及其理由（可为空）
最可能改变推荐的新证据
需要将军明确的取舍 / 参数 / 停止条件
```

你的响应必须是显式 `GeneralDecision`：

```text
adopt | modify | reject | defer | request_research | propose_new_option
selected_option
changes_to_scope_or_constraints
accepted_risks
rejected_risks
execution_intent
rationale_optional
```

只有当 `execution_intent` 明确且命令参数完整时，参谋长才可起草 `ExecutionOrder`。Agent 推荐、模型置信度和多数意见均不能替代你的决断。

---

## 9. 按需调用的扩展能力

### 9.1 人物/流派视角

经蒸馏的人物或流派视角可作为 `AnalysisAgent` 或 `RedTeamAgent` 的一次性分析插件，用于：

- 当前框架高度同质化；
- 需要检验某个特定的风险、激励或产品取舍；
- 你明确要求采用某种思考框架；
- 重大复盘需要不同的解释模型。

使用要求：有明确的不可替代变量、共享证据、输出边界和结束条件。它们不形成常驻“会议”，没有工具权限，也不能改变本规格的任务图和决断链。

相关调研保存在：[女娲蒸馏人物圆桌机制调研笔记_V1.0.md](<D:/熊猫开发项目/熊猫智脑cola v4/docs/research/女娲蒸馏人物圆桌机制调研笔记_V1.0.md>)，仅作为可选能力研究，不是当前开发主线。

### 9.2 多模型

第一版应使用单模型多角色验证协同协议。只有在出现可测量的同质化、质量或成本问题时，再按任务选择模型。模型差异不能代替岗位边界和证据规则。

### 9.3 自动化程度

- L0 观察、整理、低风险分析：可自动按任务图推进；
- 方案比较、风险判断：可自动形成材料，但在关键分歧处请示；
- 会影响外部现实的命令：始终要求 `GeneralDecision → ExecutionOrder → Policy / Approval`；
- 高风险、不可逆、权限或资金相关操作：保持人工参与或拒绝。

---

## 10. 关键架构决策记录

### ADR-10-01：采用任务图编排，而非自由 Agent 对话

- 决策：以 `StaffTask`、依赖关系和汇合门驱动协作；自然语言对话只能解释工作，不是协调协议。
- 原因：可审计、可测试、能限制重复劳动与越权。
- 代价：初期需要定义任务和输出结构；灵活探索必须通过新任务显式加入。

### ADR-10-02：采用最小必要编制，而非固定全员参谋团

- 决策：每次只启用解决当前问题所需的岗位，并记录选择理由。
- 原因：降低成本、等待时间、噪声和虚假共识。
- 代价：参谋长需要具备任务分类与调度能力；漏掉关键岗位是需要测试的风险。

### ADR-10-03：采用共享态势与对象所有权，而非 Agent 间私有摘要传递

- 决策：Evidence、Claim、Option、Decision 等写入同一版本化工作区，产物可追溯。
- 原因：让你看到证据链，保留分歧，防止摘要失真和状态覆盖。
- 代价：需要版本、权限、冲突状态和存储设计。

### ADR-10-04：以汇合门和停止条件控制分析过程

- 决策：每一阶段都定义进入、退出、升级和预算条件。
- 原因：避免 Agent 无限收集、过早推演或为展示而增加工作。
- 代价：规则需要随真实使用迭代，不能追求一次定义完整。

### ADR-10-05：人物视角仅作为按需分析插件

- 决策：不建设常驻圆桌；只有具体任务缺少某个认知变量时才调用蒸馏视角。
- 原因：保证专业岗位、事实证据和将军决断始终是主线。
- 代价：不会获得持续的拟人化陪伴感，但避免角色表演吞噬协同效率。

---

## 11. 开发优先级

```text
P0  Workspace、任务契约、产物溯源与对象所有权
P1  ChiefAdvisor 的任务分类、最小编制和汇合门
P2  Research + Analysis + RedTeam 的离线协同
P3  Option + Feasibility + DecisionBrief + GeneralDecision
P4  ExecutionOrder、Policy、Execution 与独立 Verification
P5  异常处理、复盘、记忆与任务编排改进
P6  按需视角插件、多模型选择和真实执行器
```

第一版只需验证一个代表性案例：将军提出有现实约束的目标，参谋长能分派两到四项不同任务，保留冲突和证据，按阶段向将军请示；未经决断不能执行，执行后由独立岗位核验。

---

## 12. 验收场景

| 编号 | 场景 | 必须证明 |
|---|---|---|
| C01 | 最小编制 | 低风险事实任务不启动完整参谋部，且说明未启用岗位的理由 |
| C02 | 正确并行 | 互不依赖的任务并行；依赖 Evidence 的分析不会抢跑 |
| C03 | 任务边界 | 每个 Agent 只处理自己的 `StaffTask`，不得扩张范围或调用未授权工具 |
| C04 | 可见证据 | 将军可以从任何结论回溯到任务、输入、来源和产生者 |
| C05 | 汇合门 | 证据不够时不能生成伪确定的方案；证据到位才进入下一阶段 |
| C06 | 分歧处理 | 事实、假设、预测和价值分歧采用不同处理方式，少数意见保留 |
| C07 | 将军介入 | 将军补充约束或否决路径后，参谋长能重新编排下游任务 |
| C08 | 分析止损 | 达到预算、证据饱和或命中升级条件时停止，而不是无休止扩展 |
| C09 | 执行隔离 | 没有 `GeneralDecision` 和有效 `ExecutionOrder` 时，所有外部执行请求被拒绝 |
| C10 | 独立核验 | ExecutionAgent 报告成功不等于任务完成，必须有 VerificationResult |
| C11 | 异常请示 | 发现范围外动作、风险升级或结果异常时暂停并向将军说明需要的决定 |
| C12 | 视角降级 | 不需要额外思维框架时不调用人物视角；调用时只作为有边界的分析插件 |

---

## 13. 最终原则

熊猫智脑的参谋价值，体现在它能主动而克制地组织工作：该并行时并行，该质疑时质疑，该停止时停止，该请示时把真正需要你取舍的问题讲清楚。

```text
不是：多个 Agent 轮流说话
而是：多个专业岗位围绕同一目标，在受控任务图中贡献各自不可替代的工作
```

将军始终掌握目标、取舍、命令与结果接受权；参谋长和专业 Agent 的职责，是让你的决断建立在更完整、更透明、更可验证的基础上。
