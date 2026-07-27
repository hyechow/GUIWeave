---
id: knowledge.browser.shopping_admin.moderate_product_reviews
source_type: knowledge_section
platform: browser
app: shopping_admin
scope:
  - decompose
  - orchestrator
  - planner
  - replanner
selector_when: 当需要使用 product reviews、rating/stars、Product association、Nickname、Review Summary 或评论修改能力时查阅本节
when: 当需要使用 product reviews、rating/stars、Product association、Nickname、Review Summary 或评论修改能力时查阅本节
source: manual_distilled
confidence: medium
sensitivity: internal
ttl: session
version: 14
---
# Moderate product reviews

For Commerce product reviews, a submitted product review must be approved before it can be displayed. This ensures that reviews are appropriate for public display your store. A submitted review is in a `Pending` status until it is approved or rejected.

## Review data surfaces

**Marketing > User Content > All Reviews** is the complete review-record source. Its grid exposes
the Product association, Title, Nickname, Review text, and an Action link to each review detail. Product-
scoped review queries bind their search/filter to the grid's **Product** field rather than looking
for reviews from the Products list.

The review detail owns **Detailed Rating** and the editable **Nickname** and **Summary of Review**
fields. Nickname is also directly available in the All Reviews grid. When the requested title is
available in the grid, use **Title**; on the detail form, the corresponding review-title field is
**Summary of Review**, not Nickname and not the page heading.
Therefore a complete review collection may take Product, Title, Nickname, Review, and the Action URL from
the grid, but it cannot declare Rating as a grid row field. Rating-dependent analysis must obtain
Detailed Rating from each row's linked review-detail resource before aggregating.

## Planning boundary

**All Reviews** is the exact review collection literal; `Reviews` is not an alias.

- Filter: **Product**
- Query fields: **Action**, **Nickname**, **Title**, **Review**
- Detail-only field: **Detailed Rating** (`number`), unavailable to `query`

**Action** is the row locator required when a queried review feeds a detail `read`.
For rating comparison, query **Action**, then read every row with
`fields={"Detailed Rating": "number"}`. Never put **Detailed Rating** in query fields or filters.
For a review-only read or aggregation, a product mention binds directly to the **Product** filter
on **All Reviews**; do not pre-query **Products**. Include **Nickname** in the review query whenever
it is a requested output. A separate **Products** lookup is needed only when the goal also mutates
the product-owned resource itself.

Product fields owned by **Products** include **Name**, **Type**, and
**Short Description**; configurable parent products use Type value
`Configurable Product`. A Short Description mutation filters both the full-name and fallback
Products queries by that Type, asserts one filtered owner, and commits once.
<!-- /planning-boundary -->

## View product reviews in the Admin

To view all reviews for a specific product in the Admin, do the following:

1. On the _Admin_ sidebar, go to **Catalog** > **Products**.

1. Find the product that you want to view and click **Edit** in the _Action_ column.

1. On the product page, scroll down and expand  the **Product Reviews** section.

   In this grid, you can also change the specific review by clicking the **Edit** link in the _Action_ column.

## Update status for reviews

1. On the _Admin_ sidebar, go to **Marketing** > _User Content_ > **Pending Reviews** or **All Reviews**.

1. In the list, click a pending review to view the details and edit if necessary.

1. Change the **Status** according to your assessment:

   - To approve a pending review, select `Approved`.

   - To reject a review, select `Not Approved`. Unapproved reviews disappear from the list of _Pending Reviews_ page.

   **NOTE:**
   >
   >Reviews with the `Pending` and `Not Approved` statuses are not displayed on the storefront.

1. If applicable, set the **Visibility** of a product review for appearing in different store views.

1. If needed, change the values for **Detailed Rating**, **Nickname**, and **Summary of Review**.

   To change the store view where a review is available, choose the needed store view in the _Visibility_ column.

1. When complete, click **Save Review**.

## Batch update

You can update or delete multiple reviews at the same time:

1. On the _Admin_ sidebar, go to **Marketing** > _User Content_ > **All Reviews**.

1. Select the reviews that you want to update.

1. Use the _Action_ selector at the top-left corner to apply an action.

1. Click **Submit**

## Delete a product review

1. On the _Admin_ sidebar, go to **Marketing** > _User Content_ > **All Reviews**.

1. Find the product review to be deleted and open it in edit mode.

1. In the menu bar, click **Delete Review** button.

1. To confirm the action, click **OK**.

## Button bar

| Button   | Description  |
|----------|--------------|
| **Back** | Returns to the Reviews page without saving changes |
| **Delete Review** | Deletes the review |
| **Reset** | Resets any unsaved changes in the review form to their previous values |
| **Previous** | Opens the previous review |
| **Next** | Opens the next review |
| **Save and Previous** | Saves current changes and opens the previous review. This button is displayed if there are other reviews. |
| **Save and Next** | Saves the current changes and opens the next view. This button is displayed if there are other reviews. |
| **Save Review** | Saves changes and closes the review edit page |
