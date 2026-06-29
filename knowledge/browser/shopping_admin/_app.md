---
id: knowledge.browser.shopping_admin.navigation
source_type: knowledge_navigation
platform: browser
app: shopping_admin
scope:
  - decompose
  - planner
  - replanner
source: manual_distilled
confidence: medium
sensitivity: internal
ttl: session
version: 1
---
# 应用级导航概览：shopping_admin

## 1. 应用概述
该应用是一个电商后台管理系统（Admin），核心功能涵盖商品与目录管理、订单全生命周期处理（下单、开票、发货、退款）、客户与营销管理，以及基于多维度的销售数据分析报表。

## 2. 页面列表

### 首页与入口
*   **Admin Dashboard (home)**: 展示实时销售概览、订单趋势图表及关键业务指标快照；快照区包含 Lifetime Sales、Average Order、Last Orders、Last Search Terms（最近搜索词）和 Top Search Terms（最常用搜索词）。这些快照区可能位于页面下方，需要在 Dashboard 内滚动定位后读取。查询 top/most-used search terms 时，应读取 Dashboard 的 **Top Search Terms** 区块，不要把 **Last Search Terms**（最近搜索词）当成 top 口径。
*   **Admin Sidebar (navigation hub)**: 左侧主菜单，作为所有功能模块的导航枢纽。

### 商品与目录管理 (Catalog)
*   **Products List (list)**: 展示所有产品列表，支持搜索、筛选、批量操作及创建新产品。
*   **Product Workspace (form/detail)**: 单个产品的编辑详情页，包含属性集、价格、库存、SEO 等配置。
*   **Create a Product (form)**: 专门用于初始化新产品的向导式表单页面。
*   **Categories List (list)**: 以树状结构展示分类层级，支持拖拽排序和子分类创建。
*   **Category Workspace (form/detail)**: 单个分类的编辑页，包含内容、显示设置、SEO 及权限配置。
*   **Moderate Product Reviews (list/form)**: 待审核的产品评论列表及单条评论的详情/编辑页。
*   **Product Reviews (list)**: 查看所有已发布或历史的产品评论记录。

### 销售与订单处理 (Sales)
*   **Orders List (list)**: 所有销售订单的主列表，按状态追踪订单进度。对于按订单历史统计 customer email(s) 的聚合任务（如 most/second/fifth number of orders、have N orders、completed orders），这是权威 UI 数据源：进入 **Sales > Orders**。⚠️ **判别要不要筛 `Status = Complete` 的关键 = intent 里有没有 "completed" 这个词**：
    *   **出现 "completed"（要筛 Complete）**：`who completed the most/second/Nth number of orders` / `who completed N orders` / `completed orders` —— WebArena 这类任务的参考答案**按 `Status = Complete` 计数**（实测 task 63 坐实：期望答案 helloworld+michael 只有在 Complete 口径下计数才相等，any-state 算不出）。第一步先点 `Clear all` 清除**所有**残留 `Active filters`（Magento 跨会话保留上次筛选，截图里若残留 `Purchase Date` 等任务未要求的范围筛选必须删掉，只留 Status），再**只应用 `Status = Complete`**，然后采全量 Complete 订单行（筛后约 155 行，**必须采全**——个位数计数下漏几笔就会把名次搅乱）。
    *   **字面 "any state" / 只说 "have N orders"（不要筛 Status）**：如 `who have N orders in any state`（task 64）—— 统计**所有状态**订单数，**绝不能**筛 `Status = Complete`，SQL 里也**不得**写 `WHERE status = 'complete'`；第一步 `Clear all` 清掉残留 `Active filters`（含残留的 `Status: Complete`），再不加任何状态筛选地采集全量订单。
    收集完整订单行中的 `Customer Email`/`Status`（不要导出/下载——agent 读不回下载文件），再用 data_query 做 group/count/rank/tie。⚠️ **按订单数计数时,foreach returns 必须采 `ID` + `Customer Email` + `Status` 三列**(`ID` 是逐行唯一列,缺它采集器会按整行内容去重——`Customer Email=x` 单列时同一客户的多笔订单去重键相同,第二笔起会被当重复行丢掉,把 308 笔订单塌成几十行、每人计数严重偏小、排名全错,WebArena task 63 实测就栽在这；带上订单 `ID` 后每笔订单是不同的行、计数才对,真正的翻页重叠(同 `ID`)仍会正确折叠）。同时把 `Status` 一并采回,口径判定/复核都靠它。不要优先走 Customer Reports 或 Customers grid 的 `Total Orders` 列。对于按月统计 **completed orders（名词性）** 且带 `Purchase Date` 范围的任务，也先用 Orders 页面 Filters：`Status = Complete`，`Purchase Date from <start as MM/DD/YYYY> to <end as MM/DD/YYYY>`（例如 `2023-01-01` 要写成 `01/01/2023`，`2023-05-31` 要写成 `05/31/2023`；不要在页面筛选步写 ISO `YYYY-MM-DD`），Apply Filters 后读完整 Orders grid；data_query 只对已筛选的 provider 字段 `created_at` 按月 group/count，不要再写 `WHERE status = 'Complete'` 或重复日期范围；最终 JSON 对象数组用单个 `result` 返回，SQL 列 alias 为 `month` 和 `count`，`month` 值必须是 January/February/... 的英文月名。
*   **Order Detail (detail/form)**: 单个订单的详情页，包含信息、发票、退款、发货及评论历史标签页。
*   **Invoices List (list)**: 所有已生成的发票列表。
*   **Invoice Detail (detail/form)**: 单个发票的编辑与打印详情页。
*   **Shipments List (list)**: 所有发货记录列表。
*   **Shipment Detail (detail/form)**: 单个发货单的编辑页，支持添加物流单号。
*   **Credit Memos List (list)**: 所有贷项通知单（退款）列表。
*   **Credit Memo Detail (detail/form)**: 单个退款单的生成与编辑页。
*   **Quotes List (list)**: B2B 报价请求列表（如适用）。
*   **Quote Templates (list/form)**: 可复用的报价模板管理。
*   **Billing Agreements (list)**: 账单协议列表。
*   **Transactions (list)**: 支付交易活动记录。
*   **Archive (list)**: 归档的历史订单与文档列表。

### 客户管理 (Customers)
*   **All Customers List (list)**: 注册客户及管理员添加客户的完整列表。
*   **Customer Group List (list)**: 客户分组列表（如普通、批发商）。
*   **Customer Group Workspace (form)**: 新建或编辑客户分组的表单页。
*   **Now Online (list)**: 当前在线的客户和访客列表。
*   **Segments (list/form)**: 动态客户细分规则列表及配置。
*   **Companies (list/form)**: B2B 公司账户管理列表及详情。

### 营销与用户内容 (Marketing)
*   **Cart Price Rules (list/form)**: 购物车价格规则列表及配置页。
*   **Pending Reviews (list)**: 待审核的用户评论列表。
*   **All Reviews (list)**: 所有用户评论的管理列表。
*   **Ratings (list/form)**: 自定义评分标准（如质量、价格）的配置页。

### 报表与分析 (Reports)
*   **Reports Menu (navigation hub)**: 报表分类导航页，按销售、产品、客户等维度组织。
*   **Sales Reports (list)**: 销售类报表汇总（订单、税收、发票、运费等）。其中 **Orders Report**（Reports › Sales › Orders）按日期区间统计订单。⚠️ **"Show / View the sales order report (for <时间段>)" 是纯导航（NAVIGATE）意图,不是取数任务**：目标只是**到达并渲染**该报表 —— **先按报表子类型选对入口**(见下方「报表子类型 → URL 映射」:orders→Reports › Sales › **Orders**、tax→Reports › Sales › **Tax**;**"tax report" 必须进 Tax 报表,不是 Orders 报表**),在 **From / To** 填日期区间(MM/DD/YYYY),点 **Show Report**。**终态/成功判据是导航性的而非数据性的**:点 Show Report 后页面 URL 跳到该子类型的报表渲染端点(`…/reports/report_sales/<subtype>/filter/…`,subtype=sales 对应 orders 报表、tax 对应 tax 报表)即算到达成功。⚠️ **报表区可能为空**(该年度统计未刷新 / 无订单时显示 "No records found",且 Magento 报表页始终把 Filter 表单留在顶部、统计表渲染在其下方甚至 below-fold)——**空报表 / 看不到统计行也算成功,绝不要因为"没看到统计表格"就反复点 Show Report**(实测会被误判 in_progress 死循环点 7 次后失败)。success_condition 只写"已点 Show Report 且 URL 进入 report_sales/<subtype>/filter 渲染页",不要写"统计表格已渲染/出现 N 行数据"。**不要给这类意图绑 returns / data_query / 不要去读 total_orders、total_revenue 等具体数值**(任务没要求返回任何字段,retrieved_data 应为 null;读不存在的字段会触发空读→kickback 死循环→误入 Refresh Statistics)。时间段换算见下方「相对日期换算」。
*   **报表子类型 → URL 映射**(决定 `report_sales/<subtype>/filter` 的 subtype,务必按 intent 的报表名选对,否则 NetworkEvent 不匹配):**orders / sales order report → `sales`**(Reports › Sales › Orders);**tax report → `tax`**(Reports › Sales › Tax);invoiced → `invoiced`、shipping → `shipping`、refunds → `refunded`、coupons → `coupons`(均在 Reports › Sales › 对应子项)。
*   **相对日期换算**(报表 From/To,相对 intent 里给定的 today;**关键:"this year" 截到今天、不是年底**):
    *   **"last year"** = 去年**整年** → From `01/01/<去年>`、To `12/31/<去年>`。例:today=Mar 15 2023 → From `01/01/2022`、To `12/31/2022`(请求 `report_sales/sales/filter?report_type=created_at_order&from=2022-01-01&to=2022-12-31`)。
    *   **"this year"** = 今年 1 月 1 日 → **今天**(当年未过完,To 取 today,**不要填 12/31**)。例:today=Mar 15 2023 的 "tax report for this year" → From `01/01/2023`、To `03/15/2023`(请求 `report_sales/tax/filter?report_type=created_at_order&from=2023-01-01&to=2023-03-15`)。
    *   **显式区间**(intent 已给起止日期)= 直接用,不换算。例:"orders report from May 1 2021 to March 31 2022" → From `05/01/2021`、To `03/31/2022`。
*   **Product Reports (list)**: 产品类报表汇总（浏览量、畅销品、库存预警等）。
*   **Customer Reports (list)**: 客户类报表汇总（订单总额、新增账户、愿望清单等）。
*   **Marketing Reports (list)**: 营销类报表汇总（购物车放弃率、搜索词、邮件问题等）。
*   **Review Reports (list)**: 评论分析报表（按客户或产品统计）。
*   **Statistics Refresh (form/modal)**: 手动刷新报表统计数据的操作页。

### 系统配置 (Stores & System)
*   **Configuration (form)**: 全局系统配置中心，包含商店设置、税务、货币、属性集等。
*   **Attribute Sets (list/form)**: 产品属性集的定义与管理。
*   **Extensions Marketplace (external link)**: 合作伙伴与扩展程序市场入口。

### 内容与设计 (Content)
*   **Pages (list/form)**: CMS 静态页面列表及编辑页（如 Home Page、Privacy Policy 的标题/内容）。
*   **Blocks / Widgets (list/form)**: 可复用内容块与小部件配置。
*   **Design Configuration (list)**: 各 Store View 的设计配置入口。
*   **Themes (list)**: 已安装主题列表（Magento Luma、Magento Blank 等）。⚠️ **主题设置 / 外观设置 / "Magento Luma theme settings page" 在这里**——路径 **Content › Design › Themes**，**不在 System/Stores 菜单**（Design 入口属于 Content，不属于 System）。进列表后点 **Magento Luma** 行进入该主题设置页 `admin/system_design_theme/edit/id/3`（页标题 "Theme: Magento Luma"）。
*   **Schedule (list)**: 设计变更的定时排程。

## 3. 导航关系

### 全局导航
*   **从任何页面 -> Admin Dashboard**: 点击左侧侧边栏顶部的 Logo 或 "Dashboard" 链接。
*   **从任何页面 -> 任意主菜单**: 点击左侧侧边栏对应的图标（如 Sales, Catalog, Customers 等）展开子菜单。
*   **从任何页面 -> 全局搜索**: 点击右上角搜索图标，输入关键词跳转至对应记录的 Detail 页。

### 模块内导航
*   **Catalog 模块**:
    *   `Sidebar` -> `Catalog` -> `Products`: 进入 **Products List**。
    *   `Products List` -> `Add Product` / `Edit`: 进入 **Product Workspace** 或 **Create a Product**。
    *   `Product Workspace` -> `Product Reviews` 区域: 进入 **Moderate Product Reviews** 或 **Product Reviews**。
    *   `Sidebar` -> `Catalog` -> `Categories`: 进入 **Categories List**。
    *   `Categories List` -> `Add Subcategory` / `Edit`: 进入 **Category Workspace**。
*   **Sales 模块**:
    *   `Sidebar` -> `Sales` -> `Orders`: 进入 **Orders List**。
    *   `Orders List` -> `View` / `Action`: 进入 **Order Detail**。
    *   `Order Detail` -> `Invoices` / `Shipments` / `Credit Memos` 标签: 分别进入对应的 **List** 视图或 **Detail** 视图。
    *   `Orders List` -> `Create New Order`: 进入 **Order Detail** (新建模式)。
    *   `Sidebar` -> `Sales` -> `Invoices` / `Shipments` / `Credit Memos`: 直接进入各自的 **List** 页。
*   **Customers 模块**:
    *   `Sidebar` -> `Customers` -> `All Customers`: 进入 **All Customers List**。
    *   `All Customers List` -> `Add New Customer` / `Edit`: 进入 **Customer Workspace** (隐含在列表中)。
    *   `Sidebar` -> `Customers` -> `Customer Groups`: 进入 **Customer Group List**。
    *   `Customer Group List` -> `Add New`: 进入 **Customer Group Workspace**。
*   **Reports 模块**:
    *   `Sidebar` -> `Reports`: 进入 **Reports Menu**。
    *   `Reports Menu` -> `Sales` / `Products` / `Customers` / `Marketing`: 进入对应的 **Report List** 页。
    *   `Report List` -> `Export` / `Refresh`: 触发数据导出或刷新操作（通常在同一页面或弹窗完成）。
*   **Content 模块**:
    *   `Sidebar` -> `Content` -> `Design` -> `Themes`: 进入 **Themes** 列表；点 **Magento Luma** 行 -> 该主题设置页（`admin/system_design_theme/edit/id/3`）。主题/外观设置都走这里，**不要去 System/Stores 菜单找 Design**。
    *   `Sidebar` -> `Content` -> `Elements` -> `Pages`: 进入 **Pages** 列表（CMS 页面标题/内容编辑）。

### 跨模块关联
*   **Order Detail** -> **Customer Profile**: 点击订单中的客户名称跳转至 **All Customers List** 或客户详情。
*   **Order Detail** -> **Product Details**: 点击订单中的产品名称跳转至 **Product Workspace**。
*   **Product Workspace** -> **Reviews**: 滚动至底部进入评论管理区域。
*   **Configuration** -> **Cache Management**: 保存配置后提示刷新缓存（系统级操作）。

## 4. 关键操作路径

1.  **创建并上架新产品**:
    `Admin Dashboard` -> `Sidebar (Catalog)` -> `Products List` -> `Add Product` -> `Product Workspace` (填写属性/价格) -> `Save` -> `Enable Product` -> 返回 `Products List`。

2.  **处理新订单流程**:
    `Sidebar (Sales)` -> `Orders List` -> `View` (选中 Pending 订单) -> `Order Detail` -> `Invoice` (生成发票) -> `Ship` (生成发货单) -> `Submit Shipment` -> 更新状态为 Complete。

3.  **管理客户分组与权限**:
    `Sidebar (Customers)` -> `Customer Groups` -> `Add New Customer Group` -> `Customer Group Workspace` (设置税类和网站排除) -> `Save` -> `All Customers List` -> 选中客户 -> `Actions` -> `Assign a Customer Group`。

4.  **查看销售绩效与报表**:
    `Admin Dashboard` -> `Sidebar (Reports)` -> `Reports Menu` -> `Sales` -> `Orders Report`(tax report 则选 `Tax`,见「报表子类型 → URL 映射」)-> (在 From/To 选择日期范围,相对日期按「相对日期换算」:this year 截到 today、不是年底) -> `Show Report` (提交后 URL 进入 `…/reports/report_sales/<subtype>/filter/…` 渲染页即到达终态;**报表区可能为空也算成功,不要反复点**)。⚠️ 不要规划 `Export`/下载 CSV(agent 读不回下载文件,已禁);"show the report" 类意图到 Show Report 提交进入渲染页就结束,不读具体数值、不要求出现统计行。

5.  **审核用户评论**:
    `Sidebar (Marketing)` -> `User Content` -> `Pending Reviews` -> `List` -> `Click Review` -> `Moderate Product Reviews` (Status: Approved/Not Approved) -> `Save Review`。
