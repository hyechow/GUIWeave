---
id: task.orchestrator.foreach_select
source_type: task_template
platform: shared
scope:
  - orchestrator_expansion
owner: gui_agent.core.orchestrator.expansion
schema: _SelectDraft
eval_suites:
  - scripts/progressive_expand_experiment.py
version: 1
---
你在渐进式编排的【foreach 检查点】做**成员圈选**:骨架计划声明了目标集合的语义描述,候选行已从页面真实采集到(下方 JSON)。

看着真实行数据**逐行判断**哪些行属于目标集合描述的成员,输出它们的【行号】(0 起,基于给出的顺序)。

- 不要归纳谓词、不要猜编码规律——直接按每行的实际字段值(名称/SKU/状态/数值)对照成员描述判断。
- 注意排除:形似但不满足描述的行(如不同尺寸/颜色的兄弟变体、无规格后缀的父产品、状态不符的记录)。
- 若没有任何行属于目标集合,给空列表(这是合法结果,不要硬凑)。

只输出 JSON:{"member_row_indices":[...],"reason":"一句话:圈选依据"}
