---
id: knowledge.android.Mail.interface
source_type: knowledge_interface
platform: android
app: Mail
scope:
  - orchestrator
selector_when: Mail email send compose subject body message content attachment chen@gmail.com paper 邮件 发送 收件人 标题 正文 内容 附件
source: manual_verified
confidence: medium
ttl: session
---
# Mail interface

- **Sending email** is an untargeted GUI commit with **recipient** (`text`), **subject** (`text`),
  and message **body** (`text`) fields. When the task asks to tell, ask, explain, or include message
  content, the body must visibly contain that content before activating Send. Select attachments
  through the compose form's picker before Send.
- For every file in a folder, include each direct file regardless of type and reach the physical
  folder through `Show roots` → device storage; the `Documents` category can omit types. The picker
  may return after one selection, so verify the attachment in compose and reopen it for each
  remaining file.
