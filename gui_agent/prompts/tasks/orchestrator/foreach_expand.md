---
id: task.orchestrator.foreach_expand
source_type: task_template
platform: shared
scope:
  - orchestrator_expansion
owner: gui_agent.core.orchestrator.expansion
schema: _ExpandDraft
eval_suites:
  - scripts/progressive_expand_experiment.py
version: 1
---
你在渐进式编排的【foreach 检查点】:骨架计划声明了「对目标集合的每个成员执行子目标」,现在候选行已从页面真实采集到(下方 JSON)。完成两件事:

1. **圈选成员**(member_row_indices):看着真实行数据逐行判断哪些行属于子目标描述的目标集合,给出它们的【行号】(0 起,基于给出的顺序)。不要归纳谓词——直接按每行的实际字段值判断。若没有任何行属于目标集合,给空列表。
2. **产出成员 body**(body):对每个选中成员执行的具体步骤列表(所有成员共用,用 {循环变量[字段]} 引用当前成员的行字段)。可用步骤:
   - {"op":"run","kind":"navigation|filter|action|read","name":"...","success_condition":"...","var":"...","returns":[...],"read_spec":"..."}
   - {"op":"compute","var":"...","expr":"受限表达式(round/float、算术、{循环变量[字段]} 或已读变量)"}
   - {"op":"if","cond":{"var":"某读取步的var","field":"字段","cmp":"empty|exists|==|!=","value":"..."},"then":[...],"otherwise":[...]}(分支;空值回退用 cmp="empty")
   规则:运行时才知道的值先读(returns+read_spec)再用;算出的值必须以 {变量名} 模板写进后续 action 的 name(不写「新值」泛指);success_condition 写可见终态;不要 finish(循环外骨架已有)。
若子目标含「读某属性,为空则回退到父/关联实体」:body 必须写成 读取步(returns 该属性)→ if 该字段 empty → then 分支里 compute 派生关联键(如 name.rsplit('-', 2)[0] 去掉“-尺寸-颜色”后缀)→ 用 {派生变量} 搜索并打开关联实体 → 再读该属性;otherwise 分支留空(已读到)。

只输出 JSON:{"member_row_indices":[...],"body":[...],"reason":"一句话"}
