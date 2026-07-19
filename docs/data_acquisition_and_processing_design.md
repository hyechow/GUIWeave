# 运行时数据采集与处理设计

> 状态：已落地的架构合同。完整集合采集由独立 `Acquire` 执行；`Interact` 不再声明业务
> outputs，当前帧读取与验收统一由 `Data` 执行。Browser、iPhone 与 Android 共用无状态的
> `NormalizedObservation`；结构信号不可用时，Data/Acquire 以截图作为跨平台基线。

## 决策

GUI 数据工作拆成三个不同权限的步骤：

```text
Interact：圈定业务集合、让筛选生效、暴露所需字段（不返回业务数据）
    ↓
Acquire：只在同一集合内搬运窗口并物化 list[record]
    ↓
Data：读取当前帧或处理已物化数据
    ↓
If / ForEach：显式业务分支与固定循环
```

这不是把旧遍历状态机改名。Acquire 的唯一目标是传输同一集合的窗口；它不理解业务记录、
不改变筛选、不打开详情、不计算结果，也不能宣布 Program 完成。

## 编译合同与 Program 合同

字段可读性分支属于 Program，但固定 wiring 不由 Decomposer LLM 手写。语义草稿只声明 Data 的
source coverage、required_fields 和 prepare_source；Compiler 确定性生成下面的
inspect / If / Acquire 节点、固定 outputs、引用和 statement id。

### Interact

Interact 只到达一个线性 UI 后置条件。它可以跨页面、dialog 或 screen，但不得声明业务
`returns`。当前帧的 text/number/boolean/record 读取属于 Data，跨窗口 `list[record]` 物化属于
Acquire。Interact 的终态 observation 必须原样交给紧邻的 Data；两者之间不得执行新的 UI 动作。

### Acquire（lowered Program）

```python
Acquire(
    id="collect_orders",
    bind="observed",
    goal="Materialize every reachable record from the scoped collection",
    source_check=ValueRef(var="schema", path=["available"]),
    returns={
        "rows": OutputSpec(
            type="list[record]",
            coverage="complete",
            description="stable record identity, customer identity and final amount",
        )
    },
)
```

机械约束：

- 必须且只能声明一个 `list[record]` output；
- coverage 只能是 `complete` 或 `best_effort`；
- `source_check` 必填，必须引用 Data inspect 的 boolean output；
- 不允许页面路径、页数、手势次数、真实列名、CSS 或 XPath 进入 Program；
- 完成结果只通过 `StatementOutcome.outputs` 进入 Program env。

### Data inspect 与 Program If（Compiler 生成）

字段是否可读是运行时事实，不由 Compiler 猜测，也不由 Acquire 临场修 UI：

```text
Data(mode=inspect, required_fields=[语义字段...])
  -> available:boolean, bindings:record, missing_fields:json
If available == false
  -> Interact：暴露字段 / 切换正确视图
Data inspect（最终检查）
Acquire(source_check=最终 available)
```

If 只决定是否修正 UI；Acquire 在 If 后只有一个，因此没有分支 bind 合流。Data inspect 只绑定“语义字段 → 当前真实字段”。它不点击 Columns。缺列、集合未圈定、需要换另一张
业务表，都属于 Program/Interact；只有已绑定集合确实无法移动才属于 Acquire blocked。
最终检查仍失败时，由 Acquire 的 source_check 机械返回 infeasible，现有 RecoveryRouter 再根据
Journal 中的失败事实触发剩余 Program 热重编排；这不是对“字段存在”的一次性全局断言。

消费 Acquire 集合的 Data derive 必须在 `required_fields` 重述其分组、筛选、排序和最终输出所需的
语义源字段。Compiler validator 要求它们被每条可达 Acquire 分支引用的 Data inspect 覆盖；这是
Program 数据依赖，不是运行态字段缓存。

当前帧 schema、总数或可见记录已经足够回答问题时，Compiler 直接使用 Data，不插 Acquire。

### UI 谓词下推

用户明确给出的状态、日期范围、关键词、类别或归属等集合条件，优先通过 Interact 使用当前 UI 的
搜索、筛选、排序或视图能力生效；Acquire 随后只物化已经圈定的集合。Data 主要处理分组、聚合、
排名、投影以及 UI 无法可靠表达的残余条件，不能默认采集未筛选的全量集合后重复实现 UI 筛选。

Program 只保存语义条件和值，不保存真实控件或列名。若 Interact 在运行时证明当前 UI 不支持该条件，
RecoveryRouter 才能选择显式的 `Acquire → Data filter` 降级路线；初始编排不能静默假设 UI 不支持，
也不能清除与用户口径一致的现有筛选。

### 数据读取、验收与纠正

Data 是业务数据事实及数据验收依据的唯一 owner，但没有 UI 纠正权。UI 动作是否到达声明的界面
后置条件仍由 Interact/Transition 判断；Program 决定验收不通过后的分支。

```text
Interact：执行修改并到达“已保存”的 UI 后置条件
Data：从终态 observation 读取保存后的业务事实
If satisfied
  -> 继续 / Finish
Else
  -> Interact：纠正可确认的不满足项
  -> Data：重新读取并验收
```

数据验收必须区分三种结果：

| 结果 | 含义 | 处理 |
| --- | --- | --- |
| satisfied | 数据可读且满足声明条件 | Data completed，输出事实和 `satisfied=true` |
| unsatisfied | 数据可读但不满足声明条件 | Data completed，输出事实和 `satisfied=false`；由 Program If 进入纠正 Interact |
| unavailable | 当前来源或证据无法读取所需事实 | Data infeasible，携带缺失来源证据和 kickback；不得伪装成 false |

Data 禁止为了验收而点击、导航、切 tab、展开区域、暴露列、修改筛选或写入数据。已知的纠正路径必须
显式存在于 Program；未预见的数据源不可用由 RecoveryRouter 热重编排剩余 Program。纠正之后必须由
新的 Data 再读，不能复用纠正前的读取结果。

Compiler 必须把“Interact 后读取/验收”正规化为相邻 Data，而不是要求 Decomposer LLM 描述 DOM、
视觉抽取或字段绑定细节。Program validator 拒绝 Interact 的业务 returns，以及 If/ForEach/Finish 对
Interact 业务输出的直接依赖。

### 实体检索分支

检索匹配模式同样属于 Program，而不是 Statement 内部 improvisation。Decomposer 用一个 compile-time
`lookup` macro 指明 Router mention 与承载它的语义字段，Compiler 展开为：

```text
Interact：完整 mention 精确检索（零结果也是有效终态）
Data：读取 match_count
If match_count == 0 且 Router 允许 approximate
  -> Interact：在同一语义字段使用 Router search_hint
Data：读取最终 match_count
If final match_count > 0
  -> found branch
Else
  -> confirmed no-result branch
```

真实控件、真实列名和到达检索界面的动作仍由 Interact React 决定。第二次仍为零时保留为空结果事实，
不把“用户提到了实体”错误解释成“实体必然存在”。

## 运行时所有权

| 事实/决策 | Owner |
| --- | --- |
| 集合范围、筛选和字段可见性 | Interact / Program |
| 同一集合下一窗口如何暴露 | Acquire adapter 或 AcquirePolicy |
| 每个窗口实际观察到的 records/provenance | EventJournal |
| 跨窗口只读汇总 | CollectionView reducer |
| 是否达到可信采集终点 | Acquire Runtime 机械校验 |
| 当前帧业务数据读取、数据验收、物化集合内的残余筛选、排序、去重、分组、聚合 | Data |
| 数据不满足后的 UI 纠正路径 | Program If / RecoveryRouter → Interact |
| 业务分支与逐记录固定 body | If / ForEach |

Transition 只处理业务 Interact 的“当前状态 + 下一步在哪里做什么”。它看不到累计采集记录、分页
状态或 coverage，不负责翻完一个集合。

## 绑定规则

Acquire 启动时只接受以下机械绑定：

1. 恰好一个带可靠 traversal/total 的结构集合：自动绑定；
2. 零个可靠结构集合、但 Browser 有可物化候选：允许 AcquirePolicy 第一枪只声明一个当前候选
   `bound_hint`；不得同时执行移动；
3. 两个及以上可靠结构集合：infeasible/ambiguous，不允许视觉猜一个；
4. 没有任何可物化集合：infeasible，不让 Policy 满页寻找业务表。

绑定后 provenance 漂移或集合消失即失败；fallback 不得重新选择另一张业务集合。

## 自适应采集策略

每个窗口按能力选择路径：

```text
可靠 adapter traversal
  -> structured move（零 LLM）
  -> 失败事实写 Journal
  -> AcquirePolicy React fallback
```

结构能力恢复时可以回到 structured，但必须满足：

- 每个新窗口只 probe 一次同一 capability fingerprint；
- 同一失败 capability 不重试；
- 同一窗口不 structured ↔ react 抖动；
- React 成功前进到新 content/结构 fingerprint 后，才允许重新 probe structured。

这些信息不保存在私有 session phase 中。每轮都从 Journal 的 slices 和 receipts 重建。

## AcquirePolicy

AcquirePolicy 是采集移动的局部控制器，不是业务 Transition。输入只包含：

- Acquire goal 与 coverage；
- 当前绑定区域；
- 候选集合的 ref/caption/headers/count/能力摘要；
- 累计 record count、known total、coverage 摘要；
- 最近 move receipts 与失败 capability；
- 当前截图。

它不接收业务 StatementMemory，也不接收完整累计 records。输出空间固定为：

```text
move | boundary | blocked
```

move 的 action family 硬白名单：

```text
bind_region
paginate_next | paginate_prev
scroll_forward | scroll_backward
load_more | wait
```

Runtime 同时校验 Action Policy 的物理动作和 adapter 的绑定 affordance：

- pager/load_more 只允许绑定分页区域内的 tap；
- scroll 只允许绑定集合区域内的 scroll/drag；
- type、open_detail、activate_row、filter、Columns、sidebar、URL navigation、tab/app switch 一律拒绝。

这是动作权限校验，不是业务正则 guard。

## 数据面与控制面

完整 records 只存在于 CollectionView、Program env 和确定性 Data kernel。LLM 控制面只接收
`DatasetDescriptor`：稳定变量引用、producer、record_count、字段/类型、有界样本、coverage、
verification、filter snapshot 与 provenance。它选择 DataRef 和算子，不接收或复制完整记录。

DataPlan 执行前由纯类型预检沿线推导 `scalar / record / list[record] / table`，只校验 binding 拓扑、
transform source 与 emit 顶层基数。字段存在性仍由 Data kernel 的真实执行和最终 OutputSpec 校验负责，
不在预检中复制一套字段类型系统。错误携带实际 shape、目标 shape 与合法引用方式，只允许交给同一个
Data statement 做一次 bounded repair；预检不持有 phase，也不写 Journal。

Data 已有显式物化 input 时，该 input 是唯一集合权威；当前页面的局部窗口不会作为并列 dataset 注入，
也不能把上游 `complete + confirmed` 降级为 partial。Transition 不接收采集 records。热重编排同样只
接收 completed binding manifest，不展开 `StatementOutcome.outputs`；新 Program 通过原变量名引用
仍在 env 中的数据。

## Journal 与 replay

唯一持久事实为：

```text
CollectionSliceEvent
  statement_instance_id / statement_id
  collection_key / provenance / window_key / content_key
  records / known_total / boundary / source / strategy

AcquisitionReceiptEvent
  strategy / capability / action_family / status
  collection_key / bound_region
  before_content_key / after_content_key / reason
```

明确禁止：

- `phase=paging|seeking_end|done`；
- 私有页码、游标、完成 latch 或失败计数账本；
- Journal 与 Acquire session 双写；
- 从显示文本、name、SKU 或 URL 猜业务身份并静默合并记录。

`AcquireMemoryView` 与 `CollectionView` 都是纯 reducer。checkpoint 后重放 Journal 可得到相同逻辑
记录、绑定、失败能力与最近策略；物理页面位置仍需 adapter re-bind。

统一离线校验入口：

```bash
bin/replay_run logs/gui_agent/<run-directory>
bin/replay_run logs/gui_agent/<run-directory> --json
```

该命令只重建并交叉校验 PolicyContext、ProgramRuntime、env/run log、CollectionView、
AcquireMemoryView 和已保存的 observation snapshots；它不会连接平台或调用 LLM。重新预测
Transition/AcquirePolicy、以及物理动作执行不属于 state replay。

## 记录身份与 provenance

采集层只消除可证明的传输重复：同 collection、可靠 window key 和相同 content key 的重复 slice。

| 情况 | 行为 |
| --- | --- |
| 整 slice transport identity 相同 | 可丢弃重复写入 |
| 相邻窗口部分重叠、无结构主键 | 保留重复，标记 may contain duplicates |
| adapter 给稳定结构 row key | 可用于传输层重叠判断 |
| 纯视觉/OCR 文本相同 | 不得升级成 confirmed identity |

业务去重属于 Data。Collection provenance 至少包含 surface、filter snapshot、schema、route；任一稳定
维度漂移会形成新 collection key，旧记录不得静默混入当前集合。

## 终点与 verification

| 证据 | 结果 |
| --- | --- |
| `has_next=false/at_end` + provenance 一致 | confirmed complete |
| known total 对齐且当前无 has-next | confirmed complete |
| 仅模型 boundary 提议 | 不完成 |
| boundary + 同集合/同区域连续 2 次向前 move 无 content 变化 | confirmed complete |
| best_effort 预算耗尽 | completed + accepted_unverified + 部分 rows |
| complete 预算耗尽 | exhausted，禁止伪 complete |
| provenance/total/truncation 冲突 | 禁止 confirmed |

React 的 boundary 只是 Journal 提议；Runtime 的 `N=2`、同 collection key、同 bound region、已派发
向前动作和 content key 不变共同形成机械确认。

## 批量 adapter 能力

Adapter 可提供批量 acquisition，但必须：

- 每个窗口回调一个 CollectionSliceEvent（完全相同 transport slice 可去重）；
- 停止条件只允许 at_end、连续内容不变、预算或动作失败；
- 不根据业务 record 内容、目标满足或 known_total 业务语义提前停止；
- 不写 phase，也不直接宣布 Statement/Program 完成。

## 冻结不变量

1. Interact 不声明业务 `returns`；当前帧业务数据只由 Data 输出，完整集合只由 Acquire 输出。
2. Acquire 无绑定集合时不启动 Policy 满页搜表。
3. Policy 输出仅 `move|boundary|blocked`，越权由 Runtime 拒绝。
4. move 只允许翻页、滚动、load more、wait。
5. 缺列、改筛选、换业务集合只走 Data inspect + If + Interact。
6. boundary 不是终态；必须有结构证据或视觉证据加连续无进展。
7. 每窗事实进 Journal；Policy 没有私有 phase。
8. 同失败 capability 不重试；策略切换有迟滞。
9. structured 可用时 AcquirePolicy LLM 调用数为 0。
10. 当前帧足够的数据任务不得被 Compiler 强行插入 Acquire。
11. Data 验收只能读和判断；`unsatisfied` 进入 Program 分支，`unavailable` 不得伪装成 false。
12. Interact 终态与相邻 Data 读取之间不得插入 UI 动作或换用无关 observation。

## 验收

- 结构分页采全 records，断言 `llm_calls_on_acquire_policy == 0`；
- 破坏结构 movement 后只降级一次 React，恢复到新窗口后才重新 probe；
- Policy 提议打开记录、修改筛选或导航时机械拒绝；
- 两个结构集合时不视觉猜选；
- 假 boundary 在两次同集合无进展前不能完成；
- complete 预算耗尽不返回部分成功；best_effort 明确降级；
- checkpoint/replay 的 CollectionView 与 AcquireMemoryView 等价；
- 两条显示文本相同但业务身份不同的记录不在采集层合并；
- current-view count 类任务编译为 Data，不生成 Acquire；
- 全量排名/分组类任务编译为 Interact → 可选 inspect/If → Acquire → Data。
- Interact 保存成功但 Data 读到业务值不满足时，Program 进入纠正 Interact，纠正后由新 Data 重读；
- 当前证据读不到验收字段时返回 unavailable/kickback，不得误判 unsatisfied；
- Program validator 拒绝 Interact 业务 returns 及其被 If/ForEach/Finish 直接消费。
