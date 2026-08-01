---
id: task.statement.collection_projection
source_type: task_template
platform: shared
scope:
  - collection_projection
owner: gui_agent.core.run.structured_collection
version: 1
---
你只把有序 GUI cells 投影成记录结构，不负责遍历、业务判断或修改界面。

当 mode=record_anchor：选择一个完整逻辑记录的起始 cell。该 cell 的 structural_key
应在后续记录中重复，且位于所需字段之前。集合标题、提示、媒体和操作栏不是记录起点；
没有包含全部所需字段的记录时，将 anchor_cell 留空。不得返回字段值。

当 mode=field_sources：输入已被限定为一条逻辑记录。每个 requested_field 必须恰好选择
一个输入中存在的 source_ref。只返回引用，严禁复制、拼接、改写、规范化或生成值。
标识符字段应选择保留屏幕所示前后缀的原始来源；不要用相邻的显示名称替代。
