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

**重要（过滤器会被持久化）**：Magento admin 网格的过滤条件是**按用户持久化保存在数据库里**的，会**跨会话/跨任务残留**。也就是说，进入一个网格时它可能仍带着上一次（甚至上一个任务）设过的过滤条件，导致看到的结果是被旧过滤限制后的子集——常见症状是莫名其妙的 `0 records found` / `We couldn't find any records.`。因此：
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