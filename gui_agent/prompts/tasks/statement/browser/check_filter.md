---
id: context.statement.browser.check.filter
source_type: context_block
platform: browser
scope:
  - checker
owner: gui_agent.adapters.browser.supervisor.statement
eval_suites:
  - evals/browser/checker
version: 1
---

## 筛选/搜索类子目标（kind=filter）
判 done 必须同时满足：
1. 当前是结果页，不是自动补全/建议页、历史页或加载页
2. 搜索框或页面显示完整的目标查询/筛选条件
3. 页面显示与查询对应的结果列表或内容
⚠️ 搜索建议页 vs 结果页：若搜索框仍处于输入/激活状态、下方是自动补全建议（带放大镜/历史图标、无详情元素），即使出现目标词也判 in_progress；只有已提交（回车或点搜索）进入独立结果页才判 done。
⚠️ 表格/Grid 的列筛选、后台列表筛选、站内搜索框等“填写后需提交/应用”的 UI：输入框里出现目标文本不等于筛选已生效。若历史操作记录中本子目标最近只是 type/填入关键词，之后没有 Search/Apply/Filter/Submit/回车提交，则不能把当前列表或当前 `records found` 当成筛选后的结果；判 in_progress，并在 missing_evidence 写明需要提交/应用筛选。Search/Apply/Filter/Submit 等按钮可能是常驻控件，按钮仍可见本身不能证明未应用；只有已经提交，且有列表/计数刷新证据或明确的已应用状态，才可判 done。
⚠️ 若历史操作记录已经显示本子目标执行过 Search/Apply/Filter/Submit 或回车提交，且当前截图中筛选框仍保留目标值并显示结果列表/计数，则不得再把“需要点击 Search/Apply/提交”作为 missing_evidence；应判断当前结果是否就是已提交后的状态。
⚠️ 使用结果计数（如 "X records found"、分页总数）作为验收证据前，必须先确认它是应用目标筛选后的计数：可见结果行应与筛选条件一致，或页面有明确的已应用筛选状态/刷新后的结果。若可见行中仍有任一条不符合筛选条件，通常不能把该计数当作目标计数；但如果应用验收观察规则说明该 grid 的可见标题/截断字段不能作为反证，或说明某种 `筛选框 + records found` 形态就是已刷新状态，则以应用规则为准。若计数已刷新且可见结果均符合条件，即使 Search/Apply 按钮仍常驻可见，也可以判 done。
⚠️ 即使 in_progress，也在 missing_evidence 写出「当前值」与「目标值」，供规划器调整。
