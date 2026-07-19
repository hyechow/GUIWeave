# 运行时数据采集与处理设计

> 状态：设计方案。本文补充当前语义执行架构中尚未完整落地的数据采集能力，重点处理
> 数值/集合运算与 GUI 交互的分离，以及滚动、分页等跨帧采集。它不增加 Program 节点，
> 也不恢复旧 Read/DAG walker 或确定性 Statement 相位机。
>
> 当前工作区已落地独立 `CollectionSliceEvent`、基础 provenance 隔离和只读 CollectionView；
> P0 的终态可回放与跨集合混账已经修复。`OutputSpec.coverage`、AcquisitionSpec、统一
> ObservationMaterializer 和跨平台采集仍未收口，实现不得反向成为设计权威。

## 问题

GUI 任务中的“数据工作”其实包含两个性质不同的阶段：

1. **采集**：从真实界面取得数据，可能需要滚动、分页、打开页面或切换视图；
2. **处理**：对已经取得的数据做筛选、去重、排序、关联、聚合、计算和组合。

如果两者都塞进 Interact，GUI executor 会同时负责找控件、遍历集合和计算业务结果；如果两者
都塞进 Data，Data executor 又必须暗中操作 UI。两种做法都会模糊权限边界。

当前实现还有一个具体断点：旧框架的跨页读取实现仍然存在，但六节点运行时没有完整接入它。
Interact 可以执行滚动和翻页动作，却主要从终态帧提取 outputs；前面页面观察到的记录不会自然
汇总成类型化结果。Data 也只能处理已绑定输入和当前 observation，不能补回已经离开的页面。

因此，当前版本支持“跨页面操作”，但尚未完整支持“跨页面采集并归约为可靠 outputs”。这是
运行时能力迁移不完整，不是语义 Program 应当放弃跨页能力。

## 设计结论

采用下面的单向数据流：

```text
Program 声明语义目标与类型化数据依赖
        │
        ▼
Interact 在真实 UI 中采集
  - Transition 判断当前状态及下一步在哪里做什么
  - Action policy 将语义动作落到当前平台
  - 滚动/翻页只是自适应执行动作
        │
        ▼
ObservationMaterializer 规范化当前帧数据与来源
        │
        ▼
EventJournal 追加 CollectionSliceEvent、动作与回执
        │
        ▼ pure reducer
CollectionView 汇总记录与覆盖证据
        │
        ▼
StatementOutcome.outputs 物化类型化数据
        │
        ▼
Data 对已物化数据做确定性处理
        │
        ▼
If / ForEach 消费处理结果
```

核心边界是：

- **Interact 负责需要改变 UI 才能取得的数据**；
- **Data 负责不改变 UI 的读取与数据派生**；
- **Transition 是 Statement 内唯一的流转决策者**；
- **Journal 是跨帧事实的唯一持久来源**；
- **CollectionView 是纯投影，不是新的状态机或状态所有者**；
- **业务去重和集合运算属于 Data，采集层只消除同一观察片段的确定性重复**。

## 职责边界

| 场景 | 归属 | 原因 |
| --- | --- | --- |
| 读取当前帧已经可见的表格、表单、URL 或标题 | `Data` | 不需要改变 UI |
| 为定位目标而滚动或翻页 | `Interact` | 属于动作策略，Statement contract 无需描述过程 |
| 遍历同一个集合视图并采集所有可达记录 | `Interact` | 需要连续观察和 UI 动作 |
| 对记录筛选、去重、排序、聚合、计算或组合 | `Data` | 应由确定性数据 kernel 执行 |
| 对每条已物化记录执行相同业务操作 | `ForEach` | 属于显式 Program 控制流 |
| 打开已知 URL、返回或启动应用 | `Command` | 属于确定性平台能力 |
| 从当前界面寻找未知入口并进入 | `Interact` | 路线依赖实时观察 |

“跨页”本身不等于 Program 分支。一个 Interact 可以经过多个页面、对话框或移动端 screen，
只要它仍在追求同一个线性后置条件。用户业务中的 `if/else` 必须进入 Program `If`；对已知记录
逐项执行详情页工作必须进入 `ForEach`，不能藏在采集循环里。

### 同质集合遍历与逐记录工作

需要区分两种看起来都像“循环”的操作：

```text
同一个列表上滚动/下一页，直到采集完整
  -> Interact 内的自适应采集行为

对每条记录打开详情、修改、保存、返回
  -> Program ForEach 的固定 body
```

前者是 UI 传输机制，页面数通常只有运行时才知道；后者是业务重复结构，应由 Interpreter 显式
持有循环帧。这样既不会把浏览器分页写进 DSL，也不会让 Statement 隐藏业务控制流。

## 数值与集合处理

Compiler 只声明数据目标、输入和 typed returns，不在编排阶段固定 SQL、Python 表达式或真实字段。
Data executor 面对运行时数据选择小型执行计划，LLM 负责理解语义，确定性 kernel 负责计算。

期望支持的处理能力包括：

- 字段选择和类型归一化；
- 筛选、去重、排序和限制数量；
- 分组、计数、求和和其他受限聚合；
- 基于真实字段的关联；
- 标量算术、比较和条件派生；
- 集合差、成员判断和组合生成；
- 按 Statement declared returns 发出最终结果。

数据计划是一次执行内的临时方案，不进入 ProgramRuntimeState，也不作为可恢复的第二套程序。
执行失败可以携带确定性错误做一次局部修复；仍失败则返回 StatementOutcome，而不是把计算退回
GUI Transition 临场完成。

“采集后再处理”不等于无条件扫描整个 UI。Interact 可以根据 Statement 的语义范围操作搜索、
筛选和定位，以减少需要遍历的集合；但具体筛选控件和字段由运行时观察决定。Compiler 可以把
语义谓词下推给 Interact，不能把 SQL、数值计算或真实页面字段上浮给它。

典型链路应当是：

```text
Interact: 采集所有商品行 -> outputs.rows
Data: 从 rows 中筛选目标、去重并计算待处理集合 -> outputs.records
If: records 是否为空
ForEach: 对 records 中每条记录执行固定 Interact body
Finish: 汇总结果
```

这正是 Compiler 的主要价值之一：把数据计算和显式控制流从 GUI 执行期剥离，让 Transition
专注于理解当前 UI 状态和选择下一步操作。

## 滚动与跨页采集

### Statement 对什么有感

Statement 不应知道页码、滚动次数、分页按钮名称或平台手势，但必须声明语义输出以及必要的
覆盖要求。下面只是合同形态示意，不代表已经确定最终 schema：

```python
Interact(
    bind="observed",
    goal="Collect all records matching the requested scope",
    success="All reachable records in the collection have been observed",
    returns={"rows": OutputSpec(type="list[record]")},
    acquisition=AcquisitionSpec(
        output="rows",
        coverage="complete",
        predicate_ref=ValueRef(var="requested_filter"),
        fields=["record_id", "name"],
    ),
)
```

覆盖要求不宜直接加入通用 `OutputSpec`：Data 和 Command 的输出并不天然具有 UI 采集语义。
如果需要类型化表达，应作为 Interact 的可选 acquisition contract，并显式指向一个
`list[record]` output。`Interact.scope` 继续承载已有的业务对象/范围描述；AcquisitionSpec 不再
增加第二段 free-text scope。

AcquisitionSpec 只允许结构字段：

```text
output          指向本 Interact returns 中一个 list[record]
coverage        complete | best_effort
predicate_ref   可选；指向已绑定的语义过滤条件
fields          可选的弱列合同，只描述期望语义字段
```

语义范围来自 `goal + scope + inputs`，必要时通过 `predicate_ref` 引用已物化值。Program 中禁止
出现真实页面列名、CSS、XPath、分页参数或 SQL。

覆盖语义只区分：

- `complete`：必须有可信的集合起点、终点与一致性证据；
- `best_effort`：允许受预算限制的结果，但 verification 必须反映未完全确认。

只读取当前可见快照不属于 acquisition：应由后续 Data `read_observation` 完成。

覆盖是结果合同，不是执行相位。它不能生成 `page_1 -> page_2 -> done` 这样的确定性流转表。

`complete` 必须回答“相对于哪个集合完整”。覆盖证据至少绑定：

- Statement 声明的语义 scope；
- 本次采集所见的 collection provenance；
- 遍历起点和终点证据；
- 采集期间筛选/集合身份是否保持一致。

collection provenance 是观察事实，例如页面/表格/列表的结构签名、当前筛选快照和来源帧，
不是新的 Program `SurfaceRef`，也不负责决定下一步动作。同一 Statement 跳到无关列表后，新的
记录不能因为仍在 `main` surface 上就混入原集合。

### Compiler 与 validator 不变量

- Interact 声明 `list[record]` return 时，必须有且只有一个 AcquisitionSpec 指向它；
- AcquisitionSpec.output 必须存在且类型为 `list[record]`；
- Interact 只负责进入列表页时不返回 rows，下一条 Data 读取当前帧；
- Data 不得生成滚动、翻页或其它 UI 动作；
- validator 只检查上述结构，不根据 goal 中的 “all/every/全部”等词做正则或语言启发式判断；
- Compiler prompt/eval 负责把完整集合语义编译成 AcquisitionSpec，不能把真实页面字段写入 Program。

### Journal 中的事实

采集事实已经从动作 turn 中拆出，作为独立 Journal event。loop 在调用 Transition 前先追加当前
observation 的 slice；因此正常终态无需制造无动作 turn，也无需在 outputs 阶段临时合入 live frame：

```text
CollectionSliceEvent
  event_type = collection_slice
  after_turn
  statement_instance_id
  frame_ref
  provenance              完整 CollectionProvenance
  window_key              adapter 提供的页/窗口身份，可空
  content_key             当前 slice 内容指纹
  records                 规范化的当前片段，不做业务去重
  traversal_evidence      起点/终点/可继续移动等观察信号
  source                  visual | structural | mixed
  extractor               可选的提取器及版本
  observed_at
```

它不含 `phase`、`next_action`、`is_complete`。终态顺序必须是：

```text
观察终态帧
-> append CollectionSliceEvent
-> 从 Journal 物化 CollectionView / outputs
-> append StatementOutcomeEvent
```

每个采集帧只追加已经发生的事实：

- 当前观察到的规范化记录及其来源帧；
- 当前截图和可选的 DOM/accessibility/table/form 信息；
- 滚动、分页、打开和返回动作及其回执；
- 观察到的数量、位置和集合边界信号；
- 动作失败、页面未变化或重复观察等事实。

截图是浏览器和移动端共享的感知基线。DOM、accessibility、URL、分页元数据和滚动位置都是
可选正向证据；缺少这些结构化信号不能被解释成视觉目标或下一页不存在。

### ObservationMaterializer 合同

`ObservationMaterializer` 是无状态的共享转换服务：Interact 终态提取、Data 当前帧读取和跨帧
采集都复用同一套规范化逻辑。否则三条读取路径可能对同一个字段产生不同结果。唯一输出形状为：

```text
NormalizedObservation
  frame_ref
  collections: list[NormalizedCollectionSlice]
  forms
  page
  url
  title

ObservedValue
  value
  source: structural | visual
  evidence_ref
```

Materializer 输出所有候选 collection slices，不使用“最大表格/最多行”启发式每帧重选目标。
结构值与视觉/OCR 值保留各自 provenance；冲突时两条证据都保留，不做静默覆盖。纯视觉采集不能
升级为 confirmed structural identity。`CollectionSliceEvent` 只是 NormalizedObservation 中被
本次 acquisition 绑定的 collection slice 的 Journal 投影。

### CollectionProvenance

```text
CollectionProvenance
  surface_fingerprint     adapter 结构签名，可空
  filter_snapshot         规范化筛选/搜索/facet 状态，可空
  schema_fingerprint      规范化字段名与类型提示
  route                   URL 或移动端 route，可空
  incomplete              是否缺少足够结构信号
  observed_at             观察时间，只作元数据，不进入 collection_key
```

`collection_key` 由除 `observed_at` 外的稳定字段规范化计算。provenance 漂移时必须形成新的 key，
新 slice 不得混入旧 records，并把“集合身份变化”事实暴露给 Transition。相同 provenance 下
`known_total` 发生变化也必须保留为 drift/conflict，禁止像试验 reducer 一样取最大值掩盖变化。
provenance 不完整时，complete 最多得到 `accepted_unverified`。

### 记录身份与去重

跨平台不能假定每条记录都有稳定业务 ID。相同文本可能代表不同记录，同一记录也可能因为 OCR、
字段裁剪或动态排序在不同帧中呈现不同内容。核心层禁止扫描 `id/sku/email/name` 等候选列猜主键。

正向传输身份为：

```text
SliceTransportIdentity
  collection_key
  window_key             page index / scroll range / adapter stable window id，可空
  content_key
  frame_ref              Journal 事件身份，不参与跨帧合并
```

| 情况 | 采集层行为 |
| --- | --- |
| 同 collection、同可靠 window_key、同 content_key | 可丢弃重复 slice |
| window_key 缺失 | 保留整个 slice，即使内容看起来相同 |
| 相邻 slice 部分重叠、无结构 row key | 保留重复，`may_contain_duplicates=true` |
| adapter 提供 `source=structural, stability=collection` 的 transport_row_key | 可在同 collection 内消除传输重叠 |
| 纯视觉/OCR identity | 禁止用于跨帧合并或 confirmed identity |

CollectionView 保留原始规范化记录及来源，不根据显示文本自动合并。按 SKU、URL、业务 ID 或字段
组合去重属于 Data 的语义计划；它必须由任务目标和真实字段决定。所有 materialized list 默认都
可能包含业务重复，`may_contain_duplicates` 只是诊断事实，不改变这一语义。

### CollectionView：投影而不是控制器

不新增可变 `TraversalController`。每次 Transition 决策前，从当前 invocation 的 Journal 事件
纯归约出一个有界视图：

```text
CollectionView
  slices                  已观察的集合片段及 provenance
  records                 未做业务去重的规范化记录
  transport_identities    window/content/frame 等传输身份
  collection_keys         本次采集出现过的集合身份
  known_totals            每个 provenance 上观察到的总数序列
  boundary_evidence       已观察到的终点证据
  available_movements     当前帧可见的滚动/分页能力
  last_move_result        最近移动是否产生新内容
  may_contain_duplicates  是否缺少可靠 transport row identity
  provenance_drift        采集期间集合身份/total 是否漂移
```

它不能提供以下接口或字段：

```text
advance()
next_action()
should_continue()
is_complete()
phase = paging | collecting | done
```

这些都会让投影重新变成确定性状态机。`CollectionView` 只描述事实；Transition 结合 Statement
contract、完整 Memory 和当前观察决定下一步滚动、翻页、选择目标或提出完成。

### CoverageAssessment 与完成否决

Transition 可以提出 `completed`，Runtime 只进行机械合同校验：

- outputs 是否符合 declared returns；
- Interact.success 描述的 UI 后置条件与 AcquisitionSpec 描述的数据后置条件是否同时成立；
- `complete` 是否拥有已知总数、明确终点或等价的可信覆盖证据；
- 已有明确“仍存在下一页”的证据时，不允许声称完整；
- 预算是否已经耗尽。

纯投影可以产生 CoverageAssessment，但不能决定动作：

```text
CoverageAssessment
  state: covered | open | unknown | conflicting
  verification: confirmed | accepted_unverified | null
  reasons
```

| 证据 | assessment / terminal 约束 |
| --- | --- |
| 同一 provenance；确认起点和终点；页码连续覆盖 page_count | `covered + confirmed` |
| 同一 provenance；known_total 等于 certified transport-unique count；无下一页 | `covered + confirmed` |
| 视觉/滚动确认起点和终点；无漂移但无可靠 total | `covered + accepted_unverified` |
| 当前仍有下一页，或 complete 模式未确认起点 | `open`，否决 completed |
| 无边界、无总数 | `unknown`，否决 complete 合同的 completed |
| provenance/total 漂移、slice 截断或证据矛盾 | `conflicting`，否决 completed |
| 预算耗尽且合同为 best_effort | 可 `completed + accepted_unverified` |
| 预算耗尽且合同为 complete | 必须 exhausted/failed，不得伪完成 |

因此 complete acquisition 不要求一律 `verification=confirmed`；可信但较弱的视觉终点可以形成
`completed + accepted_unverified`。禁止的是在 `open/unknown/conflicting` 状态下伪完成。

Runtime 不决定应该先滚动还是点击哪个入口，也不根据页面词表自动补动作。`open/unknown` 时让
同一 Statement 继续决策；只有弱但已覆盖的证据，或 best_effort 在预算边界返回部分结果时，
才能落为 `accepted_unverified`。不能伪造完成，也不能因为一次动作拒绝直接热重编排。

达到终点只证明“一次连续遍历中观察到了从已确认起点到已确认终点的可达内容”，不等于获得了
数据库时点快照。若列表在采集过程中发生新增、删除、重排或筛选漂移，Runtime 不得给出强
`confirmed complete`；应保留时间与 provenance，并降级 verification 或重新采集。

## Transition 上下文

为了让 LLM 真正承担状态识别和流转决策，跨页任务的 Transition 输入必须包含：

1. 不可变 Statement contract：goal、success、inputs、required values、returns 和覆盖要求；
2. StatementMemory：已经执行的动作、回执、已建立事实和失败约束；
3. CollectionView：跨帧记录汇总和覆盖事实；
4. 当前截图，以及可选结构化 observation；
5. 最近一次动作是否改变了页面或带来新记录；
6. 剩余预算和平台当前可用能力。

Transition 仍只输出一个“在哪里做什么”的动作或一个终态提议。CollectionView 解决跨页失忆，
但不替 Transition 选择路线。

完整 records 不应每轮注入 Transition。默认决策包只包含：记录数量、字段结构、当前新增片段、
少量代表样本、集合身份、覆盖证据和最近移动结果。全量物化数据保留给 Outcome 和 Data。若需要
对全量记录做成员判断或聚合，应结束采集后进入 Data，而不是继续膨胀 Transition context。

“当前新增片段”必须来自最近 CollectionSliceEvent，不能固定取累计 records 的前 N 条；否则超过
预览上限后 Transition 将永远看不到新数据。Transition 可见 CoverageAssessment，但它仍是提出
下一步或终态的唯一权威，assessment 只负责在终态校验时提供机械否决。

## 与三个状态域的关系

本方案不增加第四个顶层状态域：

| 数据 | 所有者/来源 |
| --- | --- |
| Program 游标、环境、If/ForEach 帧 | `ProgramRuntimeState` |
| 当前 invocation 的上下文和临时资源 | `StatementRuntimeState` |
| 观察、动作、回执、CollectionSliceEvent 和终态 | `EventJournal` |
| StatementMemory、CollectionView | Journal 的只读纯投影 |
| ObservationMaterializer | 无状态共享转换服务 |
| 数据计划 | Data executor 的一次性局部值 |
| 最终采集/计算结果 | `StatementOutcome.outputs` |

去重集合、页码、完成标志或采集 phase 不应再持久化成平行账本。出于性能需要的 memo 只能缓存
纯 reducer 结果，必须可以随时从 Journal 重建。

## 旧能力的迁移原则

现有 `ListTraversalRuntime`、`TraversalSession` 和浏览器完整读取代码包含可复用能力，但不能把
旧推进逻辑整体恢复为新的权威。迁移时只保留：

- 当前帧记录提取与规范化；
- 观察片段指纹与确定性重复识别；
- 视觉/结构边界信号读取；
- 动作前后内容变化测量；
- 浏览器、iOS 和 Android 的平台动作适配。

以下行为必须退役或留在 Transition：

- 根据内部 phase 自动决定下一个动作；
- 根据页码或滚动计数自动宣布完成；
- 扫描 id/sku/email/name 等业务字段猜跨帧主键；
- 根据显示文本自动做业务记录去重；
- 在 Journal 之外维护长期记录账本；
- 把采集失败直接解释为业务不可行；
- 让遍历器修改 Program 游标或生成 ForEach body。

## 当前落地状态与不符合项

工作区已有独立 `CollectionSliceEvent`、CollectionView reducer、Transition collection block 和
replay/coverage 测试，因此不是从零开始；但完整 Acquisition 合同与跨平台 materializer 尚未落地。

可保留的方向：

- frozen CollectionView 的只读投影骨架；
- Journal replay 等价测试思路；
- Transition collection block 的接线位置；
- 不得暴露 `advance/next_action/phase` 的 ownership 测试。

已修（P0）：

1. 采集事实已从 `PolicyTurn` 拆成独立 `CollectionSliceEvent`；当前 observation 在 Transition
   前先落 Journal，终态 outputs、checkpoint 与 replay 不再依赖 live fold-in。
2. reducer 只物化最新 `collection_key`，不同 route/filter/schema/surface 的 records 不再静默混账；
   provenance 与 known_total 漂移保留为诊断/冲突事实。
3. 传输去重只在可靠 `window_key + content_key` 同时存在时启用；没有 window key 时 append-all，
   不扫描 id/sku/email/name 猜业务主键，重叠窗口允许重复。
4. `coverage=complete` 的 complete 提议由 Runtime 机械校验 Journal 投影的 `coverage_status`：
   `incomplete` / `conflicting` / `unknown` 同帧否决并重决策，不得伪造成功。
5. slice 截断、同 collection 的 known_total 漂移会成为 conflicting；合法重叠窗口不会再因
   raw record count 大于 total 被误判为冲突。

仍须替换或修正：

1. `OutputSpec.coverage` 污染通用输出合同（设计更倾向 Interact 侧 AcquisitionSpec）；
2. `at_end -> complete` 仍缺少明确起点/连续页覆盖验证；
3. Transition 固定读取累计 records 的前 30 条而非最近增量；
4. Data 根据 OutputSpec coverage 推导 `require_complete`；
5. prompt 过早把 coverage status 当成可直接复用的 complete 事实；
6. browser table sensor 已有部分接线，移动端视觉 materializer 和统一 NormalizedObservation 尚缺；
7. Interact/Data/跨帧 reducer 尚未共用 ObservationMaterializer；
8. 当前通过结构化 collection affordance 排除普通 dashboard table，但仍需 AcquisitionSpec.fields/
   predicate_ref 完成目标集合的正向语义绑定。

## 建议落地顺序

### 0. 最小闭环验证

先用一个两页表格 fixture 验证：

```text
两帧 observation
-> 两个 CollectionSliceEvent
-> CollectionView 纯归约
-> Interact StatementOutcome.outputs
-> Data count/filter
-> Journal replay 得到相同结果
```

这个阶段不接入自动翻页、不恢复 `TraversalSession`、不修改 Transition prompt，先验证事实流、
provenance 和 Data 边界。

### 1. 建立事实与投影

- 定义 AcquisitionSpec、CollectionProvenance、NormalizedObservation 和 CollectionSliceEvent；
- 实现共享 ObservationMaterializer 和紧凑 CollectionSliceEvent；
- 实现无副作用的 CollectionView reducer；
- reducer 默认 append-all；仅用 certified transport identity 消除传输重复；
- 用多帧 fixture 验证精确 slice 重写、部分重叠、合法重复记录、内容变化和边界证据；
- 验证 reducer 只读 Journal，不接管动作决策。

### 2. 接入 StatementMemory 与 Transition

- 将有界 CollectionView 注入 Transition；
- 只注入摘要、最近增量和样本，不注入全量 records；
- 要求 Transition 继续输出通用的 where+what 动作；
- 验证它能基于历史记录避免重复采集，并在覆盖不足时继续探索。

### 3. 统一终态输出

- Interact 完成时从 Journal 投影 materialized rows；
- 终态 observation 必须先追加 CollectionSliceEvent，再物化 outputs 和 Outcome；
- 严格校验 typed returns、AcquisitionSpec、provenance 与 CoverageAssessment；
- 删除“只看最后一帧”的集合输出假设。

### 4. 串通 Data 与控制流

- Data 只消费物化 records 或当前帧 snapshot；
- 用确定性 kernel 完成筛选、聚合和计算；
- If/ForEach 只消费 Data 的类型化结果。

### 5. 回放与跨平台验证

至少覆盖：

- 浏览器滚动加载列表；
- 浏览器显式分页表格；
- 当前帧读取，不产生多余 UI 动作；
- 已知总数与观察终点不一致；
- 两条显示文本相同但业务身份不同的合法记录；
- 翻页过程中筛选或集合身份发生变化；
- 动态列表在采集期间新增、删除或重排；
- 移动端无 DOM、仅视觉与手势的长列表；
- 跨页采集 -> StatementOutcome.outputs -> Program env -> Data 的 acquisition E2E；
- checkpoint 后从 Journal 重建 CollectionView，结果与中断前一致；
- 恢复后物理页面不在原位置时，由 Transition 重新绑定并允许重叠扫描。

## 其他运行风险

### LLM 调用成本

每次滚动或翻页都调用 Transition 最通用，但长列表的延迟和 token 成本可能不可接受。首版应先
保证决策正确；后续可以增加可审计的 adapter 批量 acquisition capability，但必须满足：

- 内部停止条件只允许 `at_end`、连续 N 次 content_key 不变、预算或动作失败；
- 禁止根据业务 goal、record 内容或 known_total 是否满足来停止；
- 每个微帧都产生 CollectionSliceEvent，或一次返回等价且可 replay 的 compact slice 序列；
- adapter 只能结束批量动作，不能宣布 Statement completed；
- Statement 终态仍只由 Transition 提议、Runtime 机械校验。

否则“批量能力”只是 TraversalSession 换皮。

### ForEach 失败语义

多记录 mutation 仍需单独明确 fail-fast、继续并收集失败项、每项/全局重试预算，以及 checkpoint
恢复时如何利用 mutation receipt 避免重复写入。这属于 ProgramRuntime/ForEach 合同，不能交给
CollectionView 或 Data 临时决定，应单独形成 `foreach_mutation_design.md`。

本文的 acquisition 验收只到 `StatementOutcome.outputs -> Program env -> Data`。shopping_admin 的
批量 mutation 可作为组合集成测试，但不能拿它证明采集子系统单独正确。

### 存储、隐私与上下文

跨页记录会增加 Journal、checkpoint 和报告体积，也可能包含敏感数据。CollectionSliceEvent 应只
保存声明输出所需的规范化字段、provenance 和 artifact 引用，不复制完整 DOM 或截图 bytes；同时
需要字段裁剪、大小上限和敏感值处理。Journal 保存完整事实不代表 LLM prompt 必须携带完整事实。

### Checkpoint 的能力边界

从 Journal replay CollectionView 只能恢复逻辑采集结果，不能恢复浏览器滚动位置或移动端物理
页面。恢复后必须重新观察和绑定集合，并接受 at-least-once acquisition；没有额外事务能力时，
不能声称 exactly-once traversal。

恢复后的新 slice 使用新的 frame_ref；只有具备同 collection_key、可靠 window_key 和相同
content_key 时才做传输去重。否则允许与恢复前逻辑记录重叠，最终由 Data 按业务语义去重。

## 验收不变量

实现完成时必须满足：

- Program 中没有滚动次数、页码、分页按钮或平台手势；
- Transition 是 Statement 内唯一的下一步决策权威；
- Journal 之外没有第二份可变采集账本；
- CollectionSliceEvent 不包含 phase、下一动作或完成标志；
- 无动作终态帧也必须先写 CollectionSliceEvent，replay 不依赖 live observation；
- 采集层不根据业务字段或显示文本做语义去重；
- 缺少可靠 window/row identity 时保留重叠记录，并标记 may_contain_duplicates；
- provenance 漂移后不得把 records 和 known_total 合入原 collection；
- Data executor 不触发 UI 动作；
- Interact executor 不执行集合聚合和业务数值计算；
- ForEach 不承担 UI 数据发现，只迭代已经物化的 list；
- 浏览器结构信息缺失时仍可基于视觉工作；
- 完整覆盖声明必须由可引用事实支持；
- 覆盖证据必须绑定同一 collection provenance 与语义 scope；
- complete 可以 accepted_unverified，但 open/unknown/conflicting 不得 completed；
- Transition 默认只接收集合摘要和当前增量，不接收全量 records；
- Transition 的增量预览不得固定为累计 records 的最早 N 条；
- 所有采集与处理结果只通过 `StatementOutcome.outputs` 进入 Program 环境。

这套边界保留原框架成熟的滚动与跨页能力，同时把流转权集中到 Transition、事实集中到
EventJournal、数据计算集中到 Data executor，避免通过恢复旧遍历器再次引入隐式状态机。
