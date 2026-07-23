from __future__ import annotations

from gui_agent.core.coding_orchestrator import TraceEvent, execute_code
from gui_agent.core.orchestrator import Program
from gui_agent.core.orchestrator.decomposer import _PlanDraft, _StepDraft, to_program
from gui_agent.core.orchestrator.program import ObservationBinding, OutputSpec, ValueRef
from scripts.coding_orchestrator_eval import (
    coding_verdict,
    fixture_for_task,
    grade_coding_trace,
    grade_dsl_program,
)


def test_coding_verdict_separates_functional_and_stability_gates() -> None:
    summary = {"executable_rate": 1.0}

    single = coding_verdict(summary, {778: 1}, samples_per_task=1)
    stable = coding_verdict(summary, {778: 3}, samples_per_task=5)

    assert single == {"coding_functionally_viable": True}
    assert stable == {
        "coding_functionally_viable": True,
        "coding_stability_gate": True,
    }


def test_task_778_coding_trace_requires_complete_detail_guarded_mutations() -> None:
    source = '''
def run(ctx):
    scope = ctx.lookup("size 28 Sahara leggings", field="name", fallback="Sahara")
    products = ctx.acquire(scope, fields=["sku", "name", "type"], coverage="complete")
    assert products, "Sahara candidates must exist"
    targets = []
    for product in products:
        if product["type"] != "Simple Product":
            continue
        state = ctx.read(product, fields=["size", "price"])
        if state["size"] != "28":
            continue
        price = round(state["price"] * 0.865, 2)
        assert price == round(state["price"] * 0.865, 2), "discount must be rounded to two decimals"
        targets.append((product, price))
    assert targets, "at least one size 28 Sahara variant must exist"
    for product, price in targets:
        saved = ctx.interact("save discounted price", success="price saved", target=product, values={"price": price}, persistence="explicit_commit")
        assert saved, "discounted price must be saved"
'''
    run = execute_code(source, fixture_for_task(778))

    assert run.ok, run.error
    assert grade_coding_trace(778, run.trace) == []


def test_task_62_coding_result_requires_complete_tie_aware_aggregation() -> None:
    source = '''
def run(ctx):
    scope = ctx.lookup("orders")
    orders = ctx.acquire(scope, fields=["status", "customer_email"], coverage="complete")
    assert orders, "the complete order history must be available"
    counts = {}
    for order in orders:
        if order["status"] != "Complete":
            continue
        email = order["customer_email"]
        counts[email] = counts.get(email, 0) + 1
    assert counts, "at least one completed order must exist"
    highest = max(counts.values())
    winners = sorted([email for email, count in counts.items() if count == highest])
    assert winners, "the tied top customers must be retained"
    return winners
'''
    run = execute_code(source, fixture_for_task(62))

    assert run.ok, run.error
    assert grade_coding_trace(62, run.trace, run.return_value) == []


def test_task_501_coding_trace_requires_one_parent_owned_stock_mutation() -> None:
    source = '''
def run(ctx):
    scope = ctx.lookup("Taurus Elements Shell")
    products = ctx.acquire(scope, fields=["id", "name", "type"], coverage="complete")
    parents = [p for p in products if p["type"] == "Configurable Product"]
    assert len(parents) == 1, "the configurable product owner must be unique"
    saved = ctx.interact(
        "mark Taurus Elements Shell out of stock",
        success="Taurus Elements Shell stock status is saved as Out of Stock",
        target=parents[0],
        values={"stock_status": "Out of Stock"},
        persistence="explicit_commit",
    )
    assert saved, "the parent stock status must be saved"
'''
    run = execute_code(source, fixture_for_task(501))

    assert run.ok, run.error
    assert grade_coding_trace(501, run.trace) == []


def test_task_543_coding_trace_requires_complete_ratings_and_parent_write() -> None:
    source = '''
def run(ctx):
    review_scope = ctx.lookup("Bella Tank reviews")
    reviews = ctx.acquire(review_scope, fields=["id", "title"], coverage="complete")
    assert reviews, "Bella Tank reviews must be available"
    count = 0
    for review in reviews:
        state = ctx.read(review, fields=["rating"])
        if state["rating"] >= 4:
            count += 1
    description = f"{count} customer(s) love it!" if count else "don't miss out on this amazing product"
    product_scope = ctx.lookup("Bella Tank")
    products = ctx.acquire(product_scope, fields=["id", "name", "type"], coverage="complete")
    parents = [p for p in products if p["type"] == "Configurable Product"]
    assert len(parents) == 1, "the Bella Tank parent must be unique"
    saved = ctx.interact(
        "update Bella Tank product description",
        success=f"Bella Tank Short Description is saved as {description}",
        target=parents[0],
        values={"short_description": description},
        persistence="explicit_commit",
    )
    assert saved, "the computed product description must be saved"
'''
    run = execute_code(source, fixture_for_task(543))

    assert run.ok, run.error
    assert grade_coding_trace(543, run.trace) == []


def test_unseen_read_only_task_graders_accept_complete_programs() -> None:
    sources = {
        77: '''
def run(ctx):
    rows = ctx.acquire(ctx.lookup("reviews"), fields=["status"], coverage="complete")
    assert rows, "reviews must be available"
    return sum(1 for row in rows if row["status"] == "Pending")
''',
        183: '''
def run(ctx):
    rows = ctx.acquire(ctx.lookup("products"), fields=["sku", "quantity"], coverage="complete")
    assert rows, "products must be available"
    return [row["sku"] for row in rows if row["quantity"] == 10]
''',
        208: '''
def run(ctx):
    rows = ctx.acquire(ctx.lookup("8812302"), fields=["name", "email"], coverage="complete")
    assert rows, "the customer must be found"
    return [{"name": row["name"], "email": row["email"]} for row in rows]
''',
    }
    for task_id, source in sources.items():
        run = execute_code(source, fixture_for_task(task_id))
        assert run.ok, run.error
        assert grade_coding_trace(task_id, run.trace, run.return_value) == []


def test_task_77_grader_accepts_complete_pending_reviews_scope() -> None:
    source = '''
def run(ctx):
    rows = ctx.acquire(ctx.lookup("Pending Reviews"), fields=["ID"], coverage="complete")
    assert len(rows) >= 0, "pending review scope must be readable"
    return len(rows)
'''
    run = execute_code(source, fixture_for_task(77))

    assert run.ok, run.error
    assert grade_coding_trace(77, run.trace, run.return_value) == []


def test_task_183_grader_accepts_complete_detail_quantity_reads() -> None:
    source = '''
def run(ctx):
    rows = ctx.acquire(ctx.lookup("products"), fields=["SKU"], coverage="complete")
    assert rows, "products must be available"
    result = []
    for row in rows:
        state = ctx.read(row, fields=["Quantity"])
        if state["Quantity"] == 10:
            result.append(row["SKU"])
    assert result, "products with ten units must exist"
    return result
'''
    run = execute_code(source, fixture_for_task(183))

    assert run.ok, run.error
    assert grade_coding_trace(183, run.trace, run.return_value) == []


def test_second_unseen_batch_read_only_graders_accept_complete_programs() -> None:
    sources = {
        11: '''
def run(ctx):
    rows = ctx.acquire(ctx.lookup("reviews"), fields=["review"], coverage="complete")
    assert rows, "review history must be available"
    return sum(1 for row in rows if "disappointed" in row["review"].casefold())
''',
        94: '''
def run(ctx):
    rows = ctx.acquire(ctx.lookup("invoice 000000001"), fields=["invoice", "grand_total"], coverage="complete")
    matches = [row for row in rows if row["invoice"] == "000000001"]
    assert len(matches) == 1, "invoice 000000001 must be unique"
    total = None
    for row in matches:
        total = row["grand_total"]
    assert total is not None, "invoice grand total must be available"
    return total
''',
        184: '''
def run(ctx):
    rows = ctx.acquire(ctx.lookup("products"), fields=["name", "quantity", "color"], coverage="complete")
    assert rows, "product inventory must be available"
    result = [
        {"name": row["name"], "color": row["color"]}
        for row in rows if row["quantity"] == 0
    ]
    assert result, "at least one zero-quantity product must exist"
    return result
''',
        203: '''
def run(ctx):
    rows = ctx.acquire(ctx.lookup("orders"), fields=["status", "purchase_date", "order_id"], coverage="complete")
    pending = [row for row in rows if row["status"] == "Pending"]
    assert pending, "at least one pending order must exist"
    latest_date = max(row["purchase_date"] for row in pending)
    latest = []
    for row in pending:
        if row["purchase_date"] == latest_date:
            latest.append({"purchase_date": row["purchase_date"][:10], "order_id": row["order_id"]})
    assert len(latest) == 1, "the most recent pending order must be unique"
    return latest
''',
    }
    for task_id, source in sources.items():
        run = execute_code(source, fixture_for_task(task_id))
        assert run.ok, run.error
        assert grade_coding_trace(task_id, run.trace, run.return_value) == []


def test_task_184_grader_accepts_complete_quantity_then_candidate_color_read() -> None:
    source = '''
def run(ctx):
    rows = ctx.acquire(ctx.lookup("products"), fields=["Name", "Quantity"], coverage="complete")
    assert rows, "products must be available"
    result = []
    for row in rows:
        if row["Quantity"] != 0:
            continue
        state = ctx.read(row, fields=["Color"])
        result.append({"name": row["Name"], "color": state["Color"]})
    assert result, "zero-quantity products must exist"
    return result
'''
    run = execute_code(source, fixture_for_task(184))

    assert run.ok, run.error
    assert grade_coding_trace(184, run.trace, run.return_value) == []


def test_second_unseen_batch_mutation_graders_accept_business_boundaries() -> None:
    sources = {
        470: '''
def run(ctx):
    rows = ctx.acquire(ctx.lookup("order #302"), fields=["id", "status"], coverage="complete")
    targets = [row for row in rows if row["status"] == "Pending"]
    assert len(targets) == 1, "pending order 302 must be unique"
    for target in targets:
        saved = ctx.interact(
            "cancel order 302",
            success="order 302 status is Canceled",
            target=target,
            values={"status": "Canceled"},
            persistence="explicit_commit",
        )
        assert saved, "order 302 must be canceled"
''',
        699: '''
def run(ctx):
    saved = ctx.interact(
        "create Cart Price Rule spring sale",
        success="spring sale cart price rule is active with a 20 percent discount",
        values={
            "name": "spring sale",
            "website": "Main Website",
            "customer_groups": ["General"],
            "coupon": "No Coupon",
            "apply": "Percent of product price discount",
            "discount_amount": 20,
            "active": "Yes",
        },
        persistence="explicit_commit",
    )
    assert saved, "spring sale price rule must be saved"
''',
    }
    for task_id, source in sources.items():
        run = execute_code(source, fixture_for_task(task_id))
        assert run.ok, run.error
        assert grade_coding_trace(task_id, run.trace, run.return_value) == []


def test_task_470_grader_accepts_status_read_from_concrete_order() -> None:
    source = '''
def run(ctx):
    rows = ctx.acquire(ctx.lookup("302"), fields=["ID"], coverage="complete")
    assert len(rows) == 1, "order 302 must be unique"
    for order in rows:
        state = ctx.read(order, fields=["Status"])
        assert state["Status"] == "Pending", "order 302 must be cancelable"
        saved = ctx.interact(
            "cancel order 302",
            success="order 302 status is Canceled",
            target=order,
            values={"Status": "Canceled"},
            persistence="explicit_commit",
        )
        assert saved, "order 302 must be canceled"
'''
    run = execute_code(source, fixture_for_task(470))

    assert run.ok, run.error
    assert grade_coding_trace(470, run.trace, run.return_value) == []


def test_task_107_grader_accepts_complete_zero_filled_months() -> None:
    source = '''
def run(ctx):
    rows = ctx.acquire(ctx.lookup("orders"), fields=["status", "purchase_date"], coverage="complete")
    assert rows, "orders must be available"
    months = [
        ("2022-05", "May"), ("2022-06", "June"), ("2022-07", "July"),
        ("2022-08", "August"), ("2022-09", "September"),
        ("2022-10", "October"), ("2022-11", "November"), ("2022-12", "December"),
    ]
    counts = {key: 0 for key, name in months}
    for row in rows:
        key = row["purchase_date"][:7]
        if row["status"] == "Complete" and key in counts:
            counts[key] += 1
    result = [{"month": name, "count": counts[key]} for key, name in months]
    assert len(result) == 8, "every requested month must be returned"
    return result
'''
    run = execute_code(source, fixture_for_task(107))

    assert run.ok, run.error
    assert grade_coding_trace(107, run.trace, run.return_value) == []


def test_new_frozen_read_only_graders_accept_derived_results() -> None:
    sources = {
        196: '''
def run(ctx):
    import datetime
    rows = ctx.acquire(ctx.lookup("orders"), fields=["status", "purchase_date", "Grand Total (Purchased)"], coverage="complete")
    canceled = []
    completed = []
    for row in rows:
        stamp = datetime.datetime.strptime(row["purchase_date"], "%b %d, %Y %I:%M:%S %p")
        if row["status"].casefold() == "canceled":
            canceled.append((stamp, row["Grand Total (Purchased)"]))
        if row["status"] == "Complete":
            completed.append((stamp, row["Grand Total (Purchased)"]))
    canceled.sort(reverse=True)
    completed.sort(reverse=True)
    assert len(canceled) >= 4 and len(completed) >= 4, "four orders of each status must exist"
    return round(sum(value for stamp, value in canceled[:4]) - sum(value for stamp, value in completed[:4]), 2)
''',
        202: '''
def run(ctx):
    import datetime
    rows = ctx.acquire(ctx.lookup("orders"), fields=["id"], coverage="complete")
    dates = []
    for row in rows:
        state = ctx.read(row, fields=["status", "created_at"])
        if state["status"].casefold() == "canceled":
            dates.append(datetime.datetime.strptime(state["created_at"], "%Y-%m-%d %H:%M:%S"))
    assert dates, "at least one canceled order must exist"
    return max(dates).strftime("%Y-%m-%d")
''',
    }
    for task_id, source in sources.items():
        run = execute_code(source, fixture_for_task(task_id))
        assert run.ok, run.error
        assert grade_coding_trace(task_id, run.trace, run.return_value) == []


def test_new_frozen_review_mutation_grader_accepts_exact_approval_set() -> None:
    source = '''
def run(ctx):
    rows = ctx.acquire(ctx.lookup("reviews"), fields=["id", "title", "status"], coverage="complete")
    assert rows, "reviews must be available"
    targets = []
    for row in rows:
        state = ctx.read(row, fields=["detailed_rating", "summary_of_review"])
        if state["detailed_rating"] >= 4 and row["status"] != "Approved":
            targets.append(row)
    assert targets, "at least one high-rating pending review must exist"
    for target in targets:
        saved = ctx.interact(
            "approve the qualifying review",
            success="the review status is saved as Approved",
            target=target,
            values={"status": "Approved"},
            persistence="explicit_commit",
        )
        assert saved, "the qualifying review must be approved"
'''
    run = execute_code(source, fixture_for_task(771))

    assert run.ok, run.error
    assert grade_coding_trace(771, run.trace, run.return_value) == []


def test_unseen_action_task_graders_accept_business_boundaries() -> None:
    tracking = '''
def run(ctx):
    orders = ctx.acquire(ctx.lookup("order #301"), fields=["id", "order_id"], coverage="complete")
    assert len(orders) == 1, "order 301 must be unique"
    saved = ctx.interact(
        "add DHL tracking number 239028439840 to order 301 shipment",
        success="DHL tracking number 239028439840 is saved",
        target=orders[0],
        values={"carrier": "DHL", "tracking_number": "239028439840"},
        persistence="explicit_commit",
    )
    assert saved, "tracking must be saved"
'''
    report = '''
def run(ctx):
    shown = ctx.interact(
        "show Sales Orders report from 02/01/2023 to 02/28/2023",
        success="Sales Orders report for 02/01/2023 through 02/28/2023 is rendered",
        values={"from": "02/01/2023", "to": "02/28/2023"},
        persistence="immediate",
    )
    assert shown, "the requested report must be rendered"
'''
    for task_id, source in ((500, tracking), (704, report)):
        run = execute_code(source, fixture_for_task(task_id))
        assert run.ok, run.error
        assert grade_coding_trace(task_id, run.trace, run.return_value) == []


def test_new_frozen_task_491_selects_latest_pending_order_before_notification() -> None:
    source = '''
def run(ctx):
    rows = ctx.acquire(
        ctx.lookup("Sarah Miller"),
        fields=["id", "order_id", "customer_name", "status"],
        coverage="complete",
    )
    pending = [
        row for row in rows
        if row["customer_name"] == "Sarah Miller" and row["status"] == "Pending"
    ]
    assert pending, "Sarah Miller must have a pending order"
    dated = []
    for row in pending:
        state = ctx.read(row, fields=["created_at"])
        dated.append((row, state["created_at"]))
    latest_date = max(date for row, date in dated)
    targets = [row for row, date in dated if date == latest_date]
    assert len(targets) == 1, "Sarah Miller's latest pending order must be unique"
    for target in targets:
        saved = ctx.interact(
            "notify the customer about this order",
            success="the order comment is saved and the customer is notified",
            target=target,
            values={"message": "the order is ready to be shipped soon!"},
            persistence="explicit_commit",
        )
        assert saved, "the customer notification must be saved"
'''
    run = execute_code(source, fixture_for_task(491))

    assert run.ok, run.error
    assert grade_coding_trace(491, run.trace, run.return_value) == []


def test_new_frozen_task_550_uses_two_ordered_business_operations() -> None:
    source = '''
def run(ctx):
    attributes = ctx.acquire(
        ctx.lookup("size"),
        fields=["id", "name", "attribute_code"],
        coverage="complete",
    )
    size_attributes = [row for row in attributes if row["attribute_code"] == "size"]
    assert len(size_attributes) == 1, "the existing Size attribute must be unique"

    products = ctx.acquire(
        ctx.lookup("Nona Fitness Tank"),
        fields=["id", "name", "type", "sku"],
        coverage="complete",
    )
    parents = [row for row in products if row["type"] == "Configurable Product"]
    assert len(parents) == 1, "the Nona configurable product must be unique"

    option_saved = ctx.interact(
        "add an option to the Size product attribute",
        success="the Size attribute contains the XXS option",
        target=size_attributes[0],
        values={"attribute": "size", "option": "XXS"},
        persistence="explicit_commit",
    )
    assert option_saved, "the XXS option must be saved"
    configurations_saved = ctx.interact(
        "add configurations to the Nona product",
        success="the Nona product contains Blue XXS and Purple XXS configurations",
        target=parents[0],
        values={"size": "XXS", "colors": ["Blue", "Purple"]},
        persistence="explicit_commit",
    )
    assert configurations_saved, "the requested Nona configurations must be saved"
'''
    run = execute_code(source, fixture_for_task(550))

    assert run.ok, run.error
    assert grade_coding_trace(550, run.trace, run.return_value) == []


def test_new_frozen_task_768_reads_quantity_before_inventory_update() -> None:
    source = '''
def run(ctx):
    products = ctx.acquire(
        ctx.lookup("Cronus yoga pants"),
        fields=["id", "name", "type", "sku"],
        coverage="complete",
    )
    assert products, "Cronus product candidates must exist"
    targets = []
    for product in products:
        if product["type"] != "Simple Product":
            continue
        state = ctx.read(product, fields=["size", "color", "quantity"])
        if state["size"] == "33" and state["color"].casefold() == "blue":
            new_quantity = state["quantity"] + 5
            assert new_quantity - state["quantity"] == 5, "inventory must increase by five"
            targets.append((product, new_quantity))
    assert len(targets) == 1, "the blue size 33 Cronus variant must be unique"
    for product, new_quantity in targets:
        saved = ctx.interact(
            "update product inventory quantity",
            success="the product quantity is durably saved",
            target=product,
            values={"quantity": new_quantity},
            persistence="explicit_commit",
        )
        assert saved, "the updated inventory quantity must be saved"
'''
    run = execute_code(source, fixture_for_task(768))

    assert run.ok, run.error
    assert grade_coding_trace(768, run.trace, run.return_value) == []


def test_task_778_coding_trace_rejects_wrong_member_and_price() -> None:
    trace = [TraceEvent("interact", ("save",), {
        "target": {"id": "sahara-30"},
        "values": {"price": 1.0},
        "persistence": "explicit_commit",
    })]

    failures = grade_coding_trace(778, trace)
    assert "GT778:NO_COMPLETE_IDENTITY_ACQUIRE" in failures
    assert any(failure.startswith("GT778:WRONG_MUTATIONS") for failure in failures)


def test_task_549_coding_trace_requires_ordered_durable_business_states() -> None:
    trace = [
        TraceEvent("interact", ("add option to existing Size attribute",), {
            "success": "Size attribute options contain XXXL",
            "values": {"attribute": "size", "option": "XXXL"},
            "persistence": "explicit_commit",
        }),
        TraceEvent("interact", ("configure Minerva LumaTech V-Tee product",), {
            "success": "configurable product configuration contains green XXXL",
            "values": {"product": "Minerva", "color": "green", "size": "XXXL"},
            "persistence": "explicit_commit",
        }),
    ]

    assert grade_coding_trace(549, trace) == []
    assert "GT549:STAGES_OUT_OF_ORDER" in grade_coding_trace(549, list(reversed(trace)))

    extra = [
        TraceEvent("interact", ("open editor",), {"persistence": "explicit_commit"}),
        *trace,
    ]
    assert "GT549:EXTRA_DURABLE_STAGES:3" in grade_coding_trace(549, extra)


def test_task_549_dsl_grader_uses_same_business_state_requirements() -> None:
    draft = _PlanDraft(steps=[
        _StepDraft(
            op="interact",
            goal="add XXXL option to existing Size attribute",
            success="Size attribute option XXXL saved",
            required_values={"attribute": "size", "option": "XXXL"},
            persistence="explicit_commit",
        ),
        _StepDraft(
            op="interact",
            goal="configure Minerva product",
            success="configurable product configuration green XXXL saved",
            required_values={"product": "Minerva", "color": "green", "size": "XXXL"},
            persistence="explicit_commit",
        ),
    ])
    program = to_program(draft, "configure product")

    assert isinstance(program, Program)
    assert grade_dsl_program(549, program) == []
