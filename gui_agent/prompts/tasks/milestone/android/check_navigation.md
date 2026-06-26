---
id: context.milestone.android.check.navigation
source_type: context_block
platform: android
scope:
  - checker
owner: gui_agent.adapters.android.supervisor.milestone
eval_suites:
  - evals/android/checker
version: 1
---

## 导航类子目标（kind=navigation）
- done 仅当当前界面身份与目标界面精确匹配（顶部标题/当前 App 匹配、主内容区符合）。
- 判 done 时 reason 必须写清界面身份证据（标题文字、当前 App、关键区块）。
- 仍在导航途中、界面不匹配、加载中，一律 in_progress。
- 若目标是浏览器/API 的具体 URL 或路径，地址栏/页面标题中可见该目标 URL/路径是最可靠的界面身份证据。若当前截图看不到地址栏或看不到目标路径，而正文又是小字号密集 JSON/文本，则不能只凭正文里出现的链接字符串或相似字段判定已导航到目标 endpoint。
- 对 API/JSON 目标页，必须区分「目标数据本身」和「指向目标数据的链接字段」。如果验收条件要求某个接口返回的 JSON 数组/列表/对象集合，当前页面必须实际显示这些顶层数据项，并能指出数组外层结构或多个同类条目；只看到 `*_url`、`url`、`href`、`*_count`、模板链接、单个元数据对象字段，或包含目标接口路径的字符串，不等于已进入该接口响应页，判 in_progress。不要因为页面里出现目标词、相似字段名、或链接里包含目标路径，就声称目标数组已经显示。
- 如果当前 JSON 页面主要是一个资源详情对象（大量 `*_url` 链接字段、计数字段、配置/状态字段、嵌套的单个 owner/user 对象等），而目标要求的是某个链接接口的数组/列表响应，则界面身份不匹配，必须判 in_progress。单个嵌套对象里的 `login/avatar_url/name` 等字段不是「多个顶层列表项」证据。
