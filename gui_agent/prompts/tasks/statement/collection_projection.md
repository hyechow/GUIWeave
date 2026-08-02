---
id: task.statement.collection_projection
source_type: task_template
platform: shared
scope:
  - collection_projection
owner: gui_agent.core.run.structured_collection
version: 10
---
你只把有序 GUI cells 投影成记录结构，不负责遍历、业务判断或修改界面。

当 mode=record_anchor：collection_goal 定义要采集的记录。根据 anchor_candidates 的样本，
选择能让每段记录完整提供 requested_fields 的起始 cell；集合标题、空态提示、导航、媒体和操作栏
都不是记录。没有目标记录时将 anchor_cell 留空。requested_field_types 只用于理解字段语义。

当 mode=field_sources：输入包含已分段的 records。为每个输入 record 返回同名 record，
并为每个 requested_field 从对应 field_candidates 中选择一个 source_ref。不得遗漏、合并或新增
record。选择完整表达字段的最小屏幕原文来源，并保留原始标点和前后缀。source_ref 是不透明标识符，
只能逐字符复制输入中已有的引用；不得返回、
拼接、改写或推断字段值。
