---
id: task.reader.screenshot_text
source_type: task_template
platform: shared
scope:
  - reader
owner: gui_agent.core.llm.reader
eval_suites:
version: 1
---
从截图中提取所有可见的文字内容。
- 每条记录一行，字段用|分隔（时间|名称|内容|状态 等）
- 保留所有可见字段，不做筛选、不汇总、不解释
- 最多200字
- 无可见文字内容则回复"无相关内容"
