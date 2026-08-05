---
id: knowledge.browser.shopping_admin.pages
source_type: knowledge_section
platform: browser
app: shopping_admin
scope:
  - planner
  - replanner
selector_when: 当需要查找或编辑 CMS Page/Pages、页面标题/Page Title、Home Page 或 Privacy Policy 时查阅本节
when: 当需要查找或编辑 CMS Page/Pages、页面标题/Page Title、Home Page 或 Privacy Policy 时查阅本节
source: manual_curated
confidence: high
sensitivity: internal
ttl: session
version: 4
---
# Pages

## Pages workspace

The CMS page collection is under **Content > Elements > Pages**. Each grid row is one
page record. The grid supports exact **Title** filtering, and its **Title** column is the stable
lookup field used to distinguish
records such as `Home Page`, `Privacy Policy`, and other content pages.

The collection also exposes page status, identifier, store view, layout, and update-time
metadata. These values describe the page record but are not aliases for its title.

## Page editor fields

Opening a page row enters its editor. **Page Title** is the browser-facing title value
owned by that page. It is distinct from the grid lookup field **Title**, from the
content heading rendered inside the page body, and from the URL key.

Other editor resources include Content, Content Heading, URL Key, Store View, Layout,
Meta Title, Meta Keywords, and Meta Description. Updating one of these fields does not
implicitly change the others. Saving persists changes on the selected page record.
