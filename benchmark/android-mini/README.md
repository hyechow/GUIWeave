# Android Mini Benchmark

轻量真机 benchmark，用 runner 的编排器模式跑可拆分的 Android 子任务。这里的任务会真实启动
`bin/runner android ... --orchestrator`，因此不放在 `evals/`；`evals/` 只保留离线/LLM 单点回归。

## 运行

```bash
HEADLESS=1 ANDROID_SERIAL=<serial> uv run python benchmark/android-mini/run.py
```

也可以只跑一个 case：

```bash
HEADLESS=1 ANDROID_SERIAL=<serial> uv run python benchmark/android-mini/run.py --label github-repo-discovery-androidworld
```

这个 mini benchmark 默认按 case 的 `runner_args` 透传 `--orchestrator --no-router`，
用于隔离测试编排器执行层；入口 router 分类问题应单独收录。

每个 Android case 可以声明 `init_script`。脚本在 runner 启动前执行，用来设置真机初始
状态；例如 `benchmark/android-mini/init/open_chrome_url.sh` 可以把 Chrome 打开到指定
URL，并按 case 配置选择保留 Chrome 前台，或 force-stop 后回到 Launcher。只
`force-stop` 不够，因为 Chrome 会恢复上一次打开的 tab。

Mastodon list 类 case 使用 `benchmark/android-mini/init/open_mastodon.sh` 的
`ANDROID_MINI_MASTODON_LIST_STATE` 准备独立前置状态，避免调试后半段时每次都真实
重跑前面的创建/添加动作。支持的状态包括 `dirty`、`empty`、`open-created`、
`open-members`、`cute-created`、`cute-pupper`、`cute-pupper-kitty`、`full`。
其中成员添加被拆成单步 mini case：init 直接打开 Add member 页面，每个 case 只点
一个用户行右侧的 `Add` 一次，然后用 DB 精确验证。对应结果用
`benchmark/android-mini/verify/verify_mastodon_lists.sh` 查 Mastodon DB 精确验证。

## Cases

| label | layer | goal | assertion |
| --- | --- | --- | --- |
| github-repo-discovery-androidworld | CheckGithubInfoTask / step 1 | 帮我找到 AndroidWorld 这个 Android GUI agent benchmark 项目的 GitHub 仓库。 | 当前 Android UI 文本必须处在 `google-research/android_world` 仓库详情页，搜索结果页不算通过 |
| github-repo-info-androidworld | CheckGithubInfoTask / step 2 | 帮我看下 AndroidWorld 这个 Android GUI agent benchmark 的 GitHub 仓库现在有多少 stars 和 contributors，直接告诉我两个数字。 | 最终输出必须同时包含 stars 和 contributors 的数量表达；UI 仍需停留在 GitHub 相关页面，不走 API/JSON |
| find-kevin-resume | SendInterviewInvitationTask / step 1 | 在手机的 Download 文件夹里找到 Kevin 的简历文件。 | 当前 UI 必须显示 Kevin 简历/PDF 相关文本 |
| read-kevin-resume-phone | SendInterviewInvitationTask / step 2 | 打开 Kevin 简历并读出电话号码。 | 最终输出必须包含 `15551234567` |
| mastodon-postpoll-search-winners | MastodonPostPollTask / step 1 | 从 Google 搜索结果页读取 2025 Nobel Prize in Economics 三位获奖者。 | 最终输出必须包含 `Joel Mokyr`、`Philippe Aghion`、`Peter Howitt` |
| mastodon-postpoll-compose-settings | MastodonPostPollTask / step 2 | 在已打开的 Mastodon poll compose 面板里填三位获奖者，并设置 `1 week` / 多选。 | 当前 UI 必须同时显示 `#vote2025`、三个人名、`1 week`、`Multiple choice`；不发布 |
| mastodon-postpoll-publish | MastodonPostPollTask / step 3 | 从已填好的 Mastodon poll compose 页发布投票。 | 后端数据库必须存在本次 marker 的 `#vote2025` poll，且包含三个人名、`multiple=true`、约 1 周过期 |
| mastodon-list-cleanup | MastodonManageMultiListTask / step 1 | 打开 Mastodon lists 页面并删除所有旧 list。 | init 先创建 `old-open`/`old-cute` 脏状态；DB 验证 `test` 用户 list 为空 |
| mastodon-list-open-create | MastodonManageMultiListTask / step 2 | 创建 `open` list，并把 replies 设置成 followed users。 | DB 验证 `open` 存在、`replies_policy=1`、`exclusive=false`、成员为空 |
| mastodon-list-open-add-members | MastodonManageMultiListTask / step 3 | 给 `open` 添加用户名包含 `open` 的用户。 | init 预置空 `open`；DB 验证成员精确为 `openCompany`、`openUniversity` |
| mastodon-list-cute-create | MastodonManageMultiListTask / step 4 | 创建 `cute` list，replies 设置成 list members，并启用 hide members in following。 | init 预置已完成 `open`；DB 验证 `cute` 存在、`replies_policy=0`、`exclusive=true`、成员为空 |
| mastodon-list-cute-add-members | MastodonManageMultiListTask / step 5 | 从 Add member 页面给 `cute` 一次性添加全部头像为猫或狗的用户。 | init 预置 `open` 完成 + 空 `cute`；DB 验证 `cute` 成员精确为 `pupper`、`kitty`、`olivia`，并验证 `open` 仍完整 |
