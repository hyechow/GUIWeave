# 知识库架构（knowledge/）

GUI Agent 的**应用知识**：按平台 / 应用分目录，喂给 Supervisor 的任务分解（decompose）、
步骤规划（plan）、修复规划（replan）。core 通过 `gui_agent/core/self_learning/app_summary.py`
的 `auto_discover_knowledge(goal, platform)` 把命中应用的知识装配成 `AppKnowledge`。

> ⚠️ 本目录整体 **gitignore**（含部署 URL/账号等环境信息）；仅本 README 经 `.gitignore`
> 的 `!knowledge/README.md` 例外入库（只写架构，不含任何部署信息）。

## 目录结构

```
knowledge/<platform>/<app>/          platform = iphone | browser | android
  ├─ _app.md          生成·导航结构（页面/菜单层级）
  ├─ _deploy.md       手维·环境/访问（入口 URL、账号、浏览器）
  ├─ _update.md       手维·版本更新覆盖层（现版本相对旧基线的变化，带日期）
  ├─ _skill.md        手维·编排技能（多步任务的 触发/数据/步骤）
  ├─ _check.md        手维·验收观察规则（Checker 专用：界面实际显示形态/完成标志）
  └─ 如何XXX.md ×N    生成·每特性章节（带 `when:` frontmatter，供检索）
```

两类来源：
- **生成层**（`_app` / `如何XXX`）：从像素 recon 蒸馏；可被 re-distill 整体重刷。
- **手维 `_` 覆盖层**（`_deploy` / `_update` / `_skill` / `_check`）：手写，`_` 前缀让它 **survive re-distill**
  （不被重刷清掉），也**不会被当可检索章节**加载（避免重复注入）。

## 装配：三个通道（`AppKnowledge`）

| 通道 | 内容 | 喂给谁 |
|---|---|---|
| `navigation` | `_deploy` + `_update` + `_app` + `_skill` 拼接（**常驻**注入） | decompose / plan / replan |
| `sections` | `如何XXX.md` ×N → `ProgressiveKnowledge` | planner / replan（**按需检索**） |
| `check` | `_check.md` 全文 | **仅 checker**（`run_checker(check_knowledge=)`） |

navigation 的拼接顺序：环境(`_deploy`) → 版本更新(`_update`) → 导航结构(`_app`) → 编排(`_skill`)。

## 渐进加载 + KnowledgeSelector（章节按需注入）

章节文件不全量灌入。每个 `如何XXX.md` 带 `when:` frontmatter（检索描述，桥接同义词）；
规划时 **KnowledgeSelector**（一次文本-only 的 LLM 微决策）按 `(milestone, 当前页面)` 选若干章节 id，
**结果缓存**（只在页面/milestone 变化时重选），命中的章节体经 `_elements_for()` 注入 planner/replan。非 RAG（无 embedding）。

## Metadata frontmatter（试点：browser/shopping_admin）

知识文件可以带轻量 YAML frontmatter，用于适配 `gui_agent/context` 的来源标注与注入路由。
旧字段 `when:` 继续兼容；新字段 `selector_when:` 优先供 KnowledgeSelector 使用。

普通章节建议：

```yaml
---
id: knowledge.browser.shopping_admin.orders
source_type: knowledge_section
platform: browser
app: shopping_admin
scope:
  - planner
  - replanner
selector_when: 订单列表、completed orders、customer email 聚合任务时
when: 订单列表、completed orders、customer email 聚合任务时
source: manual_distilled
confidence: medium
sensitivity: internal
ttl: session
version: 1
---
```

覆盖层建议：

- `_app.md`: `source_type: knowledge_navigation`, `scope: [decompose, planner, replanner]`
- `_deploy.md`: `source_type: deployment_context`, `sensitivity: secret`, 可在 frontmatter 写 `aliases:` 用于知识发现
- `_skill.md`: `source_type: knowledge_skill`, `scope: [decompose, planner, replanner]`
- `_check.md`: `source_type: knowledge_check_rules`, `scope: [checker]`
别名统一写在 `_deploy.md` 的 `aliases:`；不再单独维护 `_aliases.md`，因为它们都属于“如何把用户说法
绑定到这个部署/应用目录”的发现信息。

加载器会剥离 frontmatter 后再把正文注入模型；metadata 保留在 `AppKnowledge.metadata`，章节正文被
`ProgressiveKnowledge.body_blocks()` 渲染成带来源的 `ContextBlock`。

## 消费方路由（谁拿什么）

- **decompose**：只拿 `navigation`（要的是页面/流程结构，不需要像素级元素目录）。
- **planner / replanner**：`navigation` + 渐进章节体（KnowledgeSelector 选中的章节，经 `_elements_for()` 注入）。
- **checker**：静态通用验收规则 + `check`（`_check.md`，唯一动态源；不看 navigation/sections）。
- planner 与 replanner 注入同一份聚焦切片，不全量灌。

## 验收的静态/动态分层（为什么有 `_check.md`）

验收的唯一权威是 **checker**；decomposer 的 success_condition 只是「待验收的文本」。
据此把验收知识分两层：

- **静态层**（checker prompt）：只留**跨 app 的交互习语**——searchable-combobox 的过滤/选定语义、
  即选即生效控件、loading 判定……对任何 web 应用都成立的规则。
- **动态层**（`_check.md`）：**该 app 的显示形态与完成标志**——某列渲染短编号、保存成功提示长什么样、
  哪种 toast 表示操作被拒。按 app 注入，checker 据此把 SC 的字面要求**桥接**到屏幕实际渲染。

边界判据（新事故先问这两个问题）：
1. **交互习语还是显示形态？** 跨 app 为真 → 静态 prompt（走 eval 调优）；单 app 事实 → `_check.md` 加一行。
   把 app 事实写进静态层 = 全局加税 + 过拟合（会误导其他 app）。
2. **渲染差还是指称差？** 同一事实的两种显示（「10」=「s10-站点10」）→ `_check.md` 桥接；
   事实本身错了（SC 写死了系统自增的名字，屏上是另一台）→ **源头修**（配置/skill/decompose），
   绝不能用宽松匹配桥接——会在「多实体中操作指定那个」的任务里制造假成功。

这层分离让 decomposer 的 success_condition 保持**语义层**写法（说清什么事实应当成立），
不必预判 app 的渲染细节，也不再为个案往 decompose prompt 加规则。

## `_` 覆盖层的语义与优先级

- **`_deploy.md` = 环境/访问**：在哪、怎么进（per-instance，redeploy 才变）。**不覆盖任何知识**。
- **`_update.md` = 版本更新覆盖层**：生成层蒸馏自**旧手册**（对它那个版本正确，但产品已更新）；
  本文件登记**现版本实际状态**，每条带核验日期。**与章节冲突时，以本文件和实际界面为准。**
  随 UI 漂移而增长；待 live recon 重蒸馏时，把更新折回基线、基线刷新、本文件清空（re-baseline）。
- **`_skill.md` = 纯编排**：只含 `触发 / 数据 / 步骤`，每步「动作 + 用哪个数据」。
  **不写界面入口/手势/验收标志**（那些归章节与 `_update`）——结构上没有 HOW 字段，加上
  `app_summary.validate_skill_doc()` 的 lint（loader 加载时 warn），防它慢慢长成第二份手册。
- **`_check.md` = 验收观察规则（checker 专用）**：该 app 界面的实际显示形态/完成标志，
  注入 checker prompt 且**冲突时优先于通用规则**。与 `_update` 的分工：影响**怎么操作/怎么分解**
  的事实（入口、必填项、流程、命名规则）归 `_update`；只影响**怎么读屏验收**的事实归 `_check`。

## 手维文件编写约束

- `_deploy.md`：只写环境/访问，别塞 UI 事实（那是 `_update` 的）。
- `_update.md`：每条带日期；写**现版本的实际状态**（正面陈述），别写「手册错了」式勘误口吻；
  写 UI 事实前 **live 核验或问用户**。
- `_skill.md`：照模板 `触发/数据/步骤`；步骤短、无界面操作词；新增 skill 跑一次 lint 不报 warning。
- `_check.md`：它的权威**高于 checker 自己的眼睛**，是把双刃剑——一条过时的「放宽型」规则会制造
  **假成功**（比假失败更糟）。所以：条目须 live 实测背书；宁写「增加满足路径」（列内编号**也**算满足），
  不写「否决证据」；体量守在 1-2k 字符级（全文常注入，膨胀了再考虑按需检索，不预建）。

## 相关代码

- `gui_agent/core/self_learning/app_summary.py` — `auto_discover_knowledge`（装配）、`load_page_files`、
  `validate_skill_doc`（skill lint）。
- `gui_agent/core/self_learning/progressive.py` — `ProgressiveKnowledge`（章节体 / `when:` 解析 / selector manifest）。
- `gui_agent/core/supervisor/milestone/policy.py` — `_select_sections`（Selector 缓存）、`_elements_for`（章节路由）、
  `_do_decompose` / `_invoke_planner` / `_invoke_replanner`（三消费方注入）、`set_app_knowledge(check=)`。
- `gui_agent/core/supervisor/milestone/helpers.py` — `run_checker(check_knowledge=)`（`_check.md` 注入点，
  含 done-重试递归携带）。
