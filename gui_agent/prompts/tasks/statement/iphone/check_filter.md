---
id: context.statement.iphone.check.filter
source_type: context_block
platform: iphone
scope:
  - checker
owner: gui_agent.adapters.iphone.supervisor.statement
eval_suites:
  - evals/iphone/checker
version: 1
---

## 筛选类子目标（kind=filter）
- 截图必须显示精确的筛选条件或等价范围，才能判 done
- 更宽的范围不能当作筛选完成；即使可见项都在目标范围内，筛选摘要显示更宽范围也不能 done
- ⚠️ 即使 in_progress，也必须在 missing_evidence 中写出「当前值」与「目标值」（例：当前=5月整月、目标=05-18~05-24），供规划器决定调整方向。

## 搜索类子目标（kind=filter，含搜索操作）
判 done 必须同时满足：
1. 当前页面是结果页，不是信息流、建议页、历史页或加载页
2. 搜索框或标题显示完整目标查询/条件
3. 页面显示与查询对应的结果列表或详情

⚠️ 搜索建议页 vs 搜索结果页 区分：
- 若搜索框右侧仍显示「搜索」按钮（尚未提交），或搜索框处于激活输入状态，则当前是自动补全/建议页——即使下方列表出现了目标商家名称，也必须判 in_progress
- 搜索建议项通常左侧有放大镜图标或时钟图标，且没有评分、标签等商家详情元素
- 只有用户已提交搜索（按下搜索按钮或回车），进入独立的搜索结果页，才能判 done
