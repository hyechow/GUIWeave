---
id: task.self_learning.manual_pdf.section_system
source_type: task_template
platform: shared
scope:
  - self_learning
owner: gui_agent.core.self_learning.manual_pdf
eval_suites:
version: 1
---
你在把一份软件操作手册的【某个编号小节】解析成可执行的「操作知识」。给你该小节的标题、抽取到的文字,以及这一节版面的截图(含正文与配图)。

请输出这一节的知识:
1. 一句话说明本节要解决的问题(对应标题)。
2. 「操作步骤」:分步写清在哪个页面/菜单、点哪个按钮、填什么、如何确认;保留导航路径(如 系统->用户账号)。
3. 「界面元素与标注」:把配图里出现的关键控件读出来(名称、位置),**尤其是图上的标注(红框/箭头/序号/
   高亮/圈注)指向了哪个控件、说明了什么**——这些纯文字里没有。只描述图里真实存在的;本节无配图就写「无配图」。
用中文,纯 Markdown,不要 YAML frontmatter。