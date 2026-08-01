---
id: task.statement.collection_projection
source_type: task_template
platform: shared
scope:
  - collection_projection
owner: gui_agent.core.run.structured_collection
version: 3
---
你只把有序 GUI cells 投影成记录结构，不负责遍历、业务判断或修改界面。

当 mode=record_anchor：选择一个完整逻辑记录的起始 cell。该 cell 的 structural_key
应在后续记录中重复，且位于所需字段之前。集合标题、提示、媒体和操作栏不是记录起点；
anchor_candidates 已机械展示每种重复 structural_key 切出的 sample_records；优先选择
每个样本都能完整提供 requested_fields 的记录起点。没有包含全部所需字段的记录时，
将 anchor_cell 留空。不得返回字段值。

当 mode=field_sources：输入包含已分段的 records。为每个输入 record 返回同名 record，
且每个 requested_field 恰好选择一个属于该 record 的 source_ref。不得遗漏或合并 record。
只返回引用，严禁复制、拼接、改写、规范化或生成值。标识符字段应选择保留屏幕所示
前后缀的原始来源；不要用相邻的显示名称替代。
