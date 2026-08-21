# Tool Agent MobileWorld 阶段二失败分析

分析日期：2026-08-21

## 结论

阶段二 32 个任务中，严格通过 5 个，失败 27 个，失败率 84.4%。严格通过要求同时
满足：

- MobileWorld 官方 `score == 1.0`
- Tool Agent `outcome.phase == "completed"`

官方状态已经成功但 Tool Agent 最终未完成的任务仍计为失败。它们说明 Agent 具备
完成动作的能力，但完成判定和停机协议不可靠。

失败的首要根因分布：

| 首要根因 | case 数 | 占 27 个失败 |
| --- | ---: | ---: |
| 停滞检测与恢复失效 | 10 | 37.0% |
| Runtime 状态/协议完整性 | 9 | 33.3% |
| 完成与终止判定错误 | 4 | 14.8% |
| 规划语义契约错误 | 3 | 11.1% |
| 模型服务异常缺少恢复 | 1 | 3.7% |

核心问题是架构可靠性不足。应用知识存在缺口，但它不是多数任务的直接致命原因。

## 未通过 case

### 1. 停滞检测与恢复失效（10）

| Case | 失败过程 | 根因判断 |
| --- | --- | --- |
| `InvoiceReceiptCopyAskUserTask` | Files 中重复点击，无任务进展，最终被重复动作保护阻断 | Runtime 能识别重复，但没有提供换路径、重新定位或回到 Master 的恢复协议 |
| `GoogleMapsAlibabaSouthNeighborTask` | 已定位总部附近，连续向南滚动地图直到 50 turns | 地图画布没有位移、POI 覆盖和方位关系的结构化进展信号 |
| `MastodonCreateListTask` | 反复打开二维码入口、关闭弹层，再打开同一入口 | 已证伪导航边没有进入负证据，Worker 可无限重选错误入口 |
| `MastodonAddFeaturedHashtagsTask` | 连续点击 `Featured`，每次均为 `effect unconfirmed` | 动作效果未确认没有触发熔断和重规划 |
| `MastodonOpenAutomatedDeletionTask` | 在 `Behavior` 中反复滚动、返回、再次进入 | 缺少已访问页面图、滚动终点和错误路径记忆 |
| `MastodonImportMutedUsersTask` | 在 Privacy、Filters 和 Account 页面之间循环 | 缺少深层设置导航知识，同时没有页面循环检测 |
| `MastodonExportFollowsTask` | 在 Account settings 重复向下滚动，未生成文件 | 无滚动边界信号，也没有下载文件 postcondition |
| `MastodonRevisePollTask` | 重复点击帖子菜单，最终仍保留 4 个投票选项 | 目标卡片/菜单锚点不稳定，编辑后没有精确集合验证 |
| `MastodonRevisePhotoAltTask` | 在只读 ALT 弹窗中反复滚动寻找编辑按钮 | 未区分只读详情与编辑态，错误 affordance 没有被证伪 |
| `MattermostBudgetApprovalPipelineTask` | 在频道底部重复滚动，未完成聚合和发布 | 长列表采集缺少去重键、覆盖率、终点和数据完整性检查 |

共同特征是日志已经出现相同画面、相同动作或 `effect unconfirmed`，但这些信号只用于
记录，没有成为下一轮决策的硬约束。

### 2. Runtime 状态/协议完整性（9）

| Case | 直接错误 | 根因判断 |
| --- | --- | --- |
| `InvoiceReceiptCopyTask` | Worker decision state 不是对象 | 一次结构化输出错误直接终止任务，缺少定向纠错 |
| `CancelMeetingTask` | claim 依赖了不允许的 memory 类型 | 模型可以直接构造内部依赖图，Runtime 只在事后拒绝 |
| `CheckRegistrationTask` | evidence 被附加 validity dependency | evidence/claim/commitment 的类型约束未由确定性 API 保证 |
| `SendFormsTask` | 完成一个不存在的 commitment | commitment 生命周期不一致，内部记账错误覆盖 GUI 进展 |
| `MastodonAddBookmarkTask` | executing 阶段没有有效 active commitment | 状态转换缺少确定性 reducer |
| `MastodonRemoveBookmarkTask` | commitment 的 active dependency 已不存在 | memory patch 不是原子事务，依赖可在更新间失效 |
| `MastodonUnfollowTask` | claim 的 active dependency 不存在 | 集合遍历进度依赖自由文本 memory，容易失配 |
| `MastodonFilterLanguageTask` | 0 turn 出现 `NoneType is not iterable` | 启动期空值边界没有契约保护和可诊断错误 |
| `MastodonAdjustTootsTask` | executing 阶段 commitment 依赖无效 | 与 Bookmark/Unfollow 同源的 memory 生命周期问题 |

这些不是应用知识问题。claim、evidence、commitment 属于 Runtime 内部机制，不应由模型
通过复杂 JSON 自由维护，更不应因一次非法 patch 让整条 GUI 任务失败。

### 3. 完成与终止判定错误（4）

| Case | Tool Agent / 官方结果 | 根因判断 |
| --- | --- | --- |
| `TakeSelfieTask` | Agent 50 turns 失败；官方 score 1.0，产生 47 张新照片 | 第一次成功拍照后未验证媒体新增，也未锁存成功，持续点击快门 |
| `MastodonReplyTask` | 回复已成功；后续 WorkerState 校验错误导致 Agent failed | 成功结果可被后续非业务错误覆盖 |
| `MastodonPinTootsTask` | 官方 score 1.0；Agent 继续操作至 50 turns | 未在菜单动作后重新读取目标帖的置顶状态 |
| `MastodonManageHashtagsTask` | Agent completed；官方 score 0，`dogs` 仍在集合中 | completed 前没有验证 expected/remaining/unexpected 集合 |

前 3 个仍是失败：系统没有交付可靠的 `completed` 结果，而且继续执行可能破坏已经满足
的目标状态。第 4 个是相反方向的假完成。

### 4. 规划语义契约错误（3）

| Case | 失败过程 | 根因判断 |
| --- | --- | --- |
| `CheckDeduplicatedEventsTask` | Master program 连续 5 次被同一个 data-filter guard 拒绝 | Review 只给文本错误，没有返回可直接修复的结构化字段差异 |
| `MastodonReportTask` | 搜索不到目标帖后要求用户提供 Frank 的 handle，随后宣布不可完成 | UI 内可发现实体被错误升级为外部 unresolved input |
| `MastodonGetServerInfoTask` | 把 “owner account” 当成必须由用户提供的精确 handle | 没有区分角色标签、界面账户实体和真正的用户秘密 |

Planner 应优先在 UI 中发现账号、帖子和角色实体。只有偏好、秘密或界面外事实才应调用
ask-user。

### 5. 模型服务恢复（1）

`MastodonChangeLanguageTask` 在深层设置中已经出现重复滚动，最终又因模型服务 HTTP 500
失败。500 是直接终止原因，但控制循环本身也不健康。Worker 调用应在同一观察帧做有界
重试，并保留已有执行状态。

## 知识缺口

知识缺口主要集中在应用固有导航事实：

- Mastodon Lists 的真实入口。
- Featured hashtags、自动删除、导入/导出、账户切换的设置路径。
- Mastodon 只读详情、编辑态以及显式提交边界。
- Maps 中应用固有的搜索结果、地点卡片和 POI 控件语义。

这些内容适合进入 `knowledge/android/<App>/`。以下机制不属于 knowledge：

- 相同动作熔断。
- 滚动终点和页面循环检测。
- 集合采集覆盖率。
- success latch 和 postcondition。
- memory 图类型与生命周期。
- 地图位移和方位关系验证。

如果只补知识，Agent 会更容易进入正确页面，但仍会因重复动作、非法 memory 或错误完成
判定失败。

## 优化方案

### P0：统一进展和停滞控制器

为每轮生成 `progress token`，至少包含：

- 页面/弹层标识和画面指纹。
- 目标控件与动作签名。
- 已见对象集合和新收集对象数。
- 滚动位置、是否到达边界。
- 动作后的可观察状态差异。

`progress token` 默认只作为 Worker/Master 的决策证据，不直接成为动作 guard。视觉相似、
语义状态相似、`effect unconfirmed` 或固定重复次数都不够确定，不能据此硬拒绝动作。

硬阻断只允许用于执行器或平台能确定证明无效的情况，例如：

- 控件句柄已经失效或目标坐标明确不在可交互区域。
- 原生滚动 API 明确返回到达同一方向边界且内容 offset 未变化。
- 动作违反平台协议、任务安全规则或已锁存成功后的禁止 mutation 约束。
- 同一个幂等写入已经通过确定性状态读取证明满足目标。

其余情况使用分层软恢复：

- Runtime 记录重复模式、视觉差异、动作效果置信度和已尝试路径，但仍允许 Worker 基于
  新证据说明理由后重复动作。
- Worker 认为没有进展时返回结构化 `stalled_candidate`，附带证据和已尝试策略，不由
  Runtime 根据固定次数强制判定。
- Master 收到候选后决定继续、换路径、重新观察或终止；换路径是规划决策，不是 guard。
- 只有回放证明某一确定性平台信号不会误伤合法重复动作后，才能把该信号升级为硬 guard。

### P0：Memory 更新改为确定性事务

- 模型只输出语义操作，例如“记录证据”“开始目标”“完成目标”。
- Runtime reducer 负责创建 claim/evidence/commitment 及其依赖。
- memory patch 在提交前原子校验。
- 非法 patch 只拒绝本次更新并要求一次定向纠错，不直接终止整个任务。
- 集合任务使用显式 `expected`、`observed`、`remaining`、`unexpected`，不使用自由文本
  commitment 表达遍历进度。

### P0：建立双向完成协议

- mutation 后执行任务类型对应的 postcondition。
- postcondition 成功后写入不可逆 `success latch`。
- latch 后禁止继续 mutation，立即收敛到 completed。
- completed 前必须验证最终状态；集合任务要求 `remaining` 和 `unexpected` 均为空。
- 后续模型或 presentation 错误不能覆盖已经锁存的业务成功，但应单独记录降级状态。

### P1：修复 Planner 和恢复契约

- UI 可发现实体默认使用 discover binding，不能直接变成 ask-user 输入。
- Master review 返回字段级、机器可修复的差异，而不是重复文本错误。
- 模型 5xx 在同一观察帧有限重试，保留 Worker state。
- Runtime 启动期空值、协议类型和 memory reducer 增加确定性契约测试。

### P1：补充应用知识

- 从真实界面或成功轨迹提取稳定导航路径，不写任务名和目标值。
- 记录页面入口、字段语义、只读/编辑态、write-through 或显式提交边界。
- 导入/导出知识包含文件选择入口、下载目录和成功标志。
- Maps 知识只描述应用固有控件；画布进展与方位验证仍由 adapter/runtime 实现。

## 验证计划

回放决策验证是修复进入 live 的硬门禁。任何修复必须先证明相同失败帧上的 Master 或
Worker 决策已经改变，并且新决策满足对应架构不变量。不能只凭单元测试通过，也不能
通过增加 live 重跑次数宣布修复完成。

修复后的验证分为两条路径：

- **MobileWorld 官方 `score == 0`：必须重跑 live。** 回放只能证明决策问题已修复，
  不能替代真实应用状态、动作执行和官方评分验证。
- **MobileWorld 官方 `score == 1`，但 Agent 未 completed：不重跑 live。** 使用原 live
  轨迹进行决策回放，证明新逻辑能识别已有成功证据并收敛到 completed，即可完成验证。

本轮属于第二条路径的 case 是：

- `TakeSelfieTask`
- `MastodonReplyTask`
- `MastodonPinTootsTask`

它们仍计入原始失败统计，但修复验收不要求再次修改真实应用状态。

1. 为 memory reducer、progress token、success latch、集合 postcondition 和
   discover-vs-ask-user 增加单元测试。
2. 将本轮失败轨迹制作成 Master/Worker 决策回放，至少覆盖：非法 memory、成功后未
   停机、假完成、相同动作循环、滚动终点、模型 500。
3. 对每个修复运行对应回放，并断言：
   - 原失败决策不再出现。
   - 新决策产生可观察进展、结构化 `stalled_candidate`、可靠 completed 或可恢复错误之一。
   - 非确定性停滞信号只影响决策上下文，不直接拒绝合法动作。
   - 所有新增硬 guard 都有确定性平台证据和正反例回放。
   - 不引入任务名、目标值、页面坐标等 case-specific 分支。
   - 相邻成功轨迹的 Master/Worker 决策没有回归。
4. 回放全部通过后，对官方 score 0 的修复 case 运行代表 live：
   `MastodonAddBookmarkTask`、`MastodonAddFeaturedHashtagsTask`、
   `MastodonManageHashtagsTask`、`MastodonGetServerInfoTask`。
5. 代表 live 通过后，重跑同类所有官方 score 0 的未通过 case；不能只抽样后宣布这些
   case 已修复。
6. 最后运行路线图中的跨阶段回归集。

官方 score 0 case 的修复验收必须同时满足：重跑 live 后官方 score 1.0、Agent
completed、无非法 memory patch、无未恢复的停滞循环。官方已 score 1.0 但 Agent 失败的
case，以决策回放确认 completed 为验收标准。不能用增加 turn 上限掩盖问题。

每个修复提交应保留对应的回放测试、失败帧输入和断言；生产代码与回放/测试产物按仓库
约定拆分提交。

## 限制

分析使用每个任务截至 2026-08-21 的最新一次 live。单次模型 500 不代表稳定复现。
首要根因是为了确定修复顺序而做的互斥分类；部分 case 同时受到应用知识、视觉感知和
Runtime 的共同影响。按照路线图门禁，失败任务没有通过连续 live 重跑来碰运气。
