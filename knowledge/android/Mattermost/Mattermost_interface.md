---
id: knowledge.android.Mattermost.interface
source_type: knowledge_interface
platform: android
app: Mattermost
scope:
  - orchestrator
selector_when: Mattermost channel create reading paper reading add everyone greet welcome message 建频道 创建频道 频道 欢迎语
source: manual_verified
confidence: medium
ttl: session
---
# Mattermost interface

- **Channel** is the created entity. Its creation mutation field is **name** (`text`),
  the exact requested channel name. Channel creation is an untargeted commit owning
  the create-channel form; there is no preparatory Channel-creation entity or reach.
- After the channel exists and is active, adding every member is a per-channel
  mutation with **add_all_members** = `True`, and the welcome message is sent with
  **message** = the exact greeting text. Both operate on the created channel, so the
  program creates the channel before it adds members or sends the message.
