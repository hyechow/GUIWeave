# 知识库边界

`knowledge/` 只存放应用、站点或部署实例固有的事实。跨任务可复用的执行策略、校验规则和平台机制
属于代码或 prompt，不应写进知识文件。

本目录保存仓库内置知识。用户从文档导入的私有知识默认保存在 macOS 的
`~/Library/Application Support/GUIWeave/knowledge/`，也可通过
`GUIWEAVE_KNOWLEDGE_ROOT` 指定；同名用户知识优先于仓库内置知识。部署 URL、账号和
其他敏感配置不得提交。

## 目录

```text
knowledge/<platform>/<app>/
  ├─ _app.md          导航结构与业务区域
  ├─ _deploy.md       部署入口和访问上下文
  ├─ _update.md       当前版本相对生成知识的事实覆盖
  ├─ _skill.md        可选的业务编排事实
  ├─ _check.md        应用特有的可观察完成标志
  ├─ _elements.md     生成的元素概要
  └─ <功能>.md        可按目标选择的功能章节
```

`_app.md`、`_elements.md` 和普通功能章节可以由 recon 生成；以下划线开头的手工覆盖文件在重新生成
时保留。

## 当前消费路径

`gui_agent/core/self_learning/app_summary.py` 将命中的目录装配为 `AppKnowledge`：

- `navigation`：应用概览及导航事实，提供给 reviewed-Python orchestrator。
- `sections`：按用户目标选择的功能章节。
- `check`：应用特有的可观察完成标志。
- `metadata`：frontmatter 来源、作用域和版本信息。

`gui_agent/core/app_router.py` 在加载知识前统一解析应用身份。它从目标中的规范名称/alias、
当前 URL 或平台应用标识收集确定性证据；发生 alias 冲突时要求澄清，不会选择第一个目录。
Router 支持一次返回多个目标应用，供跨应用任务分别绑定知识。

初始规划调用 `AppKnowledge.orchestrator_context(goal)`。只有 frontmatter 中显式包含
`scope: [orchestrator]` 的普通章节才会参与目标匹配；未命中章节时仅返回 navigation。

Statement 执行期可以通过渐进知识选择器加载功能章节。知识只能帮助理解应用事实，不能替代当前
Observation、Journal receipt 或结构化状态成为完成证据。

## Frontmatter

普通章节示例：

```yaml
---
id: knowledge.browser.example.orders
source_type: knowledge_section
platform: browser
app: example
scope:
  - orchestrator
  - statement
selector_when: 用户需要在订单范围内查询或聚合时
source: manual_distilled
confidence: medium
ttl: session
version: 1
---
```

覆盖层建议：

- `_app.md`：`source_type: knowledge_navigation`
- `_deploy.md`：`source_type: deployment_context`；frontmatter 可用 `aliases` 声明自然语言
  别名、`browser_origins` 声明站点 origin、`android_packages` / `iphone_bundle_ids` 声明
  平台应用标识。正文中的 HTTP(S) 入口也会参与 Browser origin 匹配。
- `_skill.md`：`source_type: knowledge_skill`
- `_check.md`：`source_type: knowledge_check_rules`

加载器会剥离 frontmatter 后再注入正文，同时把 metadata 保存在 `AppKnowledge.metadata` 中。

## 内容边界

- 可写：页面名称、导航路径、业务字段、状态含义、应用特有的显示和数据限制。
- 不可写：WebArena task id、某条运行日志的修复策略、通用重试策略、平台点击机制、core prompt 规则。
- `_deploy.md` 只写环境和访问事实。
- `_update.md` 记录带日期的当前版本事实。
- `_skill.md` 只写业务触发、所需数据和语义步骤，不写坐标、DOM 或截图布局。
- `_check.md` 只写该应用特有且经过验证的完成标志；不得用宽松匹配掩盖错误实体。

相关实现：

- `gui_agent/core/self_learning/app_summary.py`
- `gui_agent/core/self_learning/progressive.py`
- `gui_agent/context/`
