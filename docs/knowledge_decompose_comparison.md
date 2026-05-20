# Knowledge-based Decomposition Comparison

Test date: 2026-05-20
Script: `scripts/test_knowledge_decompose.py`
Knowledge source: `knowledge/微信/_app.md` (1876 chars)

## Test Method

对 8 个不同复杂度的微信 goal，分别调用 milestone supervisor 的 `_do_decompose`，对比注入 `_app.md` 前后的里程碑分解结果。使用同一张截图作为 observation。

## Results

### 1. 在微信里发消息给张三 (简单)

| | WITHOUT | WITH |
|---|---|---|
| task_type | action | action |
| milestones | 3 | 4 |

WITHOUT:
- [1] 进入通讯录查找联系人 (navigation/visible_once)
- [2] 搜索并定位张三 (filter/read_once) depends=[1]
- [3] 打开与张三的聊天窗口 (action/visible_once) depends=[2]

WITH:
- [1] 打开微信并进入聊天列表 (navigation/visible_once)
- [2] 搜索联系人张三 (filter/read_once) depends=[1]
- [3] 进入与张三的聊天界面 (action/visible_once) depends=[2]
- [4] 发送消息给张三 (action/repeat_until_satisfied) depends=[3]

**差异**: 有知识时多了"打开微信"和"发送消息"两步，路径更完整。

---

### 2. 查看微信朋友圈 (简单)

| | WITHOUT | WITH |
|---|---|---|
| task_type | action | action |
| milestones | 2 | 2 |

WITHOUT:
- [1] 进入发现页 → 验收: 屏幕显示「发现」页面的标题和内容
- [2] 打开朋友圈 → 验收: 屏幕显示朋友圈内容流

WITH:
- [1] 切换到发现页面 → 验收: 屏幕显示「发现」页面的标题及功能入口（如朋友圈、摇一摇等）
- [2] 进入朋友圈 → 验收: 屏幕显示朋友圈内容流，且顶部标题为「朋友圈」

**差异**: 结构相同，有知识时验收条件更精确（引用了具体入口名称）。

---

### 3. 在微信通讯录里找到一个联系人并发送好友申请 (中等)

| | WITHOUT | WITH |
|---|---|---|
| task_type | action | action |
| milestones | 4 | 5 |

WITHOUT:
- [1] 进入通讯录页面 (navigation/visible_once)
- [2] 查找目标联系人 (collection/scroll_until_boundary) depends=[1]
- [3] 打开联系人详情页 (action/visible_once) depends=[2]
- [4] 发送好友申请 (action/visible_once) depends=[3]

WITH:
- [1] 进入通讯录页面 (navigation/visible_once)
- [2] 查找目标联系人 (collection/scroll_until_boundary) depends=[1]
- [3] 打开联系人详情页 (action/visible_once) depends=[2]
- [4] 发起好友申请 (action/visible_once) depends=[3]
- [5] 填写验证信息并提交 (action/repeat_until_satisfied) depends=[4]

**差异**: 有知识时把"发送申请"拆成两步（发起+填写验证），操作更细。

---

### 4. 在微信搜索一篇关于AI的公众号文章并收藏 (中等)

| | WITHOUT | WITH |
|---|---|---|
| task_type | action | action |
| milestones | 5 | 6 |

WITHOUT:
- [1] 进入微信搜索界面 (action/visible_once)
- [2] 输入搜索关键词 (filter/read_once) depends=[1]
- [3] 执行搜索并筛选公众号文章 (collection/scroll_until_boundary) depends=[2]
- [4] 选择目标文章 (action/visible_once) depends=[3]
- [5] 执行收藏操作 (action/visible_once) depends=[4]

WITH:
- [1] 打开微信应用 (navigation/visible_once)
- [2] 进入搜索页面 (action/visible_once) depends=[1]
- [3] 输入搜索关键词 (filter/read_once) depends=[2]
- [4] 提交搜索请求 (action/visible_once) depends=[3]
- [5] 筛选并定位公众号文章 (collection/scroll_until_boundary) depends=[4]
- [6] 执行收藏操作 (action/visible_once) depends=[5]

**差异**: 有知识时把搜索拆成"进入搜索页→输入→提交"三步，更贴合实际操作流。

---

### 5. 在微信设置里关闭朋友圈入口，然后回到聊天列表 (复杂)

| | WITHOUT | WITH |
|---|---|---|
| task_type | action | action |
| milestones | 3 | 4 |

WITHOUT:
- [1] 进入设置页面 (navigation/visible_once)
- [2] 关闭朋友圈入口 (action/visible_once) depends=[1]
- [3] 返回聊天列表 (navigation/visible_once) depends=[2]

WITH:
- [1] 进入个人中心 (navigation/visible_once)
- [2] 进入应用设置列表 (navigation/visible_once) depends=[1]
- [3] 关闭朋友圈入口 (action/repeat_until_satisfied) depends=[2]
- [4] 返回聊天列表 (navigation/visible_once) depends=[3]

**差异**: 无知识时直接跳到"设置页面"（跳步），有知识时知道真实路径是"个人中心→设置"，导航更准确。

---

### 6. 查看微信群里所有未读消息，统计有多少条@我的 (复杂)

| | WITHOUT | WITH |
|---|---|---|
| task_type | analysis | analysis |
| milestones | 4 | 5 |

WITHOUT:
- [1] 进入微信聊天列表 (navigation/visible_once)
- [2] 筛选并收集群聊未读消息 (collection/scroll_until_boundary) depends=[1]
- [3] 分析未读消息中的@提及 (action/read_once) depends=[2]
- [4] 统计@我的数量 (action/read_once) depends=[3]

WITH:
- [1] 进入微信聊天列表 (navigation/visible_once)
- [2] 收集所有未读会话列表 (collection/scroll_until_boundary) depends=[1]
- [3] 检查群聊中的@我消息 (action/repeat_until_satisfied) depends=[2]
- [4] 检查单聊中的@我消息 (action/repeat_until_satisfied) depends=[2]
- [5] 汇总统计结果 (verification/read_once) depends=[3, 4]

**差异**: 有知识时把群聊/单聊检查拆成并行分支，结构更合理。

---

### 7. 在微信个人中心里修改头像 (复杂)

| | WITHOUT | WITH |
|---|---|---|
| task_type | action | action |
| milestones | 4 | 4 |

WITHOUT:
- [1] 进入个人中心页面 (navigation/visible_once)
- [2] 点击头像区域 (action/visible_once) depends=[1]
- [3] 选择新头像来源 (action/visible_once) depends=[2]
- [4] 确认并保存新头像 (action/visible_once) depends=[3]

WITH:
- [1] 进入个人中心 (navigation/visible_once) → 验收引用了"detail"类型
- [2] 打开头像编辑页 (action/visible_once) depends=[1]
- [3] 选择新头像图片 (action/visible_once) depends=[2] → 验收引用了"modal"类型
- [4] 确认并保存新头像 (action/repeat_until_satisfied) depends=[3]

**差异**: 验收条件引用了知识库中的页面类型（detail/modal），step 4 策略从 visible_once 变为 repeat_until_satisfied。

---

### 8. 在微信游戏里找一个游戏并分享给好友 (跨模块)

| | WITHOUT | WITH |
|---|---|---|
| task_type | action | action |
| milestones | 5 | 4 |

WITHOUT:
- [1] 进入发现页 (navigation/visible_once)
- [2] 进入小游戏入口 (navigation/visible_once) depends=[1]
- [3] 选择目标游戏 (action/visible_once) depends=[2]
- [4] 触发分享操作 (action/visible_once) depends=[3]
- [5] 选择好友并发送 (action/visible_once) depends=[4]

WITH:
- [1] navigate_to_discover_tab (navigation/visible_once)
- [2] enter_game_center (navigation/visible_once) depends=[1]
- [3] select_game (action/visible_once) depends=[2]
- [4] share_game (action/visible_once) depends=[3]

**差异**: 有知识时合并了"触发分享+选择好友"为一步，更简洁。里程碑 ID 使用英文语义化命名。

---

## Summary

| 维度 | 无知识 | 有知识 |
|------|--------|--------|
| 页面名称 | 泛化（"设置页面"） | 精确（"个人中心概览"、"应用设置列表"） |
| 导航路径 | 可能跳步或编造中间页 | 按知识库层级逐步展开 |
| 验收条件 | 通用描述 | 引用真实 UI 元素和页面类型 |
| 复杂任务 | 串行堆叠 | 有时能拆出并行分支 |
| 步骤粒度 | 偏粗或偏细 | 更贴合实际操作 |
