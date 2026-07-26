---
id: task.orchestrator.structured_read
source_type: task_template
platform: shared
scope:
  - data
owner: gui_agent.core.run.structured_read
eval_suites:
version: 2
---
你从截图读取【指定字段】的当前值，用于程序判断。这是通用读法，具体「读什么、怎么判读」以本次的【读取说明】为准。对每个字段：
1. 先在 evidence 里写出你在界面上看到的、与该字段相关的具体信号——包括文字，也包括图标/颜色/形状/位置（例：「起点终点输入框之间右侧有一个绿色圆形对勾✓」）。
2. 再据【读取说明】（任务级，优先）和【界面信号参考】（应用约定，补充）把该信号判读成 value 的文字值。**图标/颜色信号必须判读成文字写进 value，不能因为它不是文字就留空**（如绿色✓→连通、红字「路径不可达」→不可达、灰色?→未检测）。
3. 确实读不到（界面没有该信息）才把 value 留空。
只读被指定的字段，不要补充其他字段、不要编造。
