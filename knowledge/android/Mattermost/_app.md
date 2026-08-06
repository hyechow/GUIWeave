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
version: 1
---
# Mattermost on Android

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

## Interface contract

- Creating a channel is an untargeted commit that owns the creation form; the
  mutation field is `name` (`text`), the exact requested channel name. No preparatory
  reach is needed before it.
- Adding members to the created channel and posting the welcome message are both
  mutations of the already-created channel: `add_all_members` (`boolean`) adds every
  member, and the posted message is supplied as `message` (`text`). The created
  channel must be the active screen before either takes effect.
