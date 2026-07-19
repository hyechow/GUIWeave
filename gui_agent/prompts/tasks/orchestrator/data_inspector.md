---
id: task.orchestrator.data_inspector
source_type: task_template
platform: shared
scope:
  - data_executor
owner: gui_agent.core.run.statements.data
schema: DataInspection
version: 1
---
判断当前 DataContextView 是否已经包含每个 `required_fields` 语义字段。

这是只读 schema 检查，不做数据变换，也不操作 UI。根据实际结构字段、当前截图和字段含义建立
`semantic field -> actual field` 的 bindings；不得因为字段“可能存在于详情页/隐藏列”就判 available。
只有所有字段在**当前数据源、当前观察**中都可读取时 available=true。否则 available=false，并列出缺少的
语义字段。available=false 不是“该字段在应用中永远不存在”，也不是“查询结果为空”；它只否定当前
source capability。Program 可先让 Interact 调整当前视图，最终仍不可读时再携证据热重编排到另一来源。

不得输出 CSS、XPath、点击路径或让本步骤打开列设置。缺列后的业务分支由 Program If 决定。
