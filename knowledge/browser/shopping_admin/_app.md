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
*   **Orders List (list)**: 所有销售订单的主列表，按状态追踪订单进度。对于按订单历史统计 customer email(s) 的聚合任务（如 completed/any-state orders、most/second/fifth number of orders、have N orders），这是权威 UI 数据源：进入 **Sales > Orders**；completed 口径按需筛选 `Status = Complete`；any-state 口径必须先确保没有 `Active filters`（若看到 `Clear all` 就点击清除，避免继承上次任务的 `Status: Complete`）。收集/导出完整订单行中的 `Customer Email`/`Status`，再用 data_query 做 group/count/rank/tie。不要优先走 Customer Reports 或 Customers grid 的 `Total Orders` 列。
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
*   **Sales Reports (list)**: 销售类报表汇总（订单、税收、发票、运费等）。
*   **Product Reports (list)**: 产品类报表汇总（浏览量、畅销品、库存预警等）。
*   **Customer Reports (list)**: 客户类报表汇总（订单总额、新增账户、愿望清单等）。
*   **Marketing Reports (list)**: 营销类报表汇总（购物车放弃率、搜索词、邮件问题等）。
*   **Review Reports (list)**: 评论分析报表（按客户或产品统计）。
*   **Statistics Refresh (form/modal)**: 手动刷新报表统计数据的操作页。

### 系统配置 (Stores & System)
*   **Configuration (form)**: 全局系统配置中心，包含商店设置、税务、货币、属性集等。
*   **Attribute Sets (list/form)**: 产品属性集的定义与管理。
*   **Extensions Marketplace (external link)**: 合作伙伴与扩展程序市场入口。

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
    `Admin Dashboard` -> `Sidebar (Reports)` -> `Reports Menu` -> `Sales` -> `Orders Report` (选择日期范围) -> `Show Report` -> `Export` (下载 CSV/Excel)。

5.  **审核用户评论**:
    `Sidebar (Marketing)` -> `User Content` -> `Pending Reviews` -> `List` -> `Click Review` -> `Moderate Product Reviews` (Status: Approved/Not Approved) -> `Save Review`。
