"""Offline regression harness for the reviewed-Python orchestrator.

White-box mode exposes task fixtures to generation and review for mechanism
regression. Blind mode withholds them until the final source is frozen, then
uses them only for independent contract and execution evaluation.
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
import time
from collections import Counter
from dataclasses import asdict
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(PROJECT_ROOT / ".env")

from gui_agent.core.orchestrator import (  # noqa: E402
    FixtureSpec,
    generate_reviewed_code,
)
from gui_agent.core.orchestrator.sandbox import (  # noqa: E402
    execute_code,
    validate_fixture_contract,
)
from gui_agent.core.router import resolve_intent  # noqa: E402
from gui_agent.core.self_learning.app_summary import load_knowledge_for_app  # noqa: E402
from llm.structured import get_llm_call_count, get_llm_token_usage  # noqa: E402


DATASET = PROJECT_ROOT / "webarena-verified/assets/dataset/webarena-verified.json"
SUPPORTED_TASKS = frozenset({
    11, 62, 77, 94, 107, 183, 184, 193, 196, 202, 203, 208, 470, 500, 501,
    491, 543, 549, 550, 699, 704, 768, 771, 778,
})
HIDDEN_EXPECTED_RETURNS = {193: 182.4}


def fixture_for_task(task_id: int) -> FixtureSpec:
    if task_id == 11:
        rows = [
            {"id": "r1", "review": "I was disappointed by the fabric."},
            {"id": "r2", "review": "Disappointed with the fit."},
            {"id": "r3", "review": "The color left me disappointed."},
            {"id": "r4", "review": "Very disappointed; would not buy again."},
            {"id": "r5", "review": "My daughter was disappointed too."},
            {"id": "r6", "review": "Overall I am disappointed."},
            {"id": "r7", "review": "Comfortable and well made."},
            {"id": "r8", "review": "The sizing was surprising."},
        ]
        return FixtureSpec(lookups={
            "disappointed": rows,
            "reviews": rows,
            "all reviews": rows,
            "reviews that mention disappointed": rows,
        })
    if task_id == 62:
        rows = [
            {"id": "o1", "customer_email": "amy@example.test", "status": "Complete"},
            {"id": "o2", "customer_email": "bob@example.test", "status": "Complete"},
            {"id": "o3", "customer_email": "amy@example.test", "status": "Complete"},
            {"id": "o4", "customer_email": "cara@example.test", "status": "Complete"},
            {"id": "o5", "customer_email": "bob@example.test", "status": "Complete"},
            {"id": "o6", "customer_email": "amy@example.test", "status": "Complete"},
            {"id": "o7", "customer_email": "bob@example.test", "status": "Complete"},
            {"id": "o8", "customer_email": "amy@example.test", "status": "Pending"},
        ]
        return FixtureSpec(lookups={
            "orders": rows,
            "completed orders": rows,
            "customer emails who completed the most orders": rows,
            "customer email(s) who completed the most number of orders in the entire history": rows,
        })
    if task_id == 77:
        rows = [
            {"id": "r1", "status": "Pending", "title": "One"},
            {"id": "r2", "status": "Approved", "title": "Two"},
            {"id": "r3", "status": "Pending", "title": "Three"},
            {"id": "r4", "status": "Not Approved", "title": "Four"},
            {"id": "r5", "status": "Pending", "title": "Five"},
        ]
        pending = [row for row in rows if row["status"] == "Pending"]
        return FixtureSpec(
            lookups={
                "reviews": rows,
                "all reviews": rows,
                "pending reviews": pending,
                "total number of pending reviews": rows,
            },
            reads={row["id"]: {"status": row["status"]} for row in rows},
        )
    if task_id == 94:
        rows = [{
            "id": "invoice-1",
            "invoice": "000000001",
            "action_url": "/invoice/1",
            "grand_total": 36.39,
            "Grand Total (Purchased)": 36.39,
        }]
        return FixtureSpec(
            lookups={
                "000000001": rows,
                "invoice 000000001": rows,
                "invoices": rows,
            },
            reads={
                "invoice-1": {
                    "grand_total": 36.39,
                    "Grand Total (Purchased)": 36.39,
                },
            },
        )
    if task_id == 107:
        rows = [
            {"id": "o1", "status": "Complete", "purchase_date": "2022-05-03"},
            {"id": "o2", "status": "Complete", "purchase_date": "2022-05-21"},
            {"id": "o3", "status": "Complete", "purchase_date": "2022-07-04"},
            {"id": "o4", "status": "Complete", "purchase_date": "2022-08-12"},
            {"id": "o5", "status": "Complete", "purchase_date": "2022-10-01"},
            {"id": "o6", "status": "Complete", "purchase_date": "2022-10-28"},
            {"id": "o7", "status": "Complete", "purchase_date": "2022-12-15"},
            {"id": "o8", "status": "Pending", "purchase_date": "2022-06-02"},
            {"id": "o9", "status": "Complete", "purchase_date": "2023-01-02"},
        ]
        return FixtureSpec(lookups={
            "orders": rows,
            "completed orders": rows,
            "monthly completed orders": rows,
            "monthly count of completed orders from may 2022 through december 2022": rows,
        })
    if task_id == 183:
        rows = [
            {"id": "p1", "sku": "SKU-ALPHA", "quantity": 10, "name": "Alpha"},
            {"id": "p2", "sku": "SKU-BETA", "quantity": 9, "name": "Beta"},
            {"id": "p3", "sku": "SKU-GAMMA", "quantity": 10, "name": "Gamma"},
        ]
        return FixtureSpec(
            lookups={
                "products": rows,
                "catalog > products": rows,
                "products that have 10 units left": rows,
                "10 units left": rows,
            },
            reads={
                row["id"]: {
                    "SKU": row["sku"], "Quantity": row["quantity"],
                    "Type": "Simple Product",
                }
                for row in rows
            },
        )
    if task_id == 184:
        rows = [
            {
                "id": "p-zero", "sku": "MP02-33-Blue",
                "name": "Cronus Yoga Pant -33-Blue", "quantity": 0, "color": "Blue",
            },
            {
                "id": "p-one", "sku": "WT99-S-Red",
                "name": "Control Product S Red", "quantity": 1, "color": "Red",
            },
            {
                "id": "p-two", "sku": "WT98-M-Black",
                "name": "Another Product M Black", "quantity": 2, "color": "Black",
            },
        ]
        return FixtureSpec(
            lookups={
                "products": rows,
                "products that have 0 units left": rows,
                "0 units left": rows,
            },
            reads={
                row["id"]: {"quantity": row["quantity"], "color": row["color"]}
                for row in rows
            },
        )
    if task_id == 193:
        rows = [
            {
                "ID": "o-pending", "Status": "Pending",
                "Purchase Date": "Jun 11, 2023 9:00:00 AM",
                "Grand Total (Purchased)": "$900.00",
            },
            {
                "ID": "o-older", "Status": "Complete",
                "Purchase Date": "Sep 30, 2022 9:00:00 AM",
                "Grand Total (Purchased)": "$40.00",
            },
            {
                "ID": "o-latest", "Status": "Complete",
                "Purchase Date": "Jun 09, 2023 9:00:00 AM",
                "Grand Total (Purchased)": "$100.00",
            },
            {
                "ID": "o-canceled", "Status": "Canceled",
                "Purchase Date": "Jun 10, 2023 9:00:00 AM",
                "Grand Total (Purchased)": "$800.00",
            },
            {
                "ID": "o-second", "Status": "Complete",
                "Purchase Date": "May 31, 2023 9:00:00 AM",
                "Grand Total (Purchased)": "$82.40",
            },
        ]
        return FixtureSpec(lookups={
            "orders": rows,
            "orders list": rows,
            "completed orders": [
                row for row in rows if row["Status"] == "Complete"
            ],
            "last 2 completed orders": [
                row for row in rows if row["Status"] == "Complete"
            ],
        })
    if task_id == 196:
        rows = [
            {"id": "x-old", "status": "Canceled", "purchase_date": "Apr 01, 2023 9:00:00 AM", "Grand Total (Purchased)": 999.0},
            {"id": "d2", "status": "Complete", "purchase_date": "Jun 08, 2023 9:00:00 AM", "Grand Total (Purchased)": 105.75},
            {"id": "c1", "status": "Canceled", "purchase_date": "Jun 10, 2023 9:00:00 AM", "Grand Total (Purchased)": 150.0},
            {"id": "d4", "status": "Complete", "purchase_date": "Jun 06, 2023 9:00:00 AM", "Grand Total (Purchased)": 100.0},
            {"id": "c3", "status": "Canceled", "purchase_date": "Jun 07, 2023 9:00:00 AM", "Grand Total (Purchased)": 140.0},
            {"id": "d1", "status": "Complete", "purchase_date": "Jun 09, 2023 9:00:00 AM", "Grand Total (Purchased)": 100.0},
            {"id": "c4", "status": "Canceled", "purchase_date": "Jun 05, 2023 9:00:00 AM", "Grand Total (Purchased)": 150.0},
            {"id": "d-old", "status": "Complete", "purchase_date": "Apr 02, 2023 9:00:00 AM", "Grand Total (Purchased)": 999.0},
            {"id": "c2", "status": "Canceled", "purchase_date": "Jun 08, 2023 9:00:00 AM", "Grand Total (Purchased)": 160.0},
            {"id": "d3", "status": "Complete", "purchase_date": "Jun 07, 2023 8:00:00 AM", "Grand Total (Purchased)": 100.0},
        ]
        return FixtureSpec(lookups={
            "orders": rows,
            "cancelled orders": [row for row in rows if row["status"] == "Canceled"],
            "canceled orders": [row for row in rows if row["status"] == "Canceled"],
            "completed orders": [row for row in rows if row["status"] == "Complete"],
        })
    if task_id == 202:
        rows = [
            {"id": "o-old", "status": "canceled", "purchase_date": "May 02, 2023 7:30:00 PM", "created_at": "2023-05-02 19:30:00"},
            {"id": "o-new", "status": "canceled", "purchase_date": "May 23, 2023 8:15:00 AM", "created_at": "2023-05-23 08:15:00"},
            {"id": "o-other", "status": "complete", "purchase_date": "May 30, 2023 10:00:00 AM", "created_at": "2023-05-30 10:00:00"},
        ]
        canceled = [row for row in rows if row["status"] == "canceled"]
        return FixtureSpec(
            lookups={
                "orders": rows,
                "cancelled orders": canceled,
                "canceled orders": canceled,
                "most recent cancelled order": canceled,
                "most recent canceled order": canceled,
            },
            reads={row["id"]: row for row in rows},
        )
    if task_id == 203:
        rows = [
            {
                "id": "order-298", "order_id": "000000298", "status": "Pending",
                "purchase_date": "2023-05-20 09:00:00", "action_url": "/order/298",
            },
            {
                "id": "order-299", "order_id": "000000299", "status": "Pending",
                "purchase_date": "2023-05-31 16:45:00", "action_url": "/order/299",
            },
            {
                "id": "order-300", "order_id": "000000300", "status": "Complete",
                "purchase_date": "2023-06-02 11:30:00", "action_url": "/order/300",
            },
        ]
        return FixtureSpec(lookups={
            "orders": rows,
            "pending orders": rows,
            "most recent pending order": rows,
        })
    if task_id == 208:
        rows = [
            {
                "id": "c1", "phone": "+1 (205) 881-2302",
                "name": "Avery Stone", "email": "avery@example.test",
            },
        ]
        return FixtureSpec(lookups={
            "8812302": rows,
        })
    if task_id == 470:
        rows = [{
            "id": "order-302", "order_id": "302", "increment_id": "000000302",
            "status": "Pending", "action_url": "/order/302",
        }]
        return FixtureSpec(
            lookups={
                "#302": rows,
                "302": rows,
                "000000302": rows,
                "order #302": rows,
                "order 302": rows,
            },
            reads={"order-302": {"ID": "302", "Status": "Pending"}},
        )
    if task_id == 491:
        rows = [
            {
                "id": "sarah-pending-old", "order_id": "000000291",
                "customer_name": "Sarah Miller", "status": "Pending",
                "purchase_date": "2023-05-10 09:00:00",
                "created_at": "2023-05-10 09:00:00",
            },
            {
                "id": "sarah-pending-new", "order_id": "000000305",
                "customer_name": "Sarah Miller", "status": "Pending",
                "purchase_date": "2023-05-29 14:30:00",
                "created_at": "2023-05-29 14:30:00",
            },
            {
                "id": "sarah-complete-newer", "order_id": "000000309",
                "customer_name": "Sarah Miller", "status": "Complete",
                "purchase_date": "2023-05-31 11:00:00",
                "created_at": "2023-05-31 11:00:00",
            },
            {
                "id": "jane-pending-newest", "order_id": "000000310",
                "customer_name": "Jane Doe", "status": "Pending",
                "purchase_date": "2023-06-01 08:00:00",
                "created_at": "2023-06-01 08:00:00",
            },
        ]
        sarah = [row for row in rows if row["customer_name"] == "Sarah Miller"]
        return FixtureSpec(
            lookups={
                "orders": rows,
                "pending orders": rows,
                "sarah miller": sarah,
                "sarah miller in their most recent pending order": sarah,
                "most recent pending order": rows,
            },
            reads={row["id"]: row for row in rows},
        )
    if task_id == 500:
        order = [{
            "id": "order-301", "order_id": "301", "increment_id": "000000301",
            "status": "Processing", "action_url": "/order/301",
        }]
        return FixtureSpec(
            lookups={
                "#301": order,
                "order #301": order,
                "order 301": order,
                "301": order,
                "000000301": order,
            },
            reads={
                "order-301": {
                    "shipment_status": "Available", "tracking_numbers": [],
                    "order_id": "301", "status": "Processing",
                },
            },
        )
    if task_id == 699:
        return FixtureSpec()
    if task_id == 704:
        return FixtureSpec()
    if task_id == 771:
        rows = [
            {"id": "r1", "title": "Excellent", "status": "Pending"},
            {"id": "r2", "title": "Good", "status": "Pending"},
            {"id": "r3", "title": "Poor", "status": "Pending"},
            {"id": "r4", "title": "Already live", "status": "Approved"},
        ]
        return FixtureSpec(
            lookups={
                "reviews": rows,
                "all reviews": rows,
                "pending reviews": rows[:3],
            },
            reads={
                "r1": {"rating": 5, "detailed_rating": 5, "status": "Pending", "summary_of_review": "Excellent"},
                "r2": {"rating": 4, "detailed_rating": 4, "status": "Pending", "summary_of_review": "Good"},
                "r3": {"rating": 2, "detailed_rating": 2, "status": "Pending", "summary_of_review": "Poor"},
                "r4": {"rating": 5, "detailed_rating": 5, "status": "Approved", "summary_of_review": "Already live"},
            },
        )
    if task_id == 501:
        rows = [
            {
                "id": "taurus-parent", "name": "Taurus Elements Shell",
                "type": "Configurable Product", "sku": "WJ01",
            },
            {
                "id": "taurus-xs", "name": "Taurus Elements Shell XS Black",
                "type": "Simple Product", "sku": "WJ01-XS-Black",
            },
            {
                "id": "taurus-s", "name": "Taurus Elements Shell S Blue",
                "type": "Simple Product", "sku": "WJ01-S-Blue",
            },
        ]
        return FixtureSpec(
            lookups={
                "taurus elements shell": rows,
                "mark all taurus elements shell as out of stock": rows,
                "taurus": rows,
            },
            reads={
                "taurus-parent": {
                    "stock_status": "In Stock", "type": "Configurable Product",
                    "name": "Taurus Elements Shell",
                },
                "taurus-xs": {
                    "stock_status": "In Stock", "type": "Simple Product",
                    "name": "Taurus Elements Shell XS Black",
                },
                "taurus-s": {
                    "stock_status": "In Stock", "type": "Simple Product",
                    "name": "Taurus Elements Shell S Blue",
                },
            },
        )
    if task_id == 543:
        reviews = [
            {"id": "review-1", "title": "Excellent", "product": "Bella Tank", "Action": "/review/1", "action_url": "/review/1"},
            {"id": "review-2", "title": "Good", "product": "Bella Tank", "Action": "/review/2", "action_url": "/review/2"},
            {"id": "review-3", "title": "Not for me", "product": "Bella Tank", "Action": "/review/3", "action_url": "/review/3"},
        ]
        product = [
            {
                "id": "bella-parent", "name": "Bella Tank",
                "type": "Configurable Product", "sku": "WT01",
            },
            {
                "id": "bella-xs", "name": "Bella Tank XS Black",
                "type": "Simple Product", "sku": "WT01-XS-Black",
            },
            {
                "id": "bella-s", "name": "Bella Tank S Blue",
                "type": "Simple Product", "sku": "WT01-S-Blue",
            },
        ]
        return FixtureSpec(
            lookups={
                "bella tank reviews": reviews,
                "reviews for bella tank": reviews,
                "product reviews": reviews,
                "all reviews": reviews,
                "reviews": reviews,
                "review": reviews,
                "bella tank": product,
                "update the product description of bella tank": product,
            },
            reads={
                "review-1": {"rating": 5, "detailed_rating": 5, "title": "Excellent", "product": "Bella Tank"},
                "review-2": {"rating": 4, "detailed_rating": 4, "title": "Good", "product": "Bella Tank"},
                "review-3": {"rating": 3, "detailed_rating": 3, "title": "Not for me", "product": "Bella Tank"},
                "bella-parent": {
                    "short_description": "Old description", "type": "Configurable Product",
                    "name": "Bella Tank",
                },
                "bella-xs": {
                    "short_description": "", "type": "Simple Product",
                    "name": "Bella Tank XS Black",
                },
                "bella-s": {
                    "short_description": "", "type": "Simple Product",
                    "name": "Bella Tank S Blue",
                },
            },
        )
    if task_id == 549:
        product = {
            "id": "minerva-parent",
            "name": "Minerva LumaTech V-Tee",
            "type": "Configurable Product",
            "Name": "Minerva LumaTech V-Tee",
            "Type": "Configurable Product",
            "SKU": "WS08",
            "Action_url": "/catalog/product/edit/id/minerva-parent",
        }
        product_decoy = {
            "id": "other-parent",
            "name": "Other Fitness Tee",
            "type": "Configurable Product",
            "sku": "WS99",
        }
        attribute = {
            "id": "attribute-size",
            "name": "size",
            "action_url": "/catalog/product_attribute/edit/attribute_id/144",
        }
        return FixtureSpec(
            lookups={
                "size": [{
                    **attribute,
                    "attribute_code": "size",
                    "code": "size",
                    "default_label": "Size",
                }],
                "product attributes": [{
                    **attribute,
                    "attribute_code": "size",
                    "code": "size",
                    "default_label": "Size",
                }],
                "product attribute": [attribute],
                "attribute": [attribute],
                "minerva lumatech v-tee": [product],
                "minerva": [product],
                "products": [product, product_decoy],
            },
            reads={"attribute-size": {"options": []}},
        )
    if task_id == 550:
        attribute = {
            "id": "attribute-size",
            "name": "size",
            "attribute_code": "size",
            "code": "size",
            "label": "Size",
            "action_url": "/catalog/product_attribute/edit/attribute_id/144",
        }
        product = {
            "id": "nona-parent",
            "name": "Nona Fitness Tank",
            "type": "Configurable Product",
            "sku": "WT06",
            "action_url": "/catalog/product/edit/id/nona-parent",
        }
        product_decoy = {
            "id": "other-parent",
            "name": "Other Fitness Tank",
            "type": "Configurable Product",
            "sku": "WT99",
        }
        return FixtureSpec(
            lookups={
                "size": [attribute],
                "product attributes": [attribute],
                "product attribute": [attribute],
                "attribute": [attribute],
                "nona fitness tank": [product],
                "nona": [product],
                "products": [product, product_decoy],
            },
            reads={"attribute-size": {"options": []}},
        )
    if task_id == 768:
        rows = [
            {
                "id": "cronus-parent", "name": "Cronus Yoga Pant",
                "type": "Configurable Product", "sku": "MP12",
            },
            {
                "id": "cronus-33-blue", "name": "Cronus Yoga Pant 33 Blue",
                "type": "Simple Product", "sku": "MP12-33-Blue",
            },
            {
                "id": "cronus-32-blue", "name": "Cronus Yoga Pant 32 Blue",
                "type": "Simple Product", "sku": "MP12-32-Blue",
            },
            {
                "id": "cronus-33-black", "name": "Cronus Yoga Pant 33 Black",
                "type": "Simple Product", "sku": "MP12-33-Black",
            },
        ]
        return FixtureSpec(
            lookups={
                "5 blue cronus yoga pants with size 33": rows,
                "blue cronus yoga pants": rows,
                "cronus yoga pants": rows,
                "cronus": rows,
            },
            reads={
                "cronus-parent": {
                    "size": None, "color": None, "quantity": None,
                    "type": "Configurable Product",
                },
                "cronus-33-blue": {
                    "size": "33", "color": "Blue", "quantity": 7,
                    "type": "Simple Product",
                },
                "cronus-32-blue": {
                    "size": "32", "color": "Blue", "quantity": 11,
                    "type": "Simple Product",
                },
                "cronus-33-black": {
                    "size": "33", "color": "Black", "quantity": 9,
                    "type": "Simple Product",
                },
            },
        )
    if task_id == 778:
        rows = [
            {
                "id": "sahara-parent", "name": "Sahara Leggings",
                "type": "Configurable Product", "sku": "WP06",
            },
            {
                "id": "sahara-28-a", "name": "Sahara Leggings 28 A",
                "type": "Simple Product", "sku": "WP06-28-A",
            },
            {
                "id": "sahara-30", "name": "Sahara Leggings 30",
                "type": "Simple Product", "sku": "WP06-30",
            },
            {
                "id": "sahara-28-b", "name": "Sahara Leggings 28 B",
                "type": "Simple Product", "sku": "WP06-28-B",
            },
        ]
        return FixtureSpec(
            lookups={
                "size 28 sahara leggings": rows,
                "sahara leggings": rows,
                "sahara": rows,
            },
            reads={
                "sahara-parent": {
                    "size": None, "price": None, "type": "Configurable Product",
                    "name": "Sahara Leggings", "sku": "WP06",
                },
                "sahara-28-a": {
                    "size": "28", "price": 100.0,
                    "type": "Simple Product", "name": "Sahara Leggings 28 A",
                    "sku": "WP06-28-A",
                },
                "sahara-30": {
                    "size": "30", "price": 80.0,
                    "type": "Simple Product", "name": "Sahara Leggings 30",
                    "sku": "WP06-30",
                },
                "sahara-28-b": {
                    "size": "28", "price": 75.0,
                    "type": "Simple Product", "name": "Sahara Leggings 28 B",
                    "sku": "WP06-28-B",
                },
            },
        )
    raise ValueError(f"no coding fixture for task {task_id}")

def _evaluate_hidden_source(
    source: str,
    fixture: FixtureSpec,
    *,
    expected_return: Any = None,
) -> tuple[dict[str, Any], bool]:
    diagnostics = validate_fixture_contract(
        source,
        fixture,
        match_lookup_sources=True,
    )
    run = execute_code(source, fixture) if not diagnostics else None
    return_matches: bool | None = None
    if expected_return is not None and run is not None and run.ok:
        actual = run.return_value
        return_matches = bool(
            not isinstance(actual, bool)
            and isinstance(actual, (int, float))
            and abs(float(actual) - float(expected_return)) < 1e-9
        )
    result = {
        "diagnostics": [diagnostic.render() for diagnostic in diagnostics],
        "run_error": run.error if run is not None else "",
        "return_value": run.return_value if run is not None else None,
        "expected_return": expected_return,
        "return_matches": return_matches,
        "trace": [asdict(event) for event in run.trace] if run is not None else [],
        "writes": [asdict(write) for write in run.writes] if run is not None else [],
    }
    return result, bool(
        not diagnostics
        and run is not None
        and run.ok
        and return_matches is not False
    )


def _hidden_contract_failures(task_id: int, result: dict[str, Any]) -> list[str]:
    if task_id != 193:
        return []
    native_filter = any(
        event.get("op") == "query"
        and (event.get("kwargs", {}).get("filters") or {}).get("Status") == "Complete"
        for event in result.get("trace", [])
    )
    return [] if native_filter else ["HIDDEN_CONTRACT:NATIVE_FILTER"]


def _coding_sample(
    task: dict,
    knowledge: str,
    resolution: Any,
    *,
    coding_eval_mode: str = "whitebox",
) -> dict[str, Any]:
    fixture = fixture_for_task(task["task_id"])
    plan = generate_reviewed_code(
        task["intent"],
        knowledge=knowledge,
        resolution=resolution,
        current_site="shopping_admin",
        fixture=fixture if coding_eval_mode == "whitebox" else None,
    )
    final = plan.attempts[-1]
    trace = final.run.trace if final.run is not None else []
    hidden: dict[str, Any] | None = None
    hidden_ok = True
    hidden_contract_failures: list[str] = []
    first_executable = bool(
        plan.attempts
        and not plan.attempts[0].diagnostics
        and plan.attempts[0].run is not None
        and plan.attempts[0].run.ok
    )
    if coding_eval_mode == "blind":
        expected_return = HIDDEN_EXPECTED_RETURNS.get(task["task_id"])
        hidden, hidden_ok = _evaluate_hidden_source(
            plan.source,
            fixture,
            expected_return=expected_return,
        )
        hidden_contract_failures = _hidden_contract_failures(
            task["task_id"], hidden,
        )
        first_source = plan.attempts[0].source
        if first_source == plan.source:
            first_executable = hidden_ok and not hidden_contract_failures
        else:
            first_hidden, first_executable = _evaluate_hidden_source(
                first_source,
                fixture,
                expected_return=expected_return,
            )
            first_executable = (
                first_executable
                and not _hidden_contract_failures(task["task_id"], first_hidden)
            )

    executable = plan.executable and hidden_ok and not hidden_contract_failures
    failures = [] if executable else ["CODING_NOT_EXECUTABLE"]
    failures.extend(hidden_contract_failures)
    if hidden is not None and hidden["diagnostics"]:
        failures.extend(
            f"HIDDEN_FIXTURE:{diagnostic.split(']', 1)[0].lstrip('[')}"
            for diagnostic in hidden["diagnostics"]
        )
    elif hidden is not None and not hidden_ok:
        failures.append(
            "HIDDEN_FIXTURE:RETURN"
            if hidden["return_matches"] is False
            else "HIDDEN_FIXTURE:RUNTIME"
        )
    if not plan.requirements_satisfied:
        failures.append("PROMPT_REQUIREMENTS_NOT_SATISFIED")
    reviews = [plan.review] if plan.review is not None else []
    return {
        "ok": not failures,
        "executable": executable,
        "requirements_satisfied": plan.requirements_satisfied,
        "evaluation_mode": (
            "whitebox_regression"
            if coding_eval_mode == "whitebox"
            else "blind_generalization"
        ),
        "review_fixture_visible": coding_eval_mode == "whitebox",
        "hidden_evaluation": hidden,
        "first_executable": first_executable,
        "failures": failures,
        "calls": 1 + len(reviews),
        "repairs": int(plan.repaired),
        "input_tokens": (
            sum(attempt.input_tokens for attempt in plan.attempts)
            + sum(review.input_tokens for review in reviews)
        ),
        "output_tokens": (
            sum(attempt.output_tokens for attempt in plan.attempts)
            + sum(review.output_tokens for review in reviews)
        ),
        "seconds": round(
            sum(attempt.seconds for attempt in plan.attempts)
            + sum(review.seconds for review in reviews),
            3,
        ),
        "source": plan.source,
        "review": asdict(plan.review) if plan.review is not None else None,
        "attempts": [
            {
                "source": attempt.source,
                "diagnostics": [diagnostic.render() for diagnostic in attempt.diagnostics],
                "run_error": attempt.run.error if attempt.run is not None else "",
                "trace": [asdict(event) for event in attempt.run.trace]
                if attempt.run is not None else [],
                "writes": [asdict(write) for write in attempt.run.writes]
                if attempt.run is not None else [],
                "final_state": attempt.run.final_state
                if attempt.run is not None else {},
            }
            for attempt in plan.attempts
        ],
        "trace": [asdict(event) for event in trace],
        "writes": [
            asdict(write)
            for write in (final.run.writes if final.run is not None else [])
        ],
        "final_state": final.run.final_state if final.run is not None else {},
        "diagnostics": [
            diagnostic.render() for attempt in plan.attempts for diagnostic in attempt.diagnostics
        ],
        "run_error": final.run.error if final.run is not None else "",
    }


def _surface_summary(samples: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "samples": len(samples),
        "semantic_passes": sum(sample["ok"] for sample in samples),
        "executable_rate": sum(sample["executable"] for sample in samples) / len(samples),
        "first_executable_rate": sum(sample["first_executable"] for sample in samples) / len(samples),
        "mean_calls": sum(sample["calls"] for sample in samples) / len(samples),
        "mean_output_tokens": sum(sample["output_tokens"] for sample in samples) / len(samples),
        "median_seconds": statistics.median(sample["seconds"] for sample in samples),
    }


def coding_verdict(
    summary: dict[str, Any],
    per_task_passes: dict[int, int],
    *,
    samples_per_task: int,
) -> dict[str, bool]:
    verdict = {
        "coding_functionally_viable": (
            summary["executable_rate"] >= 0.8
            and all(count >= 1 for count in per_task_passes.values())
        ),
    }
    if samples_per_task >= 3:
        required_passes = max(2, (samples_per_task * 3 + 4) // 5)
        verdict["coding_stability_gate"] = (
            summary["executable_rate"] >= 0.8
            and all(count >= required_passes for count in per_task_passes.values())
        )
    return verdict


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tasks", nargs="+", type=int, default=[549, 778])
    parser.add_argument("--k", type=int, default=5)
    parser.add_argument(
        "--coding-eval-mode",
        choices=["whitebox", "blind"],
        default="blind",
        help=(
            "whitebox exposes task fixtures to generation/review for regression; "
            "blind reveals them only after the final code is frozen"
        ),
    )
    args = parser.parse_args()
    unsupported = set(args.tasks) - SUPPORTED_TASKS
    if unsupported:
        parser.error(f"no private fixtures for task ids {sorted(unsupported)}")

    dataset = json.loads(DATASET.read_text(encoding="utf-8"))
    by_id = {task["task_id"]: task for task in dataset}
    tasks = [by_id[task_id] for task_id in args.tasks]
    knowledge = load_knowledge_for_app("shopping_admin", "browser")
    output_dir = PROJECT_ROOT / "logs/coding_orchestrator_eval" / time.strftime("%Y%m%d_%H%M%S")
    output_dir.mkdir(parents=True, exist_ok=True)
    results: list[dict[str, Any]] = []
    failures: Counter[str] = Counter()

    for task in tasks:
        resolution = resolve_intent(task["intent"])
        task_knowledge = knowledge.decompose_context(task["intent"]) if knowledge else ""
        row = {"task_id": task["task_id"], "intent": task["intent"]}
        samples = []
        for sample_index in range(args.k):
            sample = _coding_sample(
                task,
                task_knowledge,
                resolution,
                coding_eval_mode=args.coding_eval_mode,
            )
            samples.append(sample)
            failures.update(sample["failures"])
            mark = "✓" if sample["ok"] else "✗"
            print(
                f"[{task['task_id']} {sample_index + 1}/{args.k}] {mark} "
                f"calls={sample['calls']} out={sample['output_tokens']} "
                f"fail={sample['failures'][:2]}",
                flush=True,
            )
        row["samples"] = samples
        row["summary"] = _surface_summary(samples)
        results.append(row)
        (output_dir / "report.json").write_text(
            json.dumps({"tasks": results}, ensure_ascii=False, indent=1), encoding="utf-8",
        )

    samples = [sample for row in results for sample in row["samples"]]
    summary = _surface_summary(samples)
    per_task_passes = {
        row["task_id"]: sum(sample["ok"] for sample in row["samples"])
        for row in results
    }
    verdict = coding_verdict(
        summary,
        per_task_passes,
        samples_per_task=args.k,
    )
    report = {
        "coding_eval_mode": args.coding_eval_mode,
        "summary": summary,
        "verdict": verdict,
        "failure_codes": dict(failures.most_common()),
        "tasks": results,
    }
    (output_dir / "report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8",
    )
    print(json.dumps({"summary": summary, "verdict": verdict}, ensure_ascii=False, indent=2))
    print(f"report -> {output_dir / 'report.json'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
