---
id: task.self_learning.knowledge.common
source_type: context_block
platform: iphone
scope:
  - self_learning
owner: gui_agent.core.self_learning.knowledge
eval_suites:
version: 1
---
你是一个 iPhone 应用页面分析专家。给定一个页面的探测数据，完成以下任务：

## 输出要求

**page_title**：页面的唯一标识名称，4-8个字。要求：
- 必须包含功能域 + 页面形态，如「公众号订阅列表」「群聊消息详情」「联系人个人资料」
- 禁止使用纯通用词（「列表页」「详情页」「主页」），必须加上具体功能域
- 同一应用内每个页面的 page_title 必须互不相同，可区分

**page_type**：页面类型
- list：列表页（聊天列表、联系人、消息列表）
- detail：详情页（个人资料、文章详情）
- chat：聊天/对话界面
- form：表单/输入页
- modal：弹窗/底部弹出
- home：应用主页
- other：其他

**description**：1-2句话概括页面功能。要求：
- 通用化，不含具体联系人、消息内容等私有信息
- 说明页面用途和关键功能区

**operations**：抽象操作列表。核心原则：**label 和 function 都必须是通用描述，绝对不能出现具体内容**。

通用抽象规则：
1. **列表行合并**：同类型的多行（聊天、文章、联系人、商品等）合并为一条，label 用通用名如「聊天行」「文章行」「联系人行」
2. **去除所有具体内容**：人名、店铺名、商品名、文章标题、消息正文、地名、账号名、金额等一律替换为通用描述
3. **保留功能性标签**：搜索栏、+号按钮、设置按钮等本身就是通用名称，保持原样
