# Tool Agent MobileWorld GUI-only 攻关顺序

更新日期：2026-08-18

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
29. `MastodonUnfollowTask`
30. `MastodonPinTootsTask`
31. `MastodonReportTask`
32. `MastodonManageHashtagsTask`
33. `MastodonAddFeaturedHashtagsTask`
34. `MastodonFilterLanguageTask`
35. `MastodonChangeLanguageTask`
36. `MastodonGetServerInfoTask`
37. `MastodonOpenAutomatedDeletionTask`
38. `MastodonImportMutedUsersTask`
39. `MastodonExportFollowsTask`
40. `MastodonAdjustTootsTask`
41. `MastodonRevisePollTask`
42. `MastodonRevisePhotoAltTask`
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

状态约定：`⬜` 未开始、`🟡` 正在回放修复、`✅` 已通过。正式计入通过数时，必须同时
记录成功 run 目录和对应代码提交。
