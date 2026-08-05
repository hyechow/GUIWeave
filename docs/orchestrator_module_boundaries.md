# GUI Agent 当前架构与模块边界

> 状态：当前规范
>
> 更新日期：2026-08-04
>
> 本文描述当前生产架构的职责分配和演进约束。历史实验过程参见
> [Coding Orchestrator 编排实验记录](coding_orchestrator_experiment.md)；旧的显式状态句柄方案参见
> [ctx.API 状态依赖与查询降解](ctx_api_translator_layer_design.md)。两份历史文档不再定义当前接口。

## 1. 文档目的

这套架构要解决两个不同尺度的问题：

- Program 层把用户目标组织成跨页面、跨应用的数据流和业务控制流；
- Statement 层在当前界面上观察、操作、验证，并处理局部的不确定性。

稳定性的关键不是增加更多中间状态，而是让每个模块只回答自己有权回答的问题。本文因此重点规定：

1. 每个模块接收什么、产出什么；
2. 哪个模块拥有哪类决策权；
3. 哪些信息可以跨边界，哪些不可以；
4. 新问题应该落在哪一层，避免继续堆叠 Router、Knowledge、Memory 或 `reach/commit` 状态。

本文不定义单个应用的字段、页面路径或业务含义。这些事实属于 `knowledge/` 或运行时发现结果。

## 2. 总体架构

```text
用户原始任务
    │
    ├── Router ── 可选的语义补充，不改写原任务
    │
    ├── Application Facts ── 应用接口的必要事实
    │
    └── Platform Contract ── 当前平台能力、应用入口
              │
              ▼
        Coding Orchestrator
        生成受限 Python Program
              │
              ▼
       Compiler / Sandbox
       语法、能力和数据流检查
              │
              ▼
         Program Runtime
       执行 ctx.* 业务操作
              │
              ▼
       Statement Executor
       Transition 决定局部动作
              │
              ▼
       Browser / Android / iPhone Adapter
              │
              ▼
             GUI

观察事实：Adapter → EventJournal → StatementMemory → Transition
```

这不是一条“每层都重新理解任务”的链。任务语义、业务编排和 GUI 决策分别只有一个主要所有者：

- 原始任务保存用户语义；
- Orchestrator 负责业务编排；
- Transition 负责当前 GUI 的最终局部决策。

其他模块只能补充事实、验证契约或执行决定，不能建立平行决策中心。

### 2.1 主要代码落点

| 边界 | 当前实现 |
|---|---|
| Router 输入输出 | `gui_agent/core/router/intent.py` |
| Program 生成与修复 | `gui_agent/core/orchestrator/planner.py` |
| 受限 Python 和静态检查 | `gui_agent/core/orchestrator/sandbox.py` |
| `ctx` API 与 Program 执行 | `gui_agent/core/orchestrator/runtime.py`、`models.py` |
| Statement 决策与执行 | `gui_agent/core/supervisor/statement/`、`gui_agent/core/run/statement_transition.py` |
| Journal 与 Memory 投影 | `gui_agent/core/run/statement_memory.py`、`gui_agent/context/` |
| Application Facts 装载 | `gui_agent/core/self_learning/app_summary.py` |
| 平台机制 | `gui_agent/adapters/browser/`、`android/`、`iphone/` |
| 应用事实 | `knowledge/<platform>/<app>/` |

## 3. 核心设计原则

### 3.1 原始任务是不可变语义源

Router 不能翻译、重写或替换原任务。它只能在确有歧义时提供最小语义补充。即使补充为空，常规任务也应能够依靠原始任务、运行时事实和通用推理完成。

### 3.2 Knowledge 是事实，不是计划

Knowledge 只描述应用固有的接口事实。它不能包含 `ctx.*` 程序、任务步骤、benchmark 答案或通用策略。Orchestrator 可以使用事实生成计划，但不能被 Knowledge 中的伪计划驱动。

### 3.3 Program 拥有业务控制流

查询、候选遍历、排序、聚合、跨应用数据依赖和最终业务操作由受限 Python Program 表达。Statement 不应猜测 Program 的后续步骤，Memory 也不应暗中维护另一套任务阶段。

### 3.4 Transition 拥有当前 GUI 决策

Statement 收到局部目标后，由 Transition 结合当前观察、历史事实和平台能力决定点击、输入、滚动、验证、重试或结束。Program 不生成坐标级操作，也不判断复杂界面的中间态。

Runtime 只允许一个窄例外：当 typed adapter predicate 与 typed postcondition 可以精确比较且结果为
`met` 时，可以机械地短路局部完成；`unmet/unknown` 都不能替 Transition 作语义判决。

### 3.5 `reach/commit` 不是业务状态机

`reach` 建立当前业务对象或界面上下文，`commit` 执行持久化变更。它们是 Program API 的语义边界，不是要求 LLM 识别大量阶段的状态机。

内部 `CurrentUI` 只用于运行时把相邻调用连接到同一 GUI 上下文，不作为公开返回值，不进入用户程序，也不承担任务记忆。

### 3.6 确定性检查只检查可证明的不变量

Compiler/Sandbox 可以检查语法、安全能力、调用顺序、字段拼写一致性和数据来源关系。它不能通过应用名、任务关键词或自然语言模板判断业务是否完成。

### 3.7 正确性不能依赖可选增强层

Router 补充、Knowledge、缓存和历史经验都可以提高首次生成稳定性，但不能成为唯一正确路径。关闭其中任一可选增强层时，通用机制应尽量保持可用并清晰失败。

## 4. 模块职责表

| 模块 | 主要输入 | 拥有的职责 | 主要输出 | 明确禁止 |
|---|---|---|---|---|
| 平台入口 | 原始任务、环境配置 | 建立 Browser、Android、iPhone 或 MobileWorld 运行环境 | 原始目标、平台契约、可用应用 | 改写任务语义、编写业务计划 |
| Router | 原始任务 | 只补足会改变目标解释的歧义 | `IntentResolution.semantic_supplement` | 翻译任务、分步骤、指定 `ctx.*`、复制应用知识 |
| Application Facts 装载 | 应用范围、目标 | 选择完成规划所必需的应用接口事实 | 有限的事实上下文 | 把知识变成执行清单、推断当前 UI、决定任务成功 |
| Orchestrator | 原始目标、可选补充、平台契约、应用事实 | 生成业务数据流和控制流 | `CodingProgram` | 点击坐标、维护 GUI 阶段机、发明应用字段、依赖 benchmark 个案 |
| Compiler / Sandbox | Program 源码和静态契约 | 限制 Python 子集并检查通用结构、能力、数据流 | 可执行 Program 或诊断 | 解析应用业务、按关键词修计划、决定 GUI 已完成 |
| Program Runtime | 已验证 Program、`ctx` 实现 | 执行 Python 控制流，把 `ctx.*` 降解为 Statement | 业务值、最终结果、调用轨迹 | 重新规划语义、靠隐式状态补写 Program、替 Transition 判断界面 |
| Statement Executor / Transition | 单条局部目标、当前观察、历史投影 | 选择和验证下一次 GUI 动作，输出局部结果 | typed outcome、读取值、操作结果 | 重写原任务、规划跨应用流程、推断 Program 分支 |
| EventJournal | 运行时事件 | 追加可追溯的历史事实 | 事件序列 | 决策、压缩时改变事实、存储隐藏业务阶段 |
| StatementMemory | EventJournal、当前 Statement | 为当前决策投影有限历史 | 只读上下文 | 成为第二状态机、覆盖 Journal、决定任务意图 |
| Platform Adapter | 平台 API、设备观察 | 提供截图、控件、动作、应用启动等平台机制 | 结构化观察和动作结果 | 理解业务目标、包含跨平台通用策略、写入站点答案 |
| Checker / Eval / Report | 运行结果、任务判定器 | 评估和呈现结果 | 通过/失败及证据 | 反向影响生产决策或成为运行时知识源 |

## 5. 决策权归属

同一个问题只能有一个主要决策所有者：

| 问题 | 主要所有者 |
|---|---|
| 用户明确说了什么 | 原始任务 |
| 原始任务是否存在会改变结果的歧义 | Router |
| 某应用真实支持哪些资源、字段和入口 | 运行时结构化事实；稳定缺失部分由 Knowledge 补充 |
| 应该查什么、如何遍历、如何聚合、何时跨应用 | Orchestrator Program |
| Program 是否安全、结构合法、值来源可证明 | Compiler / Sandbox |
| 当前屏幕是什么、哪些控件实际存在 | Adapter observation |
| 当前局部目标下一步点哪里、是否已经完成 | Transition |
| 之前观察或执行过什么 | EventJournal |
| 当前 Statement 需要看哪些历史 | StatementMemory projection |
| 整个 Program 是否返回了任务结果 | Program Runtime + task checker |

如果两个模块都在回答同一个问题，应优先删除一个决策点，而不是增加同步状态。

## 6. 公开契约

### 6.1 输入契约

- `OriginalGoal`：用户原始文本，贯穿编排和执行，不被 Router 替换。
- `IntentResolution`：可选、最小的语义差量；为空是合法且应被覆盖的路径。
- `ApplicationFacts`：完成规划所需的应用接口事实。当前实现仍以受控文本上下文承载，长期可以演进为轻量 typed facts，但不应演进为每个应用一套重型 Target Schema。
- `PlatformContract`：平台能力和应用入口，例如可否启动应用、可用应用列表及其稳定别名。

### 6.2 `CodingProgram`

Orchestrator 生成唯一入口 `run(ctx)`。Program 可以使用受限 Python 表达：

- 变量和业务值传递；
- 条件、循环和有限集合处理；
- 查询、读取、排序、聚合和断言；
- 跨应用读取值后再执行写操作。

Program 不能包含设备坐标、直接驱动浏览器或 ADB、导入任意库、通过 helper 隐藏不可检查的数据流，或在 host Python 中解析界面文本以绕开 Statement 能力。

### 6.3 `ctx` API

> 2026-08-05 起 ctx API 改为显式 state 线程化 + acquire 独立，公共签名已变。
> 完整契约见 [编排器显式 state 架构](orchestrator_explicit_state.md)；下表仅保留职责概览。

| API | 语义 | 关键约束 |
|---|---|---|
| `ctx.reach(state, ...)` | 到达一个业务资源、记录或创建入口 | 消费 state、返回新 state；`success` 描述可观察 UI 状态 |
| `ctx.query(state, ...)` | 定位集合并施加过滤 | 返回可复用 `scope`，不返回行 |
| `ctx.acquire(scope, ...)` | 从会话材料化行 | `fields` 是唯一投影与类型声明处；scope 可复用 |
| `ctx.read(state, ...)` | 从当前目标读取详情或结构化值 | 借用 state；读取结果可进入 Program 数据流 |
| `ctx.commit(state, ...)` | 对当前目标或新目标执行持久化变更 | 消费 state、返回 post-commit 观测；现有目标变更需 target 绑定 |
| `ctx.command(state, ...)` | 执行平台能力，例如启动应用 | 消费 state、返回 post-command 状态 |

`ctx` 调用表达的是业务意图。字段如何映射到控件、当前该点哪个按钮，由 Statement 和 Adapter 决定。

### 6.4 Statement 结果契约

Statement 必须返回可验证的 typed outcome，而不是只有一句自然语言自述。结果可以包含：

- 是否到达目标或完成操作；
- 当前绑定的业务对象；
- 查询行或读取值；
- 失败分类和可重试证据；
- 产生持久化变化的操作回执。

Program Runtime 只消费这些契约值，不从截图描述中重新推断业务状态。

## 7. Knowledge 边界

### 7.1 允许进入 Knowledge 的内容

- 应用、页面、资源或字段的真实名称；
- 导航入口及应用固有的层级关系；
- 某字段属于列表还是详情、是否支持筛选或写入；
- 特定站点的状态值、业务枚举和验收语义；
- 应用特有且无法从通用观察可靠获得的限制。

例如，“Messages 的详情资源包含消息正文”是应用事实；“读取候选正文确认是否为邀请”是由目标语义产生的计划，不是知识事实。

### 7.2 禁止进入 Knowledge 的内容

- `ctx.reach/query/read/commit/command` 代码；
- 针对某个 eval task 的步骤或答案；
- “永远取第一条”“遇到 tomorrow 先改日期”等通用策略；
- reach/commit 的内部阶段或完成判定；
- 本可由 Compiler、Adapter 或通用 Prompt 表达的规则；
- 为适配编排而虚构的重型领域对象，例如应用并未暴露的 `Calendar.Event` Schema。

### 7.3 事实来源的权威范围

- Adapter 和结构化观察对“当前设备上现在有什么”负责；
- Knowledge 对“这个应用稳定的接口含义是什么”负责；
- 视觉推理只补充前两者未覆盖的当前界面信息。

这些来源的权威范围不同，不应简单用一份文本覆盖另一份事实。

`AppKnowledge` 只根据 frontmatter scope 和目标相关性选择知识文档，不解释正文里的 Markdown
标题。接口事实按稳定资源域拆成短文档，整份被选文档都必须符合事实边界；没有
`orchestrator` scope 的长导航或 UI 手册不会作为编排兜底。Planner 不得把某个 section 标题
提升为编译协议或高优先级计划指令。

## 8. Program 与 Statement 的边界

Program 回答“做哪些业务操作以及它们之间如何依赖”：

```text
查询午餐相关消息候选
    → 逐条读取正文并确认邀请语义
    → 回复被确认的消息
    → 启动日历
    → 使用已读取正文创建日程
```

Statement 回答“当前这一项业务操作如何在眼前界面完成”：

```text
识别当前页面
    → 定位筛选框或目标记录
    → 点击、输入或滚动
    → 观察结果
    → 验证局部后置条件
```

不应把两者混在一起：

- Program 不应枚举“第几行右侧第二个时间字段”；
- Statement 不应自行决定接下来要回复消息还是创建日程；
- Compiler 不应看到 “lunch” 就注入候选确认流程；
- Memory 不应因为看到日历页面就推断任务进入“设置结束时间阶段”。

## 9. Memory 边界

当前架构需要的是可追溯事实记忆，不是一个隐式语义大脑：

- `EventJournal` 追加原始观察、动作和结果，提供 provenance；
- `StatementMemory` 从 Journal 中选取当前局部决策需要的事实；
- Transition 结合投影作出下一动作；
- Program 的变量保存跨 Statement 的业务值和控制流进度。

因此不再引入独立的 TaskMemory 来推断“任务现在进行到哪一步”或修补 Router/Orchestrator 的语义。如果未来增加跨运行经验，它只能保存带来源的事实、成功操作回执或可验证能力，不得决定当前任务意图。

## 10. 跨应用任务示例

以“从短信找到午餐邀请，回复后按其中信息创建日历事件”为例，边界应当是：

1. 原始任务保留完整目标，Router 可以不输出补充；
2. Messages 和 Calendar Knowledge 只提供真实资源、字段和应用入口；
3. Orchestrator 生成候选查询、逐条语义确认、回复和跨应用创建的数据流；
4. Compiler 检查读取值是否直接流入后续来源驱动的 `commit`，不检查 “lunch” 或日期词；
5. Statement 在每个应用内完成局部 GUI 操作；
6. Program 变量保存被确认的消息正文，不靠隐式 TaskMemory 记住；
7. 日历界面的日期、开始时间和结束时间由当前目标、来源正文及实时 UI 一起决定，不依赖固定 `event_title/start_time/duration` 槽位。

这个例子验证的是通用数据流边界，不应催生 ScheduleLunch 专用代码。

## 11. 明确拒绝的设计

以下方案会增加决策中心或把个案固化进架构，原则上不再采用：

- Router 把原任务翻译或改写成另一条完整任务；
- Router 输出执行模式、步骤或应用字段；
- Knowledge 中包含 `ctx.*` 代码和任务操作清单；
- Planner 把 Markdown 标题当作高优先级计划指令；
- 为每个表单定义一套重型 Target Schema；
- 使用固定 temporal slots 或关键词启发式判断日期完整性；
- 在 reach/commit 内构造面向每个应用的中间状态机；
- 用 TaskMemory 决定业务意图或补偿 Program 错误；
- 用 `entity-only reach`、任务关键词或应用名推断业务完成；
- 把“单个目标”机械翻译成“查询结果必须恰好一条”；
- 未确认候选语义就默认 `rows[0]` 是最终业务对象；
- 在核心 Prompt、Compiler 或 Sandbox 中写 WebArena、MobileWorld 或具体 App 个案。

## 12. 新问题应放在哪一层

新增规则前依次判断：

1. 它是否是用户这次任务的明确语义？保留在原始任务，由 Orchestrator 使用。
2. 它是否只是任务中真正影响结果的歧义？由 Router 做最小补充。
3. 它是否是某个应用稳定、不可通用推导的事实？放入 Knowledge。
4. 它是否是某个平台的交互机制？放入对应 Adapter 或 adapter prompt。
5. 它是否是所有 Program 都应满足的可证明结构或数据流不变量？放入 Compiler/Sandbox。
6. 它是否属于跨操作的业务顺序、筛选或计算？由 Orchestrator Program 表达。
7. 它是否只与当前界面的下一动作和局部完成有关？由 Transition 处理。
8. 它是否只是已经发生的事实？写入 EventJournal，由 StatementMemory 投影。

如果一个规则同时命中多层，先写最小失败用例并明确唯一决策所有者，不通过新增同步字段连接多个判断点。

## 13. 验证要求

涉及边界变化时，至少覆盖受影响层的最小验证：

- Router：原始任务不变，补充可为空且不包含步骤；
- Knowledge：不含 `ctx.*`、benchmark 个案或通用策略；
- Orchestrator：正常输入和空 supplement 均能生成合法 Program；
- Compiler/Sandbox：新增诊断只依赖通用 AST、契约或数据流；
- Replay：确认 Statement 行为没有重复操作或错误终止；
- 跨平台回归：Android 变更至少检查 Browser 编排不回归，反之亦然；
- Live：只在静态编排与回放验证通过后执行，turn 上限由测试配置统一控制。

运行日志、截图和临时调查脚本用于证据，不作为生产源码提交，除非有意提炼为稳定 fixture。

### 13.1 新 case 变绿优先级（禁止 compile 语义膨胀）

静态编排 eval（`evals/*/orchestrator`）红灯时，只允许按下列顺序处理。**禁止**为压红在
planner/sandbox 增加 goal 文本匹配、Knowledge Markdown 模板解析或 case 专属
`CodeDiagnostic`。

1. **Knowledge 事实**：补 `knowledge/<platform>/<app>/` 中的接口名、字段、member 归属与入口；
   禁止写入步骤、`ctx.*` 或 benchmark 答案。
2. **Router 补充**：仅当歧义跨任务成立时给最小 `semantic_supplement`；不得改写原任务。
3. **通用 prompt**：改 `coding.md` 或平台 orchestrator prompt 时，规则须对 ≥2 个无关 case
   成立；单 case 私货不准进 prompt。
4. **Eval contract**：用 `ordered_calls` / `features` / `literal_values` / `slice_stops` 等
   描述可观察程序形态并验收；LLM 偶发 FAIL 是能力信号，不是 compile 漏洞。

分层保证：

| 层 | 保证 | 不保证 |
|---|---|---|
| Sandbox 结构 | 非法程序进不了 runtime | 业务做对 |
| Knowledge | LLM 看得到真实接口事实 | 选对编排策略 |
| Prompt（跨任务） | 提高首生成命中率 | 单 case 100% |
| Eval contract | 错形态必红、可回归 | 自动修程序 |
| Compile 语义门 | （已删除，禁止回流） | — |

加新 orchestrator case 的 PR 自检：

- [ ] contract 只描述可观察程序形态，不写自然语言关键词门；
- [ ] 生成常错时先查 knowledge → 通用 prompt → 再收紧 contract；
- [ ] diff 不含 planner/sandbox 对 goal/knowledge 的文本匹配，或依赖任务词的新诊断码；
- [ ] 单 case 策略不进 `coding.md`（需第二例佐证才算跨任务）。

## 14. 当前兼容债务

这份文档描述目标边界，但当前代码仍有少量兼容实现需要逐步收敛：

- ~~Orchestrator 的部分语义诊断仍解析自由文本 supplement 或有限 Knowledge 文本~~
  **已删除** `_semantic_contract_diagnostics`（member-shape / 粗体字段 / semantic-candidate /
  用户值大小写）。Compile 层只保留结构不变量；任务形态由 eval contract 验收，生成策略由
  prompt 与 Knowledge 事实承担。见 §13.1。
- Application Facts 当前主要以文本传递，缺少统一但轻量的来源和能力标注；
- 部分应用能力仍依靠静态 Knowledge，运行时发现尚不能完全覆盖。

治理这些债务时不应引入新的重型 Schema 或状态机。优先顺序是：删除重复判断、缩短链路、明确来源，再考虑增加结构。
eval 红灯按 §13.1 迭代，不得借机恢复 compile 语义门。

## 15. 架构改动的验收标准

一次架构改动只有同时满足以下条件才算改善：

- 决策所有者更少或更明确；
- 正确性不依赖单个 eval case 的关键词；
- Knowledge 仍然只是必要事实；
- 空 Router supplement 和未知应用路径能够合理工作或清晰失败；
- Browser、Android、iPhone 共用的核心规则保持平台无关；
- 新增代码量与新增不变量相称，并删除被替代的旧机制；
- 回放、最小编排 eval 和相关 live 验证形成一致证据。

判断一个设计是否值得合入时，优先问：“它删除了哪个不确定决策点？”如果答案只是“又增加了一层兜底”，通常说明边界还没有找对。
