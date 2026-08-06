from gui_agent.adapters.android.accessibility import (
    collection_regions_from_uiautomator,
    semantic_tree_from_uiautomator,
)
from gui_agent.adapters.android.actions import AndroidAction, AndroidActionDecision
from gui_agent.adapters.android.policies import AndroidActionPolicy
from gui_agent.core.schemas import (
    ActionIntent,
    Observation,
    StatementContract,
    SupervisorStep,
)
from gui_agent.core.supervisor.statement.context_projection import (
    project_transition_observation,
)
from gui_agent.core.supervisor.statement.observation_view import build_observation_view


XML = """<?xml version="1.0" encoding="UTF-8"?>
<hierarchy rotation="0">
  <node class="android.widget.FrameLayout" bounds="[0,0][1080,2400]">
    <node class="androidx.recyclerview.widget.RecyclerView" resource-id="org.example:id/feed"
          scrollable="true" bounds="[0,200][1080,2200]">
      <node class="android.widget.TextView" text="@pupper" bounds="[48,240][400,320]"/>
      <node class="android.widget.Button" content-desc="Favorite" clickable="true"
            enabled="true" bounds="[700,600][900,720]"/>
      <node class="android.widget.Switch" content-desc="Notifications" checkable="true"
            checked="true" bounds="[800,800][1000,920]"/>
    </node>
  </node>
</hierarchy>
"""


def test_uiautomator_normalizes_current_collection_controls() -> None:
    tree = semantic_tree_from_uiautomator(XML, viewport_size=(1080, 2400))

    assert tree is not None
    collection = next(node for node in tree if node["scrollable"])
    nodes = {node["key"]: node for node in tree if not node["scrollable"]}
    assert (collection["role"], collection["in_viewport"]) == ("list", True)
    assert (nodes["Favorite"]["role"], nodes["Notifications"]["value"]) == (
        "button", True,
    )
    assert nodes["Favorite"]["point"] == {"x": 740.7407407407408, "y": 275.0}


def test_uiautomator_failure_is_an_optional_sensor_miss() -> None:
    assert semantic_tree_from_uiautomator(None, viewport_size=(1080, 2400)) is None
    assert semantic_tree_from_uiautomator("<broken", viewport_size=(1080, 2400)) is None
    assert semantic_tree_from_uiautomator(XML, viewport_size=(0, 0)) is None


def test_uiautomator_projects_clickable_text_selection_state() -> None:
    xml = """<hierarchy>
      <node class="android.widget.FrameLayout" bounds="[0,0][1080,2400]">
        <node class="android.widget.TextView" text="Favorites" clickable="true"
              selected="false" bounds="[42,562][277,645]"/>
        <node class="android.widget.TextView" text="Bookmarks" clickable="true"
              selected="true" bounds="[298,562][616,645]"/>
      </node>
    </hierarchy>"""
    tree = semantic_tree_from_uiautomator(xml, viewport_size=(1080, 2400))
    assert tree is not None
    observation = Observation(png_bytes=b"frame", source="android", semantic_tree=tree)
    statement = StatementContract(
        id="reach",
        goal="open saved favorites",
        success="the requested view is active",
        expected_state={"entity": "SavedFavorites", "active_view": "Favorites"},
    )

    view = build_observation_view(statement, observation, [])
    projected = project_transition_observation(
        statement,
        observation,
        view,
        initial_filters=None,
    )
    states = {
        item["label"]: item["selected"]
        for item in projected["affordances"]
        if item.get("label") in {"Favorites", "Bookmarks"}
    }

    assert states == {"Favorites": False, "Bookmarks": True}


def test_semantic_textbox_supports_activate() -> None:
    """A semantic_tree textbox (no form_control_state entry) must also offer
    activate, not only input — the supervisor taps a text field to focus it."""
    observation = Observation(
        png_bytes=b"frame",
        source="android",
        semantic_tree=[
            {"role": "textbox", "key": "Search", "ref": "s1",
             "in_viewport": True},
        ],
    )
    statement = StatementContract(id="c", goal="g", success="s")
    view = build_observation_view(statement, observation, [])
    affordances = [
        item for item in view.affordances
        if item.get("label") == "Search"
    ]
    assert affordances
    assert {"input", "activate"} <= set(
        affordances[0]["supported_operations"]
    )


def test_text_input_control_supports_activate() -> None:
    """A text field is focused by tapping it (activate) then typed into (input);
    its affordance must offer both (run-5 form loop on the description field)."""
    observation = Observation(
        png_bytes=b"frame",
        source="android",
        form_control_state=[
            {"kind": "text_input", "label": "Description",
             "ref": "desc1", "value": ""},
        ],
    )
    statement = StatementContract(id="c", goal="g", success="s")
    view = build_observation_view(statement, observation, [])
    affordances = [
        item for item in view.affordances
        if item.get("label") == "Description"
    ]
    assert affordances
    assert {"input", "activate"} <= set(
        affordances[0]["supported_operations"]
    )


def test_select_control_supports_activate() -> None:
    """A date/time select field is a button that opens a picker by tapping; its
    affordance must offer activate, not only select (run-3 form loop)."""
    observation = Observation(
        png_bytes=b"frame",
        source="android",
        form_control_state=[
            {"kind": "select", "label": "event start date",
             "ref": "date1", "value": "October 16"},
        ],
    )
    statement = StatementContract(id="c", goal="g", success="s")
    view = build_observation_view(statement, observation, [])
    date_affordances = [
        item for item in view.affordances
        if item.get("label") == "event start date"
    ]
    assert date_affordances
    assert {"select", "activate"} <= set(
        date_affordances[0]["supported_operations"]
    )


def test_uiautomator_exposes_ordered_cells_without_inventing_records() -> None:
    regions = collection_regions_from_uiautomator(XML, viewport_size=(1080, 2400))

    assert regions is not None and len(regions) == 1
    region = regions[0]
    assert region["traversal"] == {"type": "scroll"}
    assert [cell["texts"][0] for cell in region["cells"]] == [
        "@pupper", "Favorite", "Notifications",
    ]
    assert region["cells"][1]["controls"][0]["label"] == "Favorite"
    assert "records" not in region


def test_uiautomator_drops_only_exact_same_bounds_ghost_cells() -> None:
    xml = """<hierarchy>
      <node class="android.widget.GridView" resource-id="app:id/list"
            scrollable="true" bounds="[0,0][1080,2400]">
        <node class="android.widget.TextView" text="Same" bounds="[0,100][1080,200]"/>
        <node class="android.widget.TextView" text="Same" bounds="[0,100][1080,200]"/>
        <node class="android.widget.TextView" text="Same" bounds="[0,200][1080,300]"/>
      </node>
    </hierarchy>"""

    regions = collection_regions_from_uiautomator(xml, viewport_size=(1080, 2400))

    assert regions is not None
    assert [cell["texts"] for cell in regions[0]["cells"]] == [["Same"], ["Same"]]
    assert [round(cell["bounds"][1]) for cell in regions[0]["cells"]] == [42, 83]


def test_uiautomator_drops_cells_without_observable_content() -> None:
    xml = """<hierarchy>
      <node class="android.widget.GridView" resource-id="app:id/list"
            scrollable="true" bounds="[0,0][1080,2400]">
        <node class="android.widget.LinearLayout" resource-id="app:id/item_root"
              bounds="[0,0][1080,100]"/>
        <node class="android.widget.TextView" text="Visible"
              bounds="[0,100][1080,200]"/>
      </node>
    </hierarchy>"""

    regions = collection_regions_from_uiautomator(xml, viewport_size=(1080, 2400))

    assert regions is not None
    assert [cell["texts"] for cell in regions[0]["cells"]] == [["Visible"]]


def test_collection_traversal_reflects_the_adapter_scroll_capability() -> None:
    xml = XML.replace('scrollable="true"', 'scrollable="false"')

    regions = collection_regions_from_uiautomator(xml, viewport_size=(1080, 2400))

    assert regions is not None
    assert regions[0]["traversal"] == {"type": "static"}


def test_collection_resource_identity_survives_hierarchy_path_shift() -> None:
    shifted = XML.replace(
        '<node class="androidx.recyclerview.widget.RecyclerView"',
        '<node class="android.widget.TextView" text="sticky chrome" '
        'bounds="[0,0][1080,100]"/>'
        '<node class="androidx.recyclerview.widget.RecyclerView"',
    )
    wrapped = XML.replace(
        '<node class="android.widget.TextView" text="@pupper" bounds="[48,240][400,320]"/>',
        '<node class="android.view.View" bounds="[48,240][400,320]">'
        '<node class="android.widget.TextView" text="@pupper" bounds="[48,240][400,320]"/>'
        '</node>',
    )

    original = collection_regions_from_uiautomator(XML, viewport_size=(1080, 2400))
    moved = collection_regions_from_uiautomator(shifted, viewport_size=(1080, 2400))
    rewrapped = collection_regions_from_uiautomator(wrapped, viewport_size=(1080, 2400))

    assert original is not None and moved is not None and rewrapped is not None
    assert original[0]["ref"] != moved[0]["ref"]
    assert original[0]["surface_fingerprint"] == moved[0]["surface_fingerprint"]
    assert [cell["content_key"] for cell in original[0]["cells"]] == [
        cell["content_key"] for cell in moved[0]["cells"]
    ]
    assert (
        original[0]["cells"][0]["content_key"]
        == rewrapped[0]["cells"][0]["content_key"]
    )


def _webview(records: str, route: str) -> str:
    return f'''<node class="android.webkit.WebView" scrollable="true"
      bounds="[0,0][1080,2400]">
      <node class="android.view.View" content-desc="{route}">
        <node class="android.view.View" bounds="[0,200][1080,2200]">
          {records}
        </node>
      </node>
    </node>'''


def test_webview_exposes_topmost_heterogeneous_cell_stream() -> None:
    hidden = _webview(
        '<node class="android.widget.TextView" text="A"/>'
        '<node class="android.widget.TextView" text="B"/>',
        "pages/home[1]",
    )
    active = _webview('''
      <node class="android.widget.TextView" text="Order 1"/>
      <node class="android.widget.Button" text="Completed" clickable="true"/>
      <node class="android.view.View">
        <node class="android.widget.TextView" text="Total"/>
        <node class="android.widget.TextView" text="1196"/>
      </node>''', "pages/orders[1]")

    regions = collection_regions_from_uiautomator(
        f"<hierarchy>{hidden}{active}</hierarchy>",
        viewport_size=(1080, 2400),
    )

    assert regions is not None and len(regions) == 1
    assert regions[0]["caption"] == "pages/orders[1]"
    assert [cell["texts"] for cell in regions[0]["cells"]] == [
        ["Order 1"], ["Completed"], ["Total", "1196"],
    ]


def test_webview_collection_identity_ignores_filtered_record_shape() -> None:
    def observe(records: str, route: str = "pages/orders[1]"):
        return collection_regions_from_uiautomator(
            f"<hierarchy>{_webview(records, route)}</hierarchy>",
            viewport_size=(1080, 2400),
        )

    full = observe('<node text="Order A"/><node text="Order B"/>')
    filtered = observe('<node text="Order D"/><node text="Total 367"/>')
    other_route = observe(
        '<node text="Order D"/><node text="Total 367"/>', "pages/cart[1]",
    )

    assert full and filtered and other_route
    assert full[0]["surface_fingerprint"] == filtered[0]["surface_fingerprint"]
    assert full[0]["surface_fingerprint"] != other_route[0]["surface_fingerprint"]


def test_android_policy_taps_only_an_exact_current_structural_ref() -> None:
    tree = semantic_tree_from_uiautomator(XML, viewport_size=(1080, 2400))
    assert tree is not None
    favorite = next(node for node in tree if node["key"] == "Favorite")
    observation = Observation(png_bytes=b"frame", source="android", semantic_tree=tree)
    policy = AndroidActionPolicy()

    decision = policy.resolve_native_action(
        observation,
        target_control="Favorite (star icon)",
        target_ref=favorite["ref"],
        action_family="activate",
        instruction="Favorite the current target",
    )
    stale = policy.resolve_native_action(
        observation,
        target_control="Favorite",
        target_ref="android:stale",
        action_family="activate",
    )

    assert decision is not None
    assert (decision.action.action_type, decision.action.x, decision.action.y) == (
        "tap", favorite["point"]["x"], favorite["point"]["y"],
    )
    assert stale is None


def test_android_policy_snaps_off_target_point_to_declared_ref() -> None:
    """A correctly-declared ref must not die on a few dozen px of point-estimate
    error: snap the action to the ref's authoritative center instead of rejecting.
    A gross mismatch (clearly a different control) still stays contradicted."""
    tree = semantic_tree_from_uiautomator(XML, viewport_size=(1080, 2400))
    assert tree is not None
    favorite = next(node for node in tree if node["key"] == "Favorite")
    observation = Observation(png_bytes=b"frame", source="android", semantic_tree=tree)
    policy = AndroidActionPolicy()
    fx, fy = favorite["point"]["x"], favorite["point"]["y"]

    def _decision(x: float, y: float) -> AndroidActionDecision:
        return AndroidActionDecision(action=AndroidAction(
            action_type="tap", x=x, y=y, description="tap the target"))

    def _step() -> SupervisorStep:
        return SupervisorStep(
            summary="target frame",
            action_intent=ActionIntent(
                instruction="tap the target",
                role="write",
                family="input",
                target_control="Favorite",
                target_ref=favorite["ref"],
            ),
        )

    # A few dozen units off the ref center: snapped to the authoritative point.
    decision = _decision(fx + 20, fy - 15)
    binding = policy.bind(_step(), observation, decision)
    assert binding is not None and binding.status == "bound"
    assert binding.source == "structural"
    assert (decision.action.x, decision.action.y) == (fx, fy)

    # Exact point: bound, coordinates unchanged.
    decision = _decision(fx, fy)
    binding = policy.bind(_step(), observation, decision)
    assert binding is not None and binding.status == "bound"
    assert (decision.action.x, decision.action.y) == (fx, fy)

    # Grossly off (different control clearly intended): contradicted, no snap.
    decision = _decision(fx + 300, fy + 250)
    binding = policy.bind(_step(), observation, decision)
    assert binding is not None and binding.status == "contradicted"
    assert (decision.action.x, decision.action.y) == (fx + 300, fy + 250)
