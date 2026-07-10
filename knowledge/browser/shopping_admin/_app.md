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
version: 2
---
# 应用级导航概览：shopping_admin

## 1. 应用概述
该应用是一个电商后台管理系统（Admin），核心功能涵盖商品与目录管理、订单全生命周期处理（下单、开票、发货、退款）、客户与营销管理，以及基于多维度的销售数据分析报表。

## 2. 页面列表

### 首页与入口
*   **Admin Dashboard (home)**: 展示实时销售概览、订单趋势图表及关键业务指标快照；快照区包含 Lifetime Sales、Average Order、Last Orders、Last Search Terms（最近搜索词）和 Top Search Terms（最常用搜索词）。这些快照区可能位于页面下方，需要在 Dashboard 内滚动定位后读取。查询 top/most-used search terms 时，应读取 Dashboard 的 **Top Search Terms** 区块，不要把 **Last Search Terms**（最近搜索词）当成 top 口径。
*   **Admin Sidebar (navigation hub)**: 左侧主菜单，作为所有功能模块的导航枢纽。

### 商品与目录管理 (Catalog)
*   **Products List (list)**: 展示配置型父商品与 Simple 变体，支持搜索、筛选、列选择、批量操作及创建产品。Color 可作为可选列启用；Material/Size 不属于本环境的网格可选列。行内 Action/Edit 是该记录的详情入口。
*   **Product Workspace (form/detail)**: 单个产品的编辑详情页，包含 Content、价格、库存、属性集及 SEO。配置型父商品拥有 Configurations、聚合 Stock Status 和目录侧 Short Description；Simple 变体拥有自己的 Price、Quantity、Size、Color。未限定长/主描述的商品 description 请求对应 Short Description，主 Description 是独立的长内容字段。Products 按名称检索可能同时出现父子记录；必须用 Type 选择实际能力所有者。逻辑范围包含整组变体时，执行次数仍由目标字段的所有权决定：父级聚合字段只改父级一次，成员字段才逐变体处理。
*   **Create a Product (form)**: 专门用于初始化新产品的向导式表单页面。
*   **Categories List (list)**: 以树状结构展示分类层级，支持拖拽排序和子分类创建。
*   **Category Workspace (form/detail)**: 单个分类的编辑页，包含内容、显示设置、SEO 及权限配置。
*   **Moderate Product Reviews (list/form)**: 待审核的产品评论列表及单条评论的详情/编辑页。
*   **Product Reviews (list)**: 查看所有已发布或历史的产品评论记录。

### 销售与订单处理 (Sales)
*   **Orders List (list)**: 所有销售订单的主列表，也是订单历史聚合的权威原始行源，含 ID、Customer Email、Status、Purchase Date、Grand Total 与详情入口。completed 口径使用 Status=Complete；any-state 不保留 Status；non-cancelled 需要采集 Status 后排除 Canceled，不能用另一个正状态近似。订单号 `#N` 用 Filters 的 ID 字段填数字 N，顶部 keyword 不可靠。Magento 跨会话保留筛选，先清除无关条件；日期筛选使用 **MM/DD/YYYY**。
*   **Order Detail (detail/form)**: 单个订单的详情页，包含 Items Ordered、发票、退款、发货及 Comments History / Notes。客户通知属于 Notes 表单；物流追踪属于 Shipment，不能用订单 Comment 代替。
*   **Invoices List (list)**: 所有已生成的发票列表。
*   **Invoice Detail (detail/form)**: 单个发票的编辑与打印详情页。
*   **Shipments List (list)**: 所有发货记录列表；订单已有 Shipment 时从这里进入其追踪信息。
*   **Shipment Detail (detail/form)**: 单个发货单的编辑页，拥有 Carrier 与 Tracking Number。订单尚无 Shipment 时由订单详情的 Ship 能力先创建发货单。
*   **Credit Memos List (list)**: 所有贷项通知单（退款）列表。
*   **Credit Memo Detail (detail/form)**: 单个退款单的生成与编辑页。
*   **Quotes List (list)**: B2B 报价请求列表（如适用）。
*   **Quote Templates (list/form)**: 可复用的报价模板管理。
*   **Billing Agreements (list)**: 账单协议列表。
*   **Transactions (list)**: 支付交易活动记录。
*   **Archive (list)**: 归档的历史订单与文档列表。

### 客户管理 (Customers)
*   **All Customers List (list)**: 注册客户及管理员添加客户的完整列表。Phone 以带括号/空格的格式显示；顶部 keyword 是字面子串检索，格式不同的整串号码可能不匹配，而连续的本地号段可以匹配。
*   **Customer Group List (list)**: 客户分组列表（如普通、批发商）。
*   **Customer Group Workspace (form)**: 新建或编辑客户分组的表单页。
*   **Now Online (list)**: 当前在线的客户和访客列表。
*   **Segments (list/form)**: 动态客户细分规则列表及配置。
*   **Companies (list/form)**: B2B 公司账户管理列表及详情。

### 营销与用户内容 (Marketing)
*   **Cart Price Rules (list/form)**: 作用于购物车、结账或整单购买的折扣规则；有 Coupon 字段。
*   **Catalog Price Rules (list/form)**: 作用于目录商品的折扣规则；无 Coupon，Conditions 为空表示全部商品。
*   **Pending Reviews (list)**: 待审核的用户评论列表。
*   **All Reviews (list)**: 所有用户评论的权威记录源，网格含 Product、Title、Review 和详情入口；Detailed Rating、Nickname、Summary of Review 属于单条评论详情，不能把 Rating 声明成网格行字段，按评分分析前必须从各评论详情补齐。
*   **Ratings (list/form)**: 自定义评分标准（如质量、价格）的配置页。

### 报表与分析 (Reports)
*   **Reports Menu (navigation hub)**: 报表分类导航页，按销售、产品、客户等维度组织。
*   **Sales Reports (list)**: 销售类报表汇总（订单、税收、发票、运费等），子项含 **Orders**（Reports › Sales › Orders）、**Tax**（Reports › Sales › Tax）等，按日期区间统计。⚠️ **"Show / View the sales order report / tax report (for <时间段>)" 是导航类意图**——目标是**到达并渲染**对应报表，不是读取报表里的具体数值。做法：① 按报表名选对子类型入口（**"tax report" 进 Tax 报表，不是 Orders 报表**；子类型→URL 见下方映射）；② 在 **From / To** 填日期区间（**MM/DD/YYYY**，相对日期换算见下方），点 **Show Report**。报表渲染在 URL `…/reports/report_sales/<subtype>/filter/…`，进入该渲染页即视为到达。**报表区可能为空**（无数据时显示 "No records found"），且 Magento 报表页把 Filter 表单留在顶部、统计表渲染在其下方甚至 below-fold——空报表是正常结果。这类导航意图不要求返回任何数值字段（不读 total_orders / total_revenue 等）。
*   **报表子类型 → URL 映射**(决定 `report_sales/<subtype>/filter` 的 subtype,务必按 intent 的报表名选对,否则会进错报表):**orders / sales order report → `sales`**(Reports › Sales › Orders);**tax report → `tax`**(Reports › Sales › Tax);invoiced → `invoiced`、shipping → `shipping`、refunds → `refunded`、coupons → `coupons`(均在 Reports › Sales › 对应子项)。
*   **相对日期换算**(报表 From/To,相对 intent 里给定的 today;**关键:"this year" 截到今天、不是年底**):
    *   **"last year"** = 去年**整年** → From `01/01/<去年>`、To `12/31/<去年>`。例:today=Mar 15 2023 → From `01/01/2022`、To `12/31/2022`。
    *   **"this year"** = 今年 1 月 1 日 → **今天**(当年未过完,To 取 today,**不要填 12/31**)。例:today=Mar 15 2023 的 "tax report for this year" → From `01/01/2023`、To `03/15/2023`。
    *   **显式区间**(intent 已给起止日期)= 直接用,不换算。例:"orders report from May 1 2021 to March 31 2022" → From `05/01/2021`、To `03/31/2022`。
*   **Product Reports (list)**: 产品类报表汇总（浏览量、畅销品、库存预警等）。
*   **Customer Reports (list)**: 客户类报表汇总（订单总额、新增账户、愿望清单等）。
*   **Marketing Reports (list)**: 营销类报表汇总（购物车放弃率、搜索词、邮件问题等）。
*   **Review Reports (list)**: 评论分析报表（按客户或产品统计）。
*   **Statistics Refresh (form/modal)**: 手动刷新报表统计数据的操作页。

### 系统配置 (Stores & System)
*   **Configuration (form)**: 全局系统配置中心，包含商店设置、税务、货币、属性集等。
*   **Attribute Sets (list/form)**: 产品属性集的定义与管理。
*   **Product Attributes (list/form)**: 全局产品属性及其可选值的管理入口。Configurations 生成组合时只能消费此前已经保存的属性选项；引入新的 Size/Color 值时，Product Attributes 的选项保存是前置资源阶段，完成后才能进入配置型父商品并保存其 Configurations 集合。两者是独立的持久化边界。
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
    *   `Sidebar` -> `Stores` -> `Attributes` -> `Product`: 进入 **Product Attributes**；选择属性后管理其全局选项。
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
