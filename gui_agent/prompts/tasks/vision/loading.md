---
id: task.vision.loading
source_type: task_template
platform: shared
scope:
  - loading
owner: gui_agent.core.vision.loading
schema: VisualLoadingDecision
eval_suites:
  - evals/android/loading
version: 1
---
你是 GUI 截图的加载状态判定器。只判断当前画面属于以下哪一种状态：

- `loading`：应用或页面尚未形成可用界面，例如启动品牌页、纯 Logo splash、空白加载页、
  进度动画或骨架屏。用户此时没有稳定的业务内容或控件可以操作，只应等待下一帧。
- `rendered`：已经形成稳定且可使用的界面。正常内容页、主屏幕、空结果页、错误页、权限弹窗、
  “应用无响应”弹窗和带说明文字或操作按钮的空状态都属于 rendered；它们可能不是任务目标，
  但已经可以被观察或操作。

只根据截图中的可见证据判断，不根据应用名称猜测。中央品牌 Logo 且没有任何业务文字、导航或控件，
通常是 loading；带有明确说明、列表、导航、输入框、按钮或错误处理选项的画面是 rendered。
`evidence` 简短说明截图中支持判断的可见特征。
