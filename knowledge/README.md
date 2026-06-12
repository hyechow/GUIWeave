# 知识库架构（knowledge/）

GUI Agent 的**应用知识**：按平台 / 应用分目录，喂给 Supervisor 的任务分解（decompose）、
步骤规划（plan）、修复规划（replan）。core 通过 `gui_agent/core/self_learning/app_summary.py`
的 `auto_discover_knowledge(goal, platform)` 把命中应用的知识装配成 `AppKnowledge`。

> ⚠️ 本目录整体 **gitignore**（含部署 URL/账号等环境信息）。这份 README 也不入库，仅作本地说明。

## 目录结构

```
knowledge/<platform>/<app>/          platform = iphone | browser | android
  ├─ _app.md          生成·导航结构（页面/菜单层级）
  ├─ _deploy.md       手维·环境/访问（入口 URL、账号、浏览器）
  ├─ _update.md       手维·版本更新覆盖层（现版本相对旧基线的变化，带日期）
  ├─ _skill.md        手维·编排技能（多步任务的 触发/数据/步骤）
  └─ 如何XXX.md ×N    生成·每特性章节（带 `when:` frontmatter，供检索）
```

两类来源：
- **生成层**（`_app` / `如何XXX`）：从像素 recon 蒸馏；可被 re-distill 整体重刷。
- **手维 `_` 覆盖层**（`_deploy` / `_update` / `_skill`）：手写，`_` 前缀让它 **survive re-distill**
  （不被重刷清掉），也**不会被当可检索章节**加载（避免重复注入）。

## 装配：两个主通道（`AppKnowledge`）

| 通道 | 内容 | 喂给谁 |
|---|---|---|
| `navigation` | `_deploy` + `_update` + `_app` + `_skill` 拼接（**常驻**注入） | decompose / plan / replan |
| `sections` | `如何XXX.md` ×N → `ProgressiveKnowledge` | planner / replan（**按需检索**） |

navigation 的拼接顺序：环境(`_deploy`) → 版本更新(`_update`) → 导航结构(`_app`) → 编排(`_skill`)。

## 渐进加载 + KnowledgeSelector（章节按需注入）

章节文件不全量灌入。每个 `如何XXX.md` 带 `when:` frontmatter（检索描述，桥接同义词）；
规划时 **KnowledgeSelector**（一次文本-only 的 LLM 微决策）按 `(milestone, 当前页面)` 选若干章节 id，
**结果缓存**（只在页面/milestone 变化时重选），命中的章节体经 `_elements_for()` 注入 planner/replan。非 RAG（无 embedding）。

## 消费方路由（谁拿什么）

- **decompose**：只拿 `navigation`（要的是页面/流程结构，不需要像素级元素目录）。
- **planner / replanner**：`navigation` + 渐进章节体（KnowledgeSelector 选中的章节，经 `_elements_for()` 注入）。
- planner 与 replanner 注入同一份聚焦切片，不全量灌。

## `_` 覆盖层的语义与优先级

- **`_deploy.md` = 环境/访问**：在哪、怎么进（per-instance，redeploy 才变）。**不覆盖任何知识**。
- **`_update.md` = 版本更新覆盖层**：生成层蒸馏自**旧手册**（对它那个版本正确，但产品已更新）；
  本文件登记**现版本实际状态**，每条带核验日期。**与章节冲突时，以本文件和实际界面为准。**
  随 UI 漂移而增长；待 live recon 重蒸馏时，把更新折回基线、基线刷新、本文件清空（re-baseline）。
- **`_skill.md` = 纯编排**：只含 `触发 / 数据 / 步骤`，每步「动作 + 用哪个数据」。
  **不写界面入口/手势/验收标志**（那些归章节与 `_update`）——结构上没有 HOW 字段，加上
  `app_summary.validate_skill_doc()` 的 lint（loader 加载时 warn），防它慢慢长成第二份手册。

## 手维文件编写约束

- `_deploy.md`：只写环境/访问，别塞 UI 事实（那是 `_update` 的）。
- `_update.md`：每条带日期；写**现版本的实际状态**（正面陈述），别写「手册错了」式勘误口吻；
  写 UI 事实前 **live 核验或问用户**。
- `_skill.md`：照模板 `触发/数据/步骤`；步骤短、无界面操作词；新增 skill 跑一次 lint 不报 warning。

## 相关代码

- `gui_agent/core/self_learning/app_summary.py` — `auto_discover_knowledge`（装配）、`load_page_files`、
  `validate_skill_doc`（skill lint）。
- `gui_agent/core/self_learning/progressive.py` — `ProgressiveKnowledge`（章节体 / `when:` 解析 / selector manifest）。
- `gui_agent/core/supervisor/milestone/policy.py` — `_select_sections`（Selector 缓存）、`_elements_for`（章节路由）、
  `_do_decompose` / `_invoke_planner` / `_invoke_replanner`（三消费方注入）。
