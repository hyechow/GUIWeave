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

    review_context = " ".join(knowledge.orchestrator_context(
        "Rate my recently purchased desk chair with five stars."
    ).split())
    assert "separate sources" in review_context
    assert "primary_product_contains" in review_context
    assert "Select the single `primary_product` identity" in review_context
    assert "do not return to My Orders or activate Reorder" in review_context


def test_shopping_newsletter_uses_the_signed_in_account_email() -> None:
    knowledge = _shopping_knowledge()
    goal = "Subscribe to the newsletter of OneStopMarket."

    assert "emma.lopez@gmail.com" in knowledge.worker_context()
    assert "emma.lopez@gmail.com" in knowledge.orchestrator_context(goal)


def test_shopping_buy_goal_combines_catalog_selection_and_checkout_commit() -> None:
    knowledge = _shopping_knowledge()
    goal = "Buy the highest rated product from a category under a budget and empty my cart."
    sections = knowledge.orchestrator_sections(goal)
    context = " ".join(knowledge.orchestrator_context(goal).split())

    assert sections == ["cart_checkout_planning", "catalog_planning"]
    assert "completed checkout" in context
    assert "rating, review count, current price" in context
    assert "Place Order" in context


def test_shopping_catalog_knowledge_preserves_exact_price_boundaries() -> None:
    knowledge = _shopping_knowledge()
    goal = "Open a category filtered to under $40."
    worker = " ".join(knowledge.worker_context().split())
    context = " ".join(knowledge.orchestrator_context(goal).split())

    assert knowledge.orchestrator_sections(goal) == ["catalog_planning"]
    assert "price=<lower>-<upper>" in worker
    assert '"under X" maps to `price=0-X`' in context
    assert "`cat=<id>` alias" in context
    assert "/women/<leaf>.html" in context
    assert "invalid unless its canonical URL" in context
    assert "retain the segment" in worker
    assert "every successive category choice" in context
    assert "not a nested hierarchy" in context
    assert "For ascending, click `Set Ascending Direction`" in worker
    assert "for descending, do the inverse" in context
    assert "rejected detail preserves query/order" in worker
    assert "N single-shoe pockets = N/2 pairs" in worker
    assert "Terminal reporting from later pages is invalid" in worker


def test_shopping_navigation_exposes_nested_category_paths() -> None:
    worker = " ".join(_shopping_knowledge().worker_context().split())

    assert "/beauty-personal-care/makeup/makeup-remover.html" in worker
    assert "/home-kitchen/furniture/accent-furniture.html" in worker


def test_shopping_contact_draft_selects_order_data_without_crossing_submit() -> None:
    knowledge = _shopping_knowledge()
    goal = "Fill Contact Us with a refund message using my order amount; do not submit."
    sections = knowledge.orchestrator_sections(goal)
    context = " ".join(knowledge.orchestrator_context(goal).split())

    assert sections == ["contact_planning", "order_history_planning"]
    assert "line Subtotal" in context
    assert "sold bundle qualifies" in context
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
    spend_goal = "How much did I spend on food in March without shipping?"
    assert knowledge.orchestrator_sections(spend_goal) == ["order_history_planning"]
    spend_context = knowledge.orchestrator_context(spend_goal)
    assert "Artificial plants and topiary are" in spend_context
    assert "speakers are Electronics" in spend_context
    assert knowledge.orchestrator_sections(
        "Reorder a product from my canceled order."
    ) == ["order_history_planning", "cart_checkout_planning"]
    assert knowledge.orchestrator_sections(
        "Get the price of an item I bought last month."
    ) == ["order_history_planning"]


def test_shopping_unavailable_arrival_stays_out_of_acquisition_schema() -> None:
    knowledge = _shopping_knowledge()
    context = knowledge.orchestrator_context(
        "Get the status of my latest order and when it will arrive."
    )

    assert "collect only the newest row's visible Status" in context
    assert "do not add an unavailable arrival field" in context
    detail_goal = "Open the most recent processing order details."
    assert knowledge.orchestrator_sections(detail_goal) == ["order_history_planning"]
    assert "required failure handling" in knowledge.orchestrator_context(detail_goal)
