# 语义执行架构：Program、Interact 与 Data

> 状态：目标架构决策（尚未全部落地）  
> 适用范围：DSL-only Runtime；DAG、函数化子编排和渐进式子编排不属于该目标  
> 现状实现细节参见 [dsl_runtime_architecture.md](dsl_runtime_architecture.md)

## 决策摘要

GUI 任务并不只包含 GUI 操作。一个任务通常同时包含：

- 在当前界面中寻找路径并产生副作用；
- 从截图、页面结构、上游返回值或表格中读取数据；
- 对数据做筛选、聚合、计算和校验；
- 根据结果选择后续业务步骤；
- 调用 URL、返回、启动应用等确定性平台能力。

DSL 不应在编排阶段猜测页面路径、控件、SQL、列名或计算表达式。Program 只声明语义目标、
数据依赖、验收合同和显式控制流；运行时执行器面对真实界面或真实数据决定具体执行方式。

目标 Program 只保留六类节点：

```text
Program
  ├── Interact   在 main 界面上完成一个线性 UI 语义目标
  ├── Data       基于当前数据完成一个线性数据语义目标
  ├── Command    执行参数明确的确定性平台能力
  ├── If         Program 中唯一的显式分支
  ├── ForEach    对已物化的 typed collection 逐项执行固定 body
  └── Finish     结束 Program
```

所有可执行节点通过同一个 `StatementOutcome` 回到 `ProgramRuntime`；所有事实只追加到
`EventJournal`。不能为 Data、Command 或 UI 再建立平行终态协议和事实账本。

## 第一性原理

### Program 描述“为什么”和“要什么”

Program 负责：

- statement 的业务顺序；
- 不可变的目标实体、范围、值和副作用定义；
- statement 之间的数据依赖；
- typed returns 和最终结果合同；
- 基于 typed output 的显式 `If`；
- 基于 typed collection 的显式 `ForEach`；
- 确定性平台命令；
- statement 终态后的推进、恢复或 Program 级重编排。

Program 不负责：

- 中间经过哪些页面、菜单、弹窗、局部 tab 或移动端 screen；
- click、type、scroll、select、save 的动作序列；
- 当前页面上具体控件和业务语义的绑定；
- SQL、Python 表达式、列名映射和聚合实现；
- 数据操作的 filter、join、group、project 顺序；
- statement 内部的候选路径树。

### Executor 描述“面对现实如何完成”

Executor 在运行时才拥有真实 observation、真实 schema、真实动作回执和 Journal memory。
因此执行路径应由 Executor 决定，而不是由 Compiler 在信息不完整时提前写死。

这不是让 LLM 直接发明执行结果：LLM 负责语义决策，GUI adapter 或受限数据内核负责实际执行，
Runtime 负责合同和硬边界。

## 单一执行界面：`main`

当前目标暂不引入多 `SurfaceRef`。Program 只有一个执行界面：

```text
surface = main
```

- 浏览器把当前前台页面视作 `main`；
- iPhone 和 Android 把当前前台 screen 视作 `main`；
- 每个 `Interact` 和需要界面的 `Command` 都显式作用于 `main`；
- statement 结束后的最终 observation 成为 `main` 的新位置；
- 下一条 statement 从该位置继续。

位置变化的语义是：

```text
main@位置A → Statement 1 → main@位置B
main@位置B → Statement 2 → main@位置C
```

“位置”是 observation 事实，可以包含 URL、title、app、screen/page identity；它不是新的可写
业务状态机。ProgramRuntime 只拥有当前 `main`，Journal 记录 statement 的起止 observation。

浏览器页面自然打开新 tab 时，当前版本可以把新前台 tab 视为新的 `main`，但不能再寻址旧 tab。
显式多 tab 切换、关闭和 checkpoint 后重绑定不属于本阶段目标。

## 线性 Statement

一个 Statement 可以执行任意长度的多步骤路径，也可以跨多个页面：

```text
观察页面 A
  → 打开菜单
  → 进入列表页 B
  → 搜索目标
  → 打开详情页 C
  → 修改并保存
  → 到达结果页 D
  → StatementOutcome
```

页面跨度是 Statement 的内部执行细节，不是 Program 控制流。

“线性”指一次实际执行只保留一条轨迹：每帧根据当前 observation 和本 statement memory 选择唯一
下一步。Statement 合同和 Runtime 不保存 `if/else` 图、候选路径树、子 Statement、函数调用或
`ForEach` body。集合循环属于 Program，不属于 Executor 私有流程。

运行时根据新观察重新选择下一动作是 React 决策，不是持有分支图。例如“菜单已经展开，所以点击
下一入口”和“菜单未展开，所以先展开”都只是当前帧的下一动作。若执行将改变业务目标、目标实体、
范围或后续事务，则必须返回 Program，由 `If` 或 Program 级恢复决定。

结构边界如下：

| 能力 | Statement 内 | Program 中 |
|---|---:|---:|
| 跨页面线性执行 | 是 | 不展开页面细节 |
| 根据当前观察选择唯一下一动作 | 是 | 否 |
| 重试被拒绝或无效果的当前动作路线 | 是 | 否 |
| 修改 statement 语义目标或写入值 | 否 | 是 |
| 显式 `if/else` 业务分支 | 否 | 是 |
| 子 Statement 或函数控制流 | 否 | 函数暂不保留 |
| 对已物化集合逐项执行 | 否 | `ForEach` |

## `Interact`：UI 语义执行器

`Interact` 表示在 `main` 上实现一个 UI 后置条件，而不是一个原子动作或一个页面。

示意合同：

```python
Interact(
    goal="确保目标记录包含声明值并持久化",
    on="main",
    success_condition="目标记录已保存且声明值可验证",
    required_values={"declared_value": ["A", "B"]},
    returns={"record_id": "text"},
)
```

Compiler 声明业务不变量，但不声明控件 selector、页面路径或动作步骤。

执行循环：

```text
CurrentObservation + StatementMemory + Contract
  → LLM Transition
       → Act(one semantic action) | Complete | Infeasible
  → mechanical Guard
  → Action Policy / adapter dispatch
  → append Journal
  → next observation
```

`Interact` 可以：

- 导航页面内菜单、链接、向导和局部 tab；
- 搜索、填写、选择、保存；
- 滚动、分页和打开多个详情页；
- 在同一语义目标下根据当前观察调整下一动作；
- 从最终或中间 observation 返回声明的 reads/rows。

`Interact` 不能：

- 改写 Program 或选择另一项业务任务；
- 更改合同中的目标实体、范围和值；
- 直接执行 Program `If`；
- 把 LLM 判断当成未执行的动作或数据事实；
- 管理 SQL、聚合或跨 statement 数据流。

## `Data`：数据语义执行器

`Data` 表示对运行时当前可用数据实现一个数据后置条件。它不是单个 SQL，也不限于单个 filter 或
compute。一次 Data Statement 的实际线性轨迹可以包含：

```text
观察 DataContext
  → 选择允许的数据源
  → 解析实际 schema
  → 提取 / 规范化
  → 筛选 / 连接 / 聚合 / 派生
  → 校验 typed returns
  → StatementOutcome
```

示意合同：

```python
Data(
    goal="从当前候选记录中选出唯一满足类型约束的记录，并返回数量和入口",
    inputs=["candidates"],
    returns={
        "match_count": "number",
        "detail_url": "url",
    },
)
```

Program 声明可以消费的上游数据和输出合同，不声明 SQL、实际列名或算子顺序。

### DataContextView

Data Executor 的只读感知面可以包含：

- Program env 中允许访问的 typed variables；
- 上游 Statement 返回的 reads 和 rows；
- 当前 `main` observation 中的截图、tables 和结构化状态；
- 数据集的真实 schema、行数和有界样本；
- 本次 Data Statement 已执行操作的 Journal 事实和错误。

DataContextView 是 Program env、Observation 和 Journal 的投影，不是新的数据账本。

### 语义决策与确定性执行

Data LLM 可以决定“从哪个真实字段筛选”“需要计数还是去重”“如何从实际 schema 投影返回值”，
但不能直接编造最终数字或记录。每一步由受限数据内核执行，例如：

- select/project；
- filter/sort/distinct；
- join/group/aggregate；
- 标量 derive 和格式规范化；
- typed output validation。

底层可以使用受限 SQL 或等价数据代数，但它是根据运行时真实 schema 生成的临时执行计划，记录在
Journal/Outcome 证据中，不进入 DSL Program。

Data Executor 不点击、滚动、切页或修改 GUI。若完成目标需要获得更多页面数据，应由 `Interact`
先完成采集并返回；Data 只消费已经可观察或已经返回的数据。

## `Command`：确定性平台能力

参数完整且无需解释当前页面才能执行的能力使用 typed `Command`：

```python
OpenURL(on="main", url="{record.detail_url}")
Back(on="main")
LaunchApp(on="main", app="declared_app")
```

Command 不进入 LLM Transition，也不经过 Action Policy。adapter 执行命令、刷新 observation，并
返回统一 `StatementOutcome`。

宽泛的“navigation”不是独立执行类型：

| 场景 | 归属 |
|---|---|
| 已知 URL、history back、启动已知应用 | Command |
| “进入目标列表”“找到目标详情”等未知页面路径 | Interact |
| 点击页面链接后的自然跳转 | Interact 内部动作 |
| 多浏览器 tab 管理 | 当前阶段非目标 |

## Read、Query、Compute 和 Filter 的归并

旧分类混合了业务动词与执行方式。同一个“read”可能完全不交互，也可能需要滚动和打开详情；同一个
“filter”可能是填写网页筛选框，也可能是对内存 rows 做过滤。目标模型按执行边界归类：

| 当前能力 | 目标归属 |
|---|---|
| 从当前截图、DOM、table、env、Journal 读取 | Data |
| SQL Query | Data |
| Compute 表达式 | Data |
| 对已有 rows 筛选、聚合、去重、排序 | Data |
| 需要滚动、分页或打开详情才能读取 | Interact |
| 在网页筛选框输入并提交 | Interact |
| Interact 完成帧读取字段 | Interact returns |
| 已知结构化 URL/数据源读取 | Data 或 typed Command，按是否产生 UI 位置变化划分 |

`Observe`、`ReadScreenshot`、`ReadDOM` 不进入 Program。Executor 开始时由 Runtime 提供新鲜
observation；感知是执行器机制，不是业务步骤。

## `If` 与显式分支所有权

Program 的 `If` 是唯一显式分支：

```python
If(
    condition=Equals("record.match_count", 1),
    then=[...],
    otherwise=[...],
)
```

条件语言保持简单、类型化和确定性，例如相等、大小、存在、空值和集合包含。复杂数据判断先通过
`Data` 产出 boolean/scalar，再由 `If` 消费。

Executor 可以根据实际 observation 选择当前唯一下一步，但不能输出或保存一棵分支计划。需要改变
业务目标或后续事务的判断必须显式回到 Program。

## `ForEach` 与集合所有权

`ForEach` 是完整 Program 架构不可缺少的集合控制流。没有它，“对集合中每个成员都执行”只能被
塞进一个不透明的大 Interact，Program 将失去：

- 集合基数和处理范围；
- 当前迭代项与数据绑定；
- 每项独立的 StatementOutcome、恢复预算和失败位置；
- 逐项结果的 materialization；
- checkpoint、replay 和报告中的可审计性。

目标 `ForEach` 只迭代运行时已经物化、类型明确的 collection：

```python
ForEach(
    items=Ref("targets.items"),
    item="variant",
    body=[
        Interact(
            goal="确保当前 {variant} 满足声明的业务状态",
            on="main",
            inputs=["variant"],
            bind="result",
        ),
    ],
    collect="result",
    into="variant_results",
)
```

它的职责只有：

1. 从 Program env 读取 typed collection；
2. 按确定顺序绑定 `item` 和可选 index；
3. 在隔离的 iteration frame 中对每项执行编译时已经固定的 body；
4. 将 `collect` 指定的一个 body-local typed value 收集进可选 `into`；
5. 把失败、恢复和 Outcome 归属到具体迭代项。

`ForEach` 不负责发现 UI 集合，也不在运行时生成 body。集合来源应先由 `Interact` 采集，或由
`Data` 从已有数据中构造：

```text
Interact/Data → typed collection → ForEach(fixed body) → materialized results
```

如果应用提供真正的批量能力，一个 Interact 可以直接实现集合后置条件；如果每个成员需要独立事务、
独立返回值或独立恢复，则必须由 Program `ForEach` 表达。两者的选择基于业务事务边界，不基于页面
长相。

应删除的是现有动态子编排字段和机制：`body_goal`、`member_desc`、运行时 sub-decompose、按样本重新
生成 body，以及为这些机制存在的 selector/expansion 状态。保留的是普通解释器循环：typed source、
固定 body、item binding 和可选 result materialization。

### 当前 ForEach 职责的拆分

当前实现把五类职责都压在 `ForEach` 上。目标架构按执行边界拆开：

| 当前 ForEach 职责 | 目标 owner |
|---|---|
| `target/row_fields/limit` 驱动页面集合采集 | Interact |
| `member_desc/select_fn` 判断哪些行属于目标集合 | Data |
| `body_goal/expand_fn/subdecompose_fn` 动态生成循环体 | 删除；Compiler 预先生成固定 body |
| 对 rows 逐项绑定并执行 body | ForEach / Interpreter |
| `returns/output_fields` 猜字段并自动拼表 | body 的 typed result + `collect/into` |

因此目标 schema 可以收成：

```python
class ForEach:
    items: Ref                    # 必须指向 completed typed collection
    item: str                     # iteration frame 中的 typed item 变量
    index: str | None = None      # 可选 iteration index 变量
    body: list[Statement]         # 编译时固定，可包含 Program If/ForEach
    collect: str | None = None    # 唯一一个要导出的 body-local typed value
    into: str | None = None       # collect 后形成的 typed collection
```

`collect` 不做字段合并和表达式求值。body 若要输出多个字段，应在末尾使用 Data 生成一个 typed record，
再由 `collect` 收集该 record。这样 ForEach 不需要理解每个 Executor 的 reads、rows 或内部字段。

### Interpreter 语义

ForEach 不是第三种 Executor，也不调用 LLM。它只是 Program Interpreter 的确定性控制节点：

```text
resolve items
  → verify completed typed collection
  → for index, item in stable_order(items)
       → push lexical iteration frame(item, index, loop_path)
       → execute fixed body through normal ProgramRuntime dispatch
       → collect declared body-local value
       → pop iteration frame
  → bind collected values to into
```

关键不变量：

- `items` 缺失、类型错误或来自未完成 Outcome 时失败，不能静默当成空集合；
- 合法空集合完成零次迭代，并产生已确认的空结果；
- 输入顺序就是执行顺序；排序、去重和成员选择必须先由 Data 完成；
- item/index/body-local variables 位于 iteration frame，结束后不得泄漏到父 env；
- 不再把 item 伪装成一个 `StatementOutcome` 写入全局 env；
- 不自动合并原 row、body reads 和 scalar；只收集显式 `collect`；
- 每条 body Statement 仍由 Interact、Data 或 Command 的正常执行器执行。

### 失败、恢复与 replay

第一版只提供 fail-fast 语义，不增加 `continue_on_error`、skip 或隐式补偿策略：

- 当前 body Statement 的正常重试仍由 ProgramRuntime/RecoveryRouter 处理；
- 重试耗尽或不可行时，ForEach 在具体 item 上失败，后续 item 不再执行；
- 聚合 Outcome 必须报告失败 index、item identity 和已完成数量；
- 不能把部分完成集合投影成完整成功结果。

每个 body Statement 的 instance identity 带稳定 loop path，例如
`foreach:s3[2]/statement:s5`。Journal 持久化物化后的输入 collection 和带 loop path 的子 Outcome；
resume 通过重放已完成 iteration，继续第一个未完成 index，不维护第二份 loop checkpoint 状态。

## 统一 Outcome、Journal 与恢复

`Interact`、`Data` 和 `Command` 使用同一个终态协议：

```text
StatementOutcome
  phase: completed | failed | exhausted | infeasible | interrupted
  verification: confirmed | accepted_unverified  # 仅 completed
  reads
  rows
  evidence
  summary
  kickback                                      # 仅 infeasible
```

- `Interact` 的证据来自 observation、动作回执和业务效果；
- `Data` 的证据来自输入数据版本、实际执行算子和输出合同校验；
- `Command` 的证据来自 adapter receipt 和命令后的 observation；
- ProgramRuntime 只消费 Outcome，不读取 executor 私有状态；
- EventJournal 是唯一事实流；Memory/DataContext/报告都是只读投影。

Guard 只保留机械否决权：预算、schema、声明数据范围、写入授权、typed returns 和终态证据。Guard
不能替 UI/Data LLM 选择业务路线，也不能用正则或页面特例补一条隐藏执行路径。

## 目标 Schema（示意）

以下只表达边界，不是最终 Python API：

```python
class Interact:
    goal: str
    on: Literal["main"]
    success_condition: str
    required_values: dict[str, JSONValue]
    inputs: list[str]
    returns: dict[str, DataType]
    persistence: PersistenceMode


class Data:
    goal: str
    inputs: list[str]
    returns: dict[str, DataType]


class Command:
    capability: CommandCapability
    on: Literal["main"] | None
    args: dict[str, JSONValue]
    returns: dict[str, DataType]


class If:
    condition: TypedCondition
    then: list[Statement]
    otherwise: list[Statement]


class ForEach:
    items: Ref                  # Program env 中的 typed collection ref
    item: str
    index: str | None
    body: list[Statement]       # 编译时固定；运行时不得重编 body
    collect: str | None         # body-local typed value
    into: str | None


class Finish:
    message: str


class Program:
    goal: str
    surface: Literal["main"] = "main"
    statements: list[Interact | Data | Command | If | ForEach | Finish]
```

Executor 没有私有 branch/body/function 字段；`ForEach.body` 只属于 Program AST。DSL 也没有
SQL、compute expression、selector 或动作列表。

## 示例

```python
candidates = Interact(
    goal="找到符合用户目标的候选记录，并读取候选数据",
    on="main",
    returns={"candidates": "rows"},
)

record = Data(
    goal="从候选中选择唯一满足业务类型约束的记录",
    inputs=["candidates"],
    returns={
        "match_count": "number",
        "detail_url": "url",
    },
)

If(
    condition=Equals("record.match_count", 1),
    then=[
        OpenURL(on="main", url="{record.detail_url}"),
        Interact(
            goal="确保目标记录包含用户声明的值并保存",
            on="main",
            success_condition="声明值已持久化且可验证",
        ),
    ],
    otherwise=[
        Finish("无法确定唯一目标记录，未执行写入"),
    ],
)

Finish("任务完成")
```

Compiler 只表达业务目标和依赖。第一个 Interact 可以跨多个列表页或详情页；Data 在真实候选 schema
上决定如何选择；OpenURL 是确定性命令；最终写入 Interact 决定当前界面中的具体动作。

## 明确非目标

当前阶段不做：

1. 多 `SurfaceRef`、浏览器 tab 变量和跨进程 tab 重绑定；
2. Statement 内显式分支图、子 Statement 或 Program 修改；
3. `FunctionDef` / `Call`、动态 `ForEach` body 和渐进式子编排；
4. DSL 级 SQL、Python expression、DOM selector 和动作序列；
5. 让 Data LLM 直接生成未经确定性执行验证的结果；
6. 为 UI、Data、Command 分别建立 Outcome、Journal 或可写完成状态；
7. 从 Journal 重放 GUI/Data executor 的全部临时思考过程。

集合 UI 任务可以由一个具有真实批量语义的线性 Interact 完成；需要逐成员独立执行时，由 Interact
或 Data 先产出 typed collection，再由 Program `ForEach` 驱动固定 body。已有结构数据上的集合
运算由 Data 完成。只有确实改变后续业务事务的条件才进入 Program `If`。

## 迁移方向

迁移应以删除旧复杂度为约束，禁止长期维护两套可写执行语义。

### 1. 固化最小协议

- 为 `main`、Interact、Data、Command、If、Finish 建立目标 schema；
- 两类 Executor 和 Command 都只返回 `StatementOutcome`；
- ownership 测试禁止 executor 修改 Program 或追加平行账本。

### 2. 先建立 Data Executor

- 从当前 env、Observation 和 Journal 投影 `DataContextView`；
- 使用受限数据算子执行 LLM 的运行时语义计划；
- 由 typed returns 校验完成；
- 用 Data 替代 Read/Query/Compute 的生产入口。

### 3. 收缩 UI Program 合同

- 将 navigation/filter/action/collection/verification 归并为 Interact；
- 保留目标、成功条件、声明值、returns 和 persistence；
- 删除页面路径、控件步骤和内部业务相位语义。

### 4. 提升确定性 Command

- 将已知 URL、back、launch 等从文本识别/Action Policy 提升为 typed Command；
- 当前只允许操作 `main`；
- 页面内未知导航继续由 Interact 处理。

### 5. 删除旧语言和验证器

- 删除 `RunKind` 的 navigation/filter/action/read/data_query 分类；
- 删除 DSL SQL、Compute expression 及其编译期修复；
- 保留最小 `ForEach(items/item/body/into)`，删除 `body_goal`、`member_desc`、运行时 body 生成和
  相关 selector/expansion 子编排；
- 删除 FunctionDef/Call；
- 删除只为上述形状存在的 validator、prompt 和恢复分支。

## 验收标准

目标架构完成时应满足：

- Compiler 输出中没有 SQL、Python expression、selector、动作序列或页面路径图；
- 一个 Interact 可以跨多个页面，但结构上不能包含 `If`、body、function 或子 Statement；
- 一个 Data 可以根据运行时真实 schema 完成多步数据处理，但不能产生 GUI 副作用；
- Program `If` 是唯一显式分支；
- Program `ForEach` 是唯一显式集合迭代，且 body 在执行前已经固定；
- 所有 UI/Data/Command 执行都返回 `StatementOutcome` 并追加同一个 EventJournal；
- ProgramRuntime 是 env、statement 顺序和恢复的唯一所有者；
- 浏览器、iPhone 和 Android 共用 singleton `main` 和相同语义合同；
- 删除旧节点后，生产代码和测试不再直接构造旧 `RunKind`、Query SQL 或 Compute expression。
