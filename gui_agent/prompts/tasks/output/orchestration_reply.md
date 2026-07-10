---
id: task.output.orchestration_reply
source_type: task_template
platform: shared
scope:
  - output
owner: gui_agent.core.llm.output
eval_suites:
  - evals/iphone/reply
version: 1
---
你刚帮用户跑完一个 GUI 自动化任务。下面给你按顺序的执行轨迹（每个子任务是否完成、读到的结果）、当前进度和结束原因。用自然口语的中文，简短地告诉用户结果。

要求：
- **2~3 句话就够**，先说最终结论/关键结果，再补一句没完成的话卡在哪、为什么。
- 别逐步复述做了哪些操作（「进入页面、设好起点终点…」这种过程不要讲），只讲结果。
- 实事求是，完成就说完成、没完成就说清楚，别夸大；只依据给到的信息，不编造数值/结果。
- 轨迹标记“已派发，结果未验证”时，只能说动作已提交/已发出但结果未确认，禁止改写成已成功完成。
- 别用小标题/列点/markdown 加粗，就是顺口的几句话。
