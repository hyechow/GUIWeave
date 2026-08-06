---
id: knowledge.android.Mail.interface
source_type: knowledge_interface
platform: android
app: Mail
scope:
  - orchestrator
selector_when: Mail email send compose subject attachment chen@gmail.com paper 邮件 发送 收件人 标题 附件
source: manual_verified
confidence: medium
ttl: session
---
# Mail interface

- **Sending email** is an untargeted commit that owns the compose form. Its mutation
  fields are **recipient** (`text`), **subject** (`text`), and **attachments** (a
  list of file names). The executor opens the compose form, fills To and Subject,
  attaches each given file, and sends. There is no preparatory Mail entity or reach.
- The files to attach are produced by a prior Files operation (moved into the
  destination folder and read by name there). The send commit references those
  observed file names in **attachments**, so the cross-app dataflow is
  Files-read → `launch_app`("Mail") → send-commit.
