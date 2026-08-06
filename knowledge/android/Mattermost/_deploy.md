---
id: knowledge.android.Mattermost.deployment
source_type: deployment_context
platform: android
app: Mattermost
scope:
  - decompose
  - orchestrator
  - planner
  - replanner
source: mobileworld_app_contract
confidence: high
sensitivity: secret
aliases:
  - Mattermost
  - mattermost
ttl: session
version: 1
---
# Mattermost 部署/访问信息(本环境)

> 本文件只记**环境/访问**信息(在哪、怎么进)。功能/界面见 `_app.md` 与接口段。

- **团队(team)名**:`neuralforge`
- Mattermost 服务在模拟器视角为 `http://10.0.2.2:8065`(容器宿主;app 通常已登录、会话长期有效)。
- 若出现登录页(会话失效异常),用团队账号登录,**所有团队账号密码均为 `password`**:
  - `admin@test.com` / `password`(admin 账号)
  - `sam.oneill@neuralforge.ai` / `password`
  - `alex.rivera@neuralforge.ai` / `password`
  - `mike.santos@neuralforge.ai` / `password`
  - `sofia.garcia@neuralforge.ai` / `password`
- 「把所有人加进频道」= 把整个 `neuralforge` 团队(共 11 名成员)加进目标频道。
