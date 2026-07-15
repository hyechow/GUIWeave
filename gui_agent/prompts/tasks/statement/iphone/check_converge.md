---
id: context.statement.iphone.check.converge
source_type: context_block
platform: iphone
scope:
  - checker
owner: gui_agent.adapters.iphone.supervisor.statement
eval_suites:
  - evals/iphone/checker
version: 1
---

## ⚠️ 连续调值类子目标（completion_strategy=repeat_until_satisfied，如滚轮/日期/时间 picker、步进器）
- 这类目标靠**多轮拖动逐步逼近目标值**，未到目标是正常的 in_progress，不是失败，不要因"上一轮没到位"判 done 或异常。
- ⚠️ 读「当前值」的**唯一依据是滚轮各列正中间那一行**（选中带/居中高亮行）：picker 的真实当前值永远是滚到正中间的那一行——年列中间行=当前年、月列中间行=当前月、日列中间行=当前日，组合起来即当前值。
- ⚠️ **绝对不要**用页面上的摘要框 / 「已选」/ 绿色高亮文字来读当前值：① 不是所有 picker 都有这种摘要框，中间行才通用；② 即使有，它刷新常滞后于滚轮，刚拖动后会显示旧值。一旦摘要框与滚轮中间行不一致，**一律以滚轮中间行为准**，摘要框的值视为过期、忽略它。
- ⚠️ done 仅当**滚轮各列中间行组合出的值**与 success_condition 的目标值精确一致（不是看摘要框）。一旦相等就**立即判 done**，不要管全局约束里的其他日期/字段（如另设的结束日期）是否完成——那是别的子目标的事。
- **in_progress 时**必须在 missing_evidence 中写出「当前值=<从滚轮中间行读到的值>」「目标值=<success_condition 目标>」
  （例：当前值=4月、目标值=4月7日），这是规划器算拖动步长/方向、以及判断是否在推进的唯一依据。
- ⚠️ **done 时 missing_evidence 必须为空**：已达标就没有缺失项，把当前值/目标值写进去反而会触发「done 证据不足」误重试。done 的依据写在 reason 里即可。
