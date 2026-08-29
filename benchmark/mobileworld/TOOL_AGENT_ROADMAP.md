# Tool Agent MobileWorld GUI-only 攻关顺序

更新日期：2026-08-29

## 当前基线

MobileWorld 当前提供 117 个 GUI-only 任务。严格按以下条件从本地 live
结果按任务类去重：

- `orchestrator.kind == "tool_agent"`
- MobileWorld `score == 1.0`
- Tool Agent `outcome.phase == "completed"`

当前通过 21 个，剩余 96 个。已经通过的任务是：

1. `OpenFlightModeTask`
2. `CloseFlightModeTask`
3. `AdjustBrightnessMaximumTask`
4. `SetAlarmTask`
5. `ScheduleCoffeeTimeViaSmsTask`
6. `ScheduleLunchViaSmsTask`
7. `ChromeSearchBeijingWeatherTask`
8. `SumFileLinesTask`
9. `MastodonConditionalFavoTask`
10. `MattermostCreateChannelTask`
11. `CartManagementTask`
12. `CheckCartPriceTask`
13. `RecentTotalExpenseTask`
14. `ReviewPaperEmailTask`

这 14 个任务是回归基线，不进入下面的待攻关编号。当前覆盖 11 个单应用任务、
3 个双应用任务，尚未通过三应用任务。

## 攻关门禁

每个任务遵循同一流程，禁止靠连续 live 碰运气：

1. **准备回放**：先运行与目标能力相关的既有 Master/Worker 决策回放。尚无该
   任务轨迹时，只允许一次 seed live 来生成真实轨迹。
2. **失败分层**：把失败归入感知、Master 分解、Worker 决策、动作执行、完成判定、
   Runtime 数据流或 MobileWorld 评分，不跨层堆补丁。
3. **回放修复**：对失败帧回放模型决策；回放未通过前不重复 live。
4. **泛化审查**：核心 prompt 和 Runtime 不允许出现任务名、应用名、页面名或按钮名。
   可复用机制进入代码；应用固有事实只能进入对应 `knowledge/android/<App>/`。
5. **live 确认**：相关回放和单元测试通过后，运行一次 headless live。通过标准是
   `score == 1.0` 且 Tool Agent 自身 `phase == completed`。
6. **回归与收口**：每完成一个小批次，运行受影响的既有回放；每完成一个阶段，
   对代表性 live 基线回归。删除被新抽象替代的 case-by-case 分支。

推荐的跨阶段 live 回归集：

- 简单状态变更：`OpenFlightModeTask`
- 单例数据读取：`ChromeSearchBeijingWeatherTask`
- 跨应用写入：`ScheduleLunchViaSmsTask`
- 多对象写入：`MattermostCreateChannelTask`
- 长链路聚合：`SumFileLinesTask`

## 阶段一：相邻能力探针（1–12）

目标：用最接近现有成功能力的任务快速检查泛化性。这个阶段不应引入新的任务级
状态机；若相邻任务仍需要大量新代码，应先停下来审查抽象边界。

1. `AdjustBrightnessMinimumTask`
2. `AdjustFontIconMaximumTask`
3. `AdjustFontIconMinimumTask`
4. `CountFileLinesTask`
5. `BidFileRenameTask`
6. `CheckConferenceDurationTask`
7. `CheckPuchasedItem`
8. `ItemCheckoutTask`
9. `SearchItemAndCheckoutTask`
10. `AcceptMeetingTask`
11. `MastodonFavoriteTootsTask`
12. `MattermostReplyToMessageTask`

阶段退出条件：Settings、Files、Calendar、Taodian、Mail、Mastodon 和 Mattermost
的基础查找、读取、编辑、提交、列表遍历均有成功样本；现有 5 个代表性回归任务
没有决策回归。

## 阶段二：补齐单应用深度（13–44）

目标：在引入跨应用数据流前，补齐单应用内的搜索、筛选、分页、批量选择、附件、
时间语义和持久化边界。Camera、Maps 是当前尚未覆盖的应用，应优先依赖通用 Android
结构化感知；只有导航路径等应用固有事实可以进入 knowledge。

13. `CheckDeduplicatedEventsTask`
14. `InvoiceReceiptCopyTask`
15. `InvoiceReceiptCopyAskUserTask`
16. `CancelMeetingTask`
17. `CheckRegistrationTask`
18. `DownloadSendReceiptTask`
19. `SendFormsTask`
20. `SendWaiverTask`
21. `GoogleMapsAlibabaSouthNeighborTask`
22. `TakeSelfieTask`
23. `MastodonAddBookmarkTask`
24. `MastodonRemoveBookmarkTask`
25. `MastodonReplyTask`
26. `MastodonNewPostTask`
27. `MastodonCreateListTask`
28. `MastodonManageMultiListTask`
29. `MastodonUnfollowTask` — ✅ 通过：`20260828_232406`，official eval 1.0
30. `MastodonPinTootsTask` — ✅ 通过：`20260828_033239`，official eval 1.0
31. `MastodonReportTask` — ✅ 通过：`20260829_024321`，official eval 1.0
32. `MastodonManageHashtagsTask` — ✅ 通过：`20260822_221712`，official eval 1.0
33. `MastodonAddFeaturedHashtagsTask` — ⛔ benchmark 定义阻塞：原生 Android 2.11.1
    只读取和展示 featured hashtags，编辑资料时还会禁用 `Featured` 标签；任务未声明
    Chrome，Web/API 路径暂不计正式通过
34. `MastodonFilterLanguageTask` — ✅ 通过：`20260829_045355`，official eval 1.0
35. `MastodonChangeLanguageTask` — ⛔ benchmark 定义阻塞：原生设置只改变发帖默认语言；
    Chrome Web 可改变 evaluator 字段，但任务未声明 Chrome，暂不计正式通过
36. `MastodonGetServerInfoTask` — ✅ 通过：`20260829_072056`，official eval 1.0
37. `MastodonOpenAutomatedDeletionTask` — ✅ 通过：`20260829_075631`，official eval 1.0
38. `MastodonImportMutedUsersTask` — ✅ 通过：`20260829_082253`，official eval 1.0
39. `MastodonExportFollowsTask` — ✅ 通过：`20260829_005757`，official eval 1.0
40. `MastodonAdjustTootsTask`
41. `MastodonRevisePollTask`
42. `MastodonRevisePhotoAltTask` — ✅ 通过：`20260828_134532`，official eval 1.0
43. `MattermostSendFileTask`
44. `MattermostBudgetApprovalPipelineTask`

阶段退出条件：44 个剩余单应用任务全部有一次正式通过；空集合、多页集合、条件选择、
多选、write-through 控件和显式提交边界均由通用机制处理。

## 阶段三：双应用数据流（45–89）

目标：验证 typed ref、跨 Worker 数据交接、冻结时间、文件/附件传递以及读后写入。
一个 Worker 只负责一个内聚 GUI 子目标；跨应用值通过 Runtime 数据流传递，不能依赖
下一 Worker 猜测上一界面的视觉记忆。

### 已覆盖应用组合的近邻任务

45. `CheckConferenceAndSendSmsTask1`
46. `CheckConferenceAndSendSmsTask2`
47. `CVEmailTask`
48. `LocalFileManagementTask2`
49. `SendInterviewEmailTask`
50. `CheckInterviewTimesTask`
51. `CheckSetMeetTimeTask`
52. `CheckEventTimeTask`
53. `CheckDepartTimeTask`
54. `RequestCarpoolingTask`
55. `SMSManagement`
56. `SendInterviewInvitationTask`
57. `CartInfoNotificationTask`
58. `CheckGithubInfoTask`
59. `LocalFileManagementTask`
60. `MattermostEmailTask`
61. `MattermostProjectHandoverTask`
62. `MattermostReadingGroupTask`

### 新应用与文档、媒体、地图组合

63. `ChangeWallpaperTask`
64. `GoogleMapsAlibabaPhoneContactTask`
65. `CheckConferenceLocationTask`
66. `TextArrivalTimeTask`
67. `CheckInvoiceTask1`
68. `ReadQwen3PaperTask1`
69. `ReadQwen3PaperTask2`
70. `ReadQwen3PaperTask3`
71. `ReadQwen3PaperTask4`
72. `ReadQwen3PaperTask5`
73. `SharePhotosTask`
74. `PhotoManagementTask`

### Mastodon 跨应用组合

75. `MastodonCalendarMultiMemosTask`
76. `MastodonCreateMemoTask`
77. `MastodonFollowTask`
78. `MastodonUpdateContactsTask`
79. `MastodonInviteTask`
80. `MastodonMultiInviteTask`
81. `MastodonMallPurchaseCommodityTask`
82. `MastodonMallShareOrderTask`
83. `MastodonMattermostPostNoticeTask`
84. `MastodonNewFilterTask`
85. `MastodonChangeHeaderTask`
86. `MastodonPostEditedPhotoTask`
87. `MastodonSavePhotosTask`
88. `MastodonPostPollTask`
89. `MastodonServerInfoReportTask`

阶段退出条件：45 个双应用任务全部正式通过；数据交接不使用自然语言猜值；文件、时间、
联系人、地图位置和集合结果都有明确 schema；重试不会重复发送、重复创建或重复购买。

## 阶段四：三应用长链路（90–103）

目标：最后验证多源读取、条件分支、长链路 mutation 和预算控制。先做已有文件/邮件
能力的组合，再做 Mattermost 复杂工作流。

90. `CheckInvoiceTask2`
91. `CheckInvoiceTask3`
92. `SuggestPaperTask`
93. `GraduationMassEmailTask`
94. `ThanksgivingPrepTask`
95. `MastodonShareLocationTask`
96. `MattermostTechnicalDebtTriageTask`
97. `MattermostVisualInstructionResponseTask`
98. `MattermostCustomerFeedbackAnalysisTask`
99. `MattermostDeadlineReconciliationTask`
100. `MattermostIncidentEscalationTask`
101. `MattermostProjectStatusReportTask`
102. `MattermostResourceConflictResolutionTask`
103. `MattermostShiftCoverageTask`

阶段退出条件：14 个三应用任务全部正式通过；Master 能稳定选择内聚 Worker 边界，
Worker 能在 50 turns 总预算内完成，失败恢复不会重复产生外部副作用。

## 小批次节奏

每次只推进 3–5 个共享能力的任务：

1. 选一个最小任务作为机制探针。
2. 选一个同机制但不同应用或不同页面结构的任务验证泛化。
3. 选一个包含条件、集合或提交边界的任务验证边界。
4. 回放通过后集中跑 live，不在失败时立即重复跑。
5. 小批次结束后 review production diff；测试和 replay 可以增长，核心 Runtime/prompt
   的增长必须能对应一个清晰、可复用的不变量。

## 进度记录模板

每个任务完成后在本节追加一行，避免仅凭聊天记录判断状态：

| 顺序 | 任务 | 首次轨迹 | 主要卡点层 | replay | live score | 提交 |
| ---: | --- | --- | --- | --- | ---: | --- |
| 1 | `AdjustBrightnessMinimumTask` | 20260817_210823 | —（一次通过） | ✅ | 1.0 | —（HEAD 无改动） |
| 2 | `AdjustFontIconMaximumTask` | 20260817_212934 | —（一次通过） | ✅ | 1.0 | —（HEAD 无改动） |
| 3 | `AdjustFontIconMinimumTask` | 20260817_213611 | —（一次通过） | ✅ | 1.0 | —（HEAD 无改动） |
| 4 | `CountFileLinesTask` | 20260817_214301（失败）→ 20260818_083016（通过） | 感知（1900 年份）· Runtime 数据流（空态假冲突）· Master 编译（transform 围栏） | ✅ | 1.0 | 07bf16c6 |
| 5 | `BidFileRenameTask` | 20260818_194441 | 验证器（36 期望）· 通配符 filter 丢失 · R2③ 定位 · 熔断跨元素 | ✅ | 1.0 | 6bf02cd3 |
| 6 | `CheckConferenceDurationTask` | 20260818_215203 | 感知（月视图零提取）· 编译（DATE_SCOPE/try/while）· rows 兜底 | ✅ | 1.0 | 56efd57b |
| 7 | `CheckPuchasedItem` | 20260818_221125 | —（一次通过） | ✅ | 1.0 | —（HEAD 无改动） |
| 29 | `MastodonUnfollowTask` | 20260828_142050（终态后继续操作）→ 20260828_232406（通过） | 冻结首次完整 Following 列表的语义顺序；State 将连续事实更新与下一目标/完成结论原子化，终态帧不再进入 Actor | ✅ | 1.0 | `9cbcf20d` |
| 30 | `MastodonPinTootsTask` | 20260821_131116（官方已成功但 Agent 跑满 50 turns）→ 20260828_033239（通过） | 在完整 profile 时间线中确定最早帖子；`Pin on profile` 为写穿提交，目标菜单闭合且无错误后由 State 终止，不再重复打开菜单 | ✅ | 1.0 | `f685c9d8` |
| 31 | `MastodonReportTask` | 20260829_020152（引号被规范化）→ 20260829_024321（通过） | UIAutomator 保留已跟踪界面的原始 Unicode 文本，执行输入时按宽松键唯一恢复；同时修正 State 当前目标授权和 Mastodon 原生举报/拉黑路径 | ✅ | 1.0 | `4d67cc39`, `a94e4a5a`, `fd4b7a49` |
| 32 | `MastodonManageHashtagsTask` | 20260821_132841（遗留 `dogs`）→ 20260822_221712（通过） | 完整遍历已关注 hashtag 集合，仅对动物相关项执行 unfollow，终态同时确认 `cats` 和 `dogs` 均已移除 | ✅ | 1.0 | —（历史 live，无新代码） |
| 33 | `MastodonAddFeaturedHashtagsTask` | 20260821_133216 | benchmark 定义：仅声明 Mastodon，但原生 Android 2.11.1 只有 featured hashtags 的只读展示，无新增/删除入口；`Edit profile` 会禁用 `Featured` 标签 | ⛔ | 0.0 | — |
| 34 | `MastodonFilterLanguageTask` | 20260829_040420（密集相邻行点击下移）→ 20260829_045355（通过） | 官方路径为已登录 Mastodon Web `Preferences` → `Other`；记录语言原生标签/顺序，并用无候选标记的完整屏幕 Grounding 将密集行点击稳定在目标文字中心 | ✅ | 1.0 | `bf8914dd`, `5cff9c38` |
| 35 | `MastodonChangeLanguageTask` | 20260828_103118（原生 UI）→ 20260828_123131（Chrome Web workaround） | benchmark 定义：仅声明 Mastodon，但原生 `Posting language` 不改变 evaluator 检查的账号 locale；Chrome Web 路径不计正式通过 | ⛔ | 0.0（原生）；1.0（workaround，不计） | — |
| 36 | `MastodonGetServerInfoTask` | 20260829_051512（误认 Web `@test`）→ 20260829_053846（State 协议失败）→ 20260829_061126（合并 native/Web 会话）→ 20260829_070401（空白 tab 循环）→ 20260829_072056（通过） | 官方路径为 native 长按 `Profile` 切换 `@owner`，再独立登录 Chrome Web owner；从 `Administration` → `Dashboard` → `Space usage` 的 `PostgreSQL` 行读取动态 MB 值，并由 owner 原样发布。接口知识分离两个会话事实；State 强制工具调用和有界 `surface` 避免协议失败 | ✅ | 1.0 | `b6e7ab71`, `a57bbf80` |
| 37 | `MastodonOpenAutomatedDeletionTask` | 20260821_140018（误入原生 `Behavior` 循环）→ 20260829_075631（通过） | 官方路径为已登录 `@test` 的 Mastodon Web 顶层 `Automated post deletion`；启用后设置 `1 week`，仅保留 pinned，关闭其余五个布尔例外，favorite/boost 均填 20，再由 `Save changes` 一次提交。接口知识将 Web-only 路径和精确例外集合编入 Master 合约 | ✅ | 1.0 | `cd609ce2` |
| 38 | `MastodonImportMutedUsersTask` | 20260821_141036（误入原生 `Privacy and reach` / `Filters` 循环）→ 20260829_082253（通过） | 官方路径为已登录 Mastodon Web `Import and export` → `Import`；选择 `Muting list`、Downloads 中的 CSV 和 `Merge`，依次执行 `Upload`、复核、`Confirm`。`Recent imports` 是静态表格，处理中需重载 Chrome 页面，不能点击非交互行假刷新 | ✅ | 1.0 | `0a672537` |
| 39 | `MastodonExportFollowsTask` | 20260828_045025（原生路径失败）→ 20260829_005757（通过） | 官方标准路径为 Chrome 已登录 Web 导出 → Files 改名；修复无身份的框外 Grounding 覆盖正确视觉点，并对 State 当前帧 identity 做无模型有界化 | ✅ | 1.0 | `6d7764bb` |
| 42 | `MastodonRevisePhotoAltTask` | 20260821_145832、20260828_070746（失败）→ 20260828_134532（通过） | Mastodon 原生编辑路径知识：帖子三点菜单 → `Edit post` → 附件小编辑按钮 → `Add alt text`；避开只读 ALT 弹层和全局发帖铅笔 | ✅ | 1.0 | —（知识修复待提交） |

状态约定：`⬜` 未开始、`🟡` 正在回放修复、`⛔` benchmark 定义阻塞、`✅` 已通过。
正式计入通过数时，必须同时记录成功 run 目录和对应代码提交。
