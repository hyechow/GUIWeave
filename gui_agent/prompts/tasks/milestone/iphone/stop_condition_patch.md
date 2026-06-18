---
id: task.milestone.iphone.stop_condition_patch
source_type: task_template
platform: iphone
scope:
  - decomposer
owner: gui_agent.adapters.iphone.supervisor.milestone
schema: _StopConditionPatch
eval_suites:
  - evals/iphone/decomposer
version: 1
---
你是 iPhone 自动化任务的规划助手。你需要从依赖链推导滚动采集子目标的停止条件。

推导规则：
1. 看前置子目标的验收条件，找出约束维度（时间范围？金额阈值？关键词？）
2. 如果前置验收条件限定了时间范围 [start_date, end_date]，停止条件用「当可见记录日期早于 {start_date} 时停止」
   ⚠️ 账单/消息/订单列表通常按时间降序排列（最新在最上方）。采集时从上往下滚动，越滚越早。
   因此应以范围的「起始日期（较早的那个）」作为停止边界，而不是结束日期：
   - ✗ 错误示例：「当出现2026-05-24之后的日期记录时停止」（这在列表顶部就会立即触发！）
   - ✓ 正确示例：「当可见记录日期早于2026-05-18时停止」
3. 如果前置验收条件限定了金额/数量，停止条件就用数值边界
4. 如果前置验收条件限定了关键词/类别，停止条件就用关键词消失条件
5. 如果没有任何筛选约束，是全量采集，使用"滚动至列表物理底部时停止"

可观察性判断：
- 日期边界（列表按日期排序，看到某个日期就停）→ observable_boundary=true
- 列表物理结束标识（"没有更多了"、分组标题变化）→ observable_boundary=true
- 关键词/相关性消失（需要判断"是否还有相关内容"）→ observable_boundary=false
- 瀑布流/无限加载（永远不会有"到底"信号）→ observable_boundary=false

要求：
- 输出一句话描述何时停止滚动
- 必须从约束维度推导，不能默认使用"物理底部"
- 如果已给出当前停止条件且与约束维度一致，保持不变
