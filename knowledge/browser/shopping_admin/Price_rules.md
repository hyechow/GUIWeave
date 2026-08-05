---
id: knowledge.browser.shopping_admin.price_rules
source_type: knowledge_section
platform: browser
app: shopping_admin
scope:
  - decompose
  - planner
  - replanner
selector_when: 当需要创建或编辑 Cart Price Rule、Catalog Price Rule、折扣规则及客户组适用范围时查阅本节
when: 当需要创建或编辑 Cart Price Rule、Catalog Price Rule、折扣规则及客户组适用范围时查阅本节
source: manual_curated
confidence: medium
sensitivity: internal
ttl: session
version: 4
---
# Price rule capabilities

Magento provides two independent promotion resources. **Cart Price Rules** apply to carts,
checkout, an order, or a purchase. **Catalog Price Rules** apply to products in the catalog.
"On all products" therefore selects a Catalog Price Rule, while "on the customer's cart" selects
a Cart Price Rule. Their forms and persistence endpoints are different resources.

## Cart Price Rule form

The form is under **Marketing > Cart Price Rules > Add New Rule**. Rule Name, Active, Websites,
Customer Groups, and Coupon are required business fields. `Main Website` is the available website
in this environment. Customer Groups is a multi-select with `NOT LOGGED IN`, `General`, `Wholesale`,
and `Retailer`; it has no literal `All Customers` option. Registered customers correspond to
General, Wholesale, and Retailer, excluding NOT LOGGED IN.

The expandable **Actions** section owns Apply and Discount Amount. Percentage discounts use
`Percent of product price discount`; fixed discounts use the corresponding fixed-amount option.
Discount Amount receives the numeric value without `%` or currency symbols.

## Catalog Price Rule form

The form is under **Marketing > Catalog Price Rules > Add New Rule**. Rule Name, Active, Websites,
and Customer Groups are required; there is no Coupon field. An empty Conditions collection means
the rule applies to all products. The Actions section owns Apply and Discount Amount, with
`Apply as percentage of original` and `Apply as fixed amount` as the relevant operation families.
The same website and customer-group semantics apply as on Cart Price Rules.
