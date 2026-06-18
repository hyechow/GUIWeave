---
id: task.self_learning.knowledge.strict
source_type: task_template
platform: iphone
scope:
  - self_learning
owner: gui_agent.core.self_learning.knowledge
schema: PageKnowledge
eval_suites:
version: 1
---
所有元素均经过实测探测。function 规则：
4. 有导航结果的标注「进入…」，将「实测→」中的具体页面名抽象为通用类型（如实测→「个人中心概览」写为「进入个人中心」）
5. 无导航的描述其交互用途（如「收藏」「分享」）

示例（错误 → 正确）：
- ✗ [Mythos 限] 实测→「…」 → ✓ [文章行]，function：进入文章详情页
- ✗ [张三] 实测→「…」 → ✓ [聊天行]，function：进入该联系人的聊天详情
- ✗ [长安网咖] 实测→「…」 → ✓ [店铺行]，function：进入店铺详情页
