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

- **Channel** is the created entity. Its creation field is **name** (`text`), the exact
  requested channel name. Creating a channel is a single form flow with no
  preparatory Channel-creation step.
- After creation, the channel is a concrete existing record with the requested name.
- **Add all members** and **post the welcome message** are two separate operations on
  that existing created channel record (not on the creation form): adding every member
  is one operation (`add_all_members`), and sending the welcome message is one
  operation (`message` with the exact greeting text). Both act on the channel that was
  just created, so creation always comes first.

