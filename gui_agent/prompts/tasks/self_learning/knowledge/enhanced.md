---
id: task.self_learning.knowledge.enhanced
source_type: task_template
platform: iphone
scope:
  - self_learning
owner: gui_agent.core.self_learning.knowledge
schema: PageKnowledge
eval_suites:
version: 1
---
元素数据分为两类：
- **已探测**（带「实测→」标记）：经过真实点击，有导航结果，可信度高
- **未探测**（带「未探测（视觉检测）」标记）：仅通过截图识别，无实测导航数据

4. **function 按数据来源区分**：
  - 已探测元素：标注「进入…」，将具体页面名抽象为通用类型（如实测→「个人中心概览」写为「进入个人中心」）
  - 未探测元素：根据元素类型推测交互用途（如「进入搜索页面」「查看活动详情」）

示例（错误 → 正确）：
- ✗ [Mythos 限] 实测→「…」 → ✓ [文章行]，function：进入文章详情页
- ✗ [长安网咖] 实测→「…」 → ✓ [店铺行]，function：进入店铺详情页
- ✗ [入口图标] 未探测 → ✓ [功能入口]，function：进入对应功能页面
