---
id: task.self_learning.gen_when.prompt
source_type: task_template
platform: shared
scope:
  - self_learning
owner: gui_agent.core.self_learning.gen_when
eval_suites:
rendered: true
version: 1
---
下面是一节应用操作手册。写一行「何时需要查阅本节」的触发描述，要求：
- 不超过 45 字、单行、不要以句号结尾、不要复述操作步骤
- 覆盖本节对应的操作/场景关键词；若标题和正文对同一事物用了不同叫法，两种叫法都要包含
- ⚠️ 只能使用本节标题或正文中出现过的名词，不得引入本节没有的概念或对象
- 只输出这一行，不要任何前后缀

章节标题：{title}

章节内容：
{body}
