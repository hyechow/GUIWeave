from gui_agent.core.self_learning.app_summary import load_knowledge_for_app


def _shopping_knowledge():
    knowledge = load_knowledge_for_app("shopping", "browser")
    assert knowledge is not None
    return knowledge


def test_shopping_worker_knowledge_covers_storefront_without_becoming_a_manual() -> None:
    knowledge = _shopping_knowledge()
    worker = " ".join(knowledge.worker_context().split())

    assert len(knowledge.worker_context()) < 7_000
    assert len(knowledge.sections) == 7
    assert "Buy" in worker and "order-success page" in worker
    assert "cannot be edited" in worker
    assert "without activating Submit" in worker
    assert "check_rules" in knowledge.metadata["_check"]["id"]


def test_shopping_review_and_wishlist_goals_select_only_their_sections() -> None:
    knowledge = _shopping_knowledge()

    assert knowledge.orchestrator_sections(
        "Get all review titles with 2 stars or below for the product on the current page."
    ) == ["product_reviews_planning"]
    assert knowledge.orchestrator_sections(
        "Add the product on the current page to my wishlist."
    ) == ["wishlist_newsletter_planning"]


def test_shopping_buy_goal_combines_catalog_selection_and_checkout_commit() -> None:
    knowledge = _shopping_knowledge()
    goal = "Buy the highest rated product from a category under a budget and empty my cart."
    sections = knowledge.orchestrator_sections(goal)
    context = " ".join(knowledge.orchestrator_context(goal).split())

    assert sections == ["cart_checkout_planning", "catalog_planning"]
    assert "completed checkout" in context
    assert "rating, review count, current price" in context
    assert "Place Order" in context


def test_shopping_contact_draft_selects_order_data_without_crossing_submit() -> None:
    knowledge = _shopping_knowledge()
    goal = "Fill Contact Us with a refund message using my order amount; do not submit."
    sections = knowledge.orchestrator_sections(goal)
    context = " ".join(knowledge.orchestrator_context(goal).split())

    assert sections == ["contact_planning", "order_history_planning"]
    assert "line Subtotal" in context
    assert "do not activate Submit" in context
    assert "The populated form is the final state" in context


def test_shopping_placed_order_address_is_not_replaced_by_address_book_edit() -> None:
    knowledge = _shopping_knowledge()
    goal = "Change the delivery address for my most recent non canceled order."
    sections = knowledge.orchestrator_sections(goal)
    context = " ".join(knowledge.orchestrator_context(goal).split())

    assert sections == ["account_planning", "order_history_planning"]
    assert "has no edit action" in context
    assert "not the placed order" in context


def test_shopping_spend_and_reorder_goals_select_order_sources() -> None:
    knowledge = _shopping_knowledge()

    assert knowledge.orchestrator_sections(
        "Find the number of my most recent order."
    ) == ["order_history_planning"]
    recent_context = knowledge.orchestrator_context(
        "Find the number of my most recent order."
    )
    assert "exactly one order record" in recent_context
    assert "exact spelling and capitalization" in recent_context
    assert knowledge.orchestrator_sections(
        "How much did I spend on food in March without shipping?"
    ) == ["order_history_planning"]
    assert knowledge.orchestrator_sections(
        "Reorder a product from my canceled order."
    ) == ["order_history_planning", "cart_checkout_planning"]
    assert knowledge.orchestrator_sections(
        "Get the price of an item I bought last month."
    ) == ["order_history_planning"]
