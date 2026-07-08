---
id: knowledge.browser.shopping_admin.admin_grid_controls
source_type: knowledge_section
platform: browser
app: shopping_admin
scope:
  - planner
  - replanner
selector_when: 在 Admin pages 中需要排序、分页、过滤、导出 CSV 或 XML 数据，以及调整 grid 列布局或保存 view 时
when: 在 Admin pages 中需要排序、分页、过滤、导出 CSV 或 XML 数据，以及调整 grid 列布局或保存 view 时
source: manual_distilled
confidence: medium
sensitivity: internal
ttl: session
version: 1
---
# Admin grid controls

Admin pages that manage data display a collection of records in a grid. The controls at the top of each column can be used to sort the data. The current sort order is indicated by an ascending or descending arrow in the column header. You can specify which columns appear in the grid, and drag them into different positions. You can also save different column arrangements as views that can be used later. The **Action** column lists operations that can be applied to an individual record. In addition, date from the current view of most grids can be exported to a CSV or XML file.

## Sort the list

1. Click any column header.

   The arrow indicates the current order as either ascending or descending.

1. Use the pagination controls to view additional pages in the collection.

## Paginate the list

1. Set the **Pagination** control to the number of records that you want to view per page.

1. Click **Next** and **Previous** to page through the list, or enter a specific **Page Number**.

## Filter the list

1. Click **Filters**.

1. Complete as many filters as necessary to describe the record you want to find.

1. Click **Apply Filters**.

**数值列的 From/To 过滤**：数值型列（如 Quantity、Price）展开时显示 **From** 和 **To** 两个输入框，表示范围区间。
- 要精确匹配某值（如库存恰好为 0），必须**同时填写 From=X 和 To=X**；只填 From=X 不填 To 表示「≥ X」，会匹配所有满足下界的行（可能是全量），不是精确匹配。
- 只填 To=X 不填 From 表示「≤ X」。

**重要（过滤器会被持久化）**：Magento admin 网格的过滤条件是**按用户持久化保存在数据库里**的、跨会话保留。也就是说，进入一个网格时它可能已带着之前设置过的过滤条件（初始环境状态，不必推测来源），导致看到的结果是被旧过滤限制后的子集——常见症状是莫名其妙的 `0 records found` / `We couldn't find any records.`。因此：
- 当任务要求**全量/不限某维度**，或网格出现**意料之外的空结果**时，**先点 `Clear all` / `Reset Filter` 把过滤器清空（reset）**，再判断数据是否真的为空或再设需要的过滤条件；
- 不要把"带着残留过滤的 0 条结果"当成"数据不存在"。

## Export data

1. Select the records that you want to export.

   **NOTE:**
   >
   >Product data cannot be exported from the grid. To learn more, see Export.

1. On the _Export_ () menu in the upper-right corner, choose one of the following file formats:

   - `CSV`
   - `Excel XML`

1. Click **Export**.

1. Look for the downloaded file of exported data at the location used for downloads by your browser.

## Grid Layout

The selection of columns and their order in the grid can be changed according to your preference, and saved as a _view_. You can control which attributes show in the grid under the individual attribute configuration. Having many attributes displayed in the product grid may affect admin load time and performance.

### Change the selection of columns

1. In the upper-right corner, click the _Columns_ () control.

1. Change the column selections:

   - Select the checkbox of any column that you want to add to the grid.
   - Clear the checkbox of any column that you want to remove from the grid.
   - To return the default grid view, click **Reset**.

  Make sure to scroll down to see all available columns.

**Columns 面板操作要点**：
- 面板底部只有两个按钮：**Cancel（取消所有改动并关闭）** 和 **Reset（恢复默认列集合）**，没有 Apply/Save 按钮（Filters 面板才有 Apply Filters，不要混淆）。
- 勾选/取消某列的复选框后，**网格立即更新**（无需点任何按钮），可实时看到效果。
- ⚠️ **勾选完目标列后通常根本不需要关闭面板**：网格已即时更新，目标列已出现在表头，面板开着也不影响后续读取。除非明确要求关闭，否则勾完直接进入下一步，不要自加「关闭面板」动作。
- ⚠️ **绝不点 Cancel**：Cancel 会**撤销**自面板打开以来的所有改动（包括刚勾的列），等同「取消操作」，不是关闭方式。
- ⚠️ 若确需关闭，点面板外空白区——但 Columns 面板是**覆盖在网格上的浮层**，空白区下方常是产品行/链接，容易误中跳到详情页；能不关就不关。
- **Reset** 恢复为默认列集合（移除用户自定义列）；比 Cancel 破坏性更大，基本不用。

### Move a column

1. Click the header of the column and hold.

1. Drag the column to the new position and release.

### Save a grid view

1. Click the _View_ () control.

1. Click **Save Current View**.

1. Enter a **name** for the view.

1. To save all changes, click the _Arrow_ ().

   The name of the view now appears as the current view.

### Change the grid view

1. Click the _View_ () control.

1. Do one of the following:

   - To use a different view, click the name of the view.
   - To change the name of a view, click the _Edit_ () icon and update the name.
   - To delete a view, click the _Edit_ () icon and then click the _Delete_ () icon.