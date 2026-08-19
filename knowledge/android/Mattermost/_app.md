---
id: knowledge.android.Mattermost.navigation
source_type: knowledge_navigation
platform: android
app: Mattermost
scope:
  - decompose
  - orchestrator
  - planner
  - replanner
source: manual_verified
confidence: medium
sensitivity: internal
ttl: session
version: 2
---
# Mattermost on Android

- 本环境 Mattermost 实例的内容团队为 `neuralforge`;频道创建与成员操作均作用于该团队。
- Mattermost is a team-messaging application. The main screen lists channels in a
  sidebar; the opened channel shows its message stream with a message input at the
  bottom.
- Creating a channel is a one-shot form flow: the create action opens a channel
  creation form, the channel name is typed into it, and confirming creates the
  channel and opens it.
- A newly created channel's members are managed from the channel itself: the member
  list / invite action can add every member to the channel.
- A welcome message is sent by composing it in the opened channel's message input
  and sending it.
- Replying is scoped to one existing message: long-press the visible message body,
  choose `Reply`, then compose and send in the thread view. Posting in the channel
  composer is a new message, not a reply.
- In this MobileWorld deployment, Harry is the task user represented by first-person
  references such as `I`, `me`, `my`, and `own`. Deployment login names are access
  credentials, not the task user's identity.

## Interface contract

- A channel is created by its `name` (text field) in a single form flow; no
  preparatory channel-creation step exists.
- Adding every member to a created channel is one operation, and posting the
  welcome message is another; both act on the already-created channel (not the
  creation form), so creation always comes first.
