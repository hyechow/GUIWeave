# ctx.API 状态依赖与查询降解

## 问题

task-108 的旧程序把筛选、分页和采集都压进一个 GUI Statement。旧导航 API 没有返回值，
后续 `ctx.query/read` 只能依赖进程级“当前页面”；Statement 的 success 又只是 goal 的
同义改写。结果是 executor 既没有可机械验收的局部终点，也没有向下一阶段交接状态的值流。

`20260724_194421` 还暴露了另一处契约冲突：`ctx.query(filters=...)` 被降解到禁止改变
上下文的 lookup Statement，但应用筛选本身会改变集合视图，因而 Apply Filters 被 gate
当成 commit 拒绝。为按钮文案继续加白名单不能消除这个矛盾。

## 最小机制

公共 API 只保留一条显式状态链：

```python
state = ctx.reach(
    "Open orders",
    success={
        "entity": "Orders",
        "fields": ["Status", "Purchase Date"],
    },
)
rows = ctx.query(
    state,
    entity="Orders",
    fields=["Status", "Purchase Date"],
    filters={"Status": "Complete"},
)
```

- `ctx.reach(...) -> UIStateHandle`：只建立下游所需的结构化 collection。
- `ctx.query(state, ...)`：必须消费 Runtime 签发的状态，固定降解为
  `locate_collection -> constrain_collection -> acquire`。
- `ctx.read(state, ...)`：必须消费状态；指定 target 时先 focus，focus 成功后签发派生状态。
- handle 只由成功的 GUI/focus Statement 签发，并直接携带已验证后置条件和观察状态。
  生成代码无法构造这种 Runtime 类型。

没有独立的 `surface_active` 抽象。当前需求只需要 collection 状态；用 document title
再建一套跨平台 surface schema 会扩大核心、adapter 和 gate，却不能改善查询交接。

## 阶段所有权

这些内部步骤共用一个 `CollectionIntent`，只由 `phase=reach|locate|constrain`
区分，不再维护三个意图类和判别联合。

| phase | 输入 | 后置条件 | 可拥有的效果 |
| --- | --- | --- | --- |
| `reach_collection` | — | 唯一 collection 覆盖 required fields，签发 state | navigation、authentication、presentation、viewport |
| `locate_collection` | state | 唯一 collection scope | query_control、presentation、viewport |
| `constrain_collection` | scope | adapter 的完整筛选谓词与请求精确相等 | query_control、presentation、viewport |
| `acquire` | constrained scope | materialized rows | pagination 与 collection traversal |
| `focus` | state + target | target fields 可读，签发派生 state | 普通交互 |
| `read` | state | 结构化字段值 | 读取 |

动作准入基于 browser adapter 在结构 grounding 后产生的单一 `effect_kind`，
不读取 Apply、Save、Previous 等本地化文案。受限阶段遇到
未知效果 fail closed；普通 Interact 也不能抢占 Acquire 的 pagination 所有权。

## Statement 语义

Statement executor 只看到局部 `goal + success + interaction_intent`。父级目标留在
Program/RunRecord，不复制进局部 contract，避免单个 Statement 持续追逐全局业务目标。

筛选使用平台中性的 `FilterPredicateSet`。Browser adapter 把实际筛选状态翻译为
`AppliedFilterState`；LLM 的 complete 只是候选，只有两者在 complete coverage 下精确相等
才能结束 constrain。日期显示差异只做安全正规化，例如 `1/01/2023` 与 `01/01/2023`。

## 静态与 Runtime 边界

静态校验只要求 `ctx.reach.success` 是包含 `entity` 与 `fields` 的 literal，并检查 API 签名。
实际执行和 probe fixture 统一验证 handle 类型、reach/query 的 entity/fields 对接和
scope 来源，不再维护状态注册表，也不在 AST 层另建一套 def-use 分析。旧
`lookup_request/constrain_request` schema 迁移不再保留，避免同时维护两套契约。

## task-108 闭环

- `logs/gui_agent/webarena/browser/20260726_100005`：Orders 表已经出现，但旧 GUI
  Statement 仍在两页间循环，query 永远未接管。
- 最小回放 `replay/fixtures/browser/100005_gui_handoff/` 证明 collection headers
  可以直接完成 `reach_collection` 并把 state 交给 query。
- `logs/gui_agent/webarena/browser/20260726_112445`：report 展示
  `reach -> ui:1 -> locate -> constrain -> acquire`；WebArena score 为 `1.0`，
  Jan–May 结果为 `12 / 7 / 5 / 9 / 5`。

核心 prompt 和 gate 不包含 WebArena、Magento 或 task 编号知识；运行日志只作为回放证据，
不成为生产依赖。
