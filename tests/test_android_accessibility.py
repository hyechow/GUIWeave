import pytest

from gui_agent.adapters.android.accessibility import (
    collection_regions_from_uiautomator,
    form_controls_from_semantic_tree,
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


def test_unlabeled_switch_uses_same_row_visible_text_as_its_label() -> None:
    xml = """<hierarchy>
      <node class="android.widget.FrameLayout" bounds="[0,0][1080,2400]">
        <node class="android.widget.LinearLayout" bounds="[0,500][1080,700]">
          <node class="android.widget.TextView" text="Wi-Fi"
                bounds="[48,540][500,660]"/>
          <node class="android.widget.Switch" resource-id="com.android.settings:id/switch_widget"
                checkable="true" checked="true" bounds="[890,550][1038,650]"/>
        </node>
        <node class="android.widget.TextView" text="Airplane mode"
              bounds="[48,900][500,1020]"/>
      </node>
    </hierarchy>"""
    tree = semantic_tree_from_uiautomator(xml, viewport_size=(1080, 2400))

    controls = form_controls_from_semantic_tree(tree)

    assert controls is not None
    assert controls[0]["label"] == "Wi-Fi"
    assert controls[0]["value"] is True


def test_clickable_bottom_sheet_rows_are_named_controls_with_center_rects() -> None:
    xml = """<hierarchy>
      <node class="android.widget.FrameLayout" bounds="[0,0][1080,2400]">
        <node class="android.widget.LinearLayout" clickable="true"
              bounds="[0,1944][1080,2056]">
          <node class="android.widget.TextView" text="Browse Channels"
                bounds="[72,1960][600,2040]"/>
        </node>
        <node class="android.widget.LinearLayout" clickable="true"
              resource-id="plus_menu_item.create_new_channel"
              bounds="[0,2056][1080,2176]">
          <node class="android.widget.TextView" text="unrelated background text"
                bounds="[72,2072][650,2160]"/>
        </node>
        <node class="android.widget.LinearLayout" clickable="true"
              bounds="[0,2176][1080,2296]">
          <node class="android.widget.TextView" text="Open Direct Message"
                bounds="[72,2192][700,2280]"/>
        </node>
        <node class="android.widget.EditText"
              resource-id="channel_info_form.display_name.input"
              text="Bugs, Marketing" bounds="[100,678][980,741]"/>
        <node class="android.widget.EditText" resource-id="password"
              text="private value" bounds="[100,760][980,820]"/>
        <node class="android.widget.Spinner" content-desc="Reminder interval"
              clickable="true" bounds="[100,900][980,1020]"/>
      </node>
    </hierarchy>"""

    tree = semantic_tree_from_uiautomator(xml, viewport_size=(1080, 2400))
    controls = form_controls_from_semantic_tree(tree)

    assert controls is not None
    by_label = {control["label"]: control for control in controls}
    assert set(by_label) == {
        "Browse Channels", "create new channel", "Open Direct Message", "name",
        "password", "Reminder interval",
    }
    create = by_label["create new channel"]
    assert create["kind"] == "button"
    assert create["bounds"] == pytest.approx((0, 856.6667, 1000, 906.6667))
    assert create["rect"] == pytest.approx({
        "x": 500,
        "y": 881.6667,
        "w": 1000,
        "h": 50,
    })
    assert (by_label["name"]["kind"], by_label["name"]["value"]) == (
        "text_input", "Bugs, Marketing",
    )
    assert by_label["Reminder interval"]["kind"] == "select"


def test_glyph_backed_multiselect_rows_expose_selected_state_and_coordinates() -> None:
    xml = """<hierarchy>
      <node class="android.widget.FrameLayout" bounds="[0,0][1080,2400]">
        <node class="android.widget.LinearLayout" content-desc="alex, \U000F05E0"
              clickable="true" selected="false" bounds="[0,510][1080,657]">
          <node class="android.widget.TextView" text="alex"
                bounds="[145,540][260,610]"/>
          <node class="android.widget.TextView" text="\U000F05E0"
                bounds="[950,540][1032,613]"/>
        </node>
        <node class="android.widget.LinearLayout" content-desc="arjun, \U000F0766"
              clickable="true" selected="false" bounds="[0,657][1080,804]">
          <node class="android.widget.TextView" text="arjun"
                bounds="[145,687][280,757]"/>
          <node class="android.widget.TextView" text="\U000F0766"
                bounds="[950,687][1032,760]"/>
        </node>
        <node class="android.widget.Button" content-desc="\U000F0610, Add Members"
              resource-id="add_members.selected.start.button" clickable="true"
              bounds="[55,2159][1025,2285]">
          <node class="android.widget.TextView" text="\U000F0610"
                bounds="[350,2190][420,2250]"/>
          <node class="android.widget.TextView" text="Add Members"
                bounds="[440,2190][720,2250]"/>
        </node>
        <node class="android.widget.Button" content-desc="Set Header, \U000F0130"
              clickable="true" bounds="[350,1800][660,1984]">
          <node class="android.widget.TextView" text="\U000F0130"
                bounds="[455,1830][525,1900]"/>
        </node>
        <node class="android.widget.LinearLayout" content-desc="Wi-Fi, \U000F0142"
              clickable="true" bounds="[0,1200][1080,1350]">
          <node class="android.widget.TextView" text="Wi-Fi"
                bounds="[100,1230][400,1320]"/>
          <node class="android.widget.TextView" text="\U000F0142"
                bounds="[950,1230][1030,1320]"/>
        </node>
        <node class="android.view.ViewGroup" bounds="[80,2050][360,2140]">
          <node class="android.widget.TextView" text="alex"
                bounds="[90,2060][250,2130]"/>
          <node class="android.widget.Button" content-desc="\U000F0159"
                resource-id="add_members.selected.user.remove.button"
                clickable="true" bounds="[250,2060][340,2130]"/>
        </node>
      </node>
    </hierarchy>"""

    tree = semantic_tree_from_uiautomator(xml, viewport_size=(1080, 2400))
    controls = form_controls_from_semantic_tree(tree)

    assert controls is not None
    rows = {
        control["label"]: control
        for control in controls
        if control["kind"] == "checkbox"
    }
    assert rows["alex"]["selected"] is True
    assert rows["arjun"]["selected"] is False
    assert rows["alex"]["selection_mode"] == "multiple"
    assert rows["alex"]["bounds"] == pytest.approx((0, 212.5, 1000, 273.75))
    assert rows["arjun"]["rect"]["y"] == pytest.approx(304.375)
    assert rows["alex"]["action_point"] == pytest.approx({
        "x": 917.5926,
        "y": 240.2083,
    })
    by_label = {control["label"]: control for control in controls}
    assert by_label["Add Members"]["kind"] == "button"
    assert "form_action" not in by_label["Add Members"]
    assert by_label["Set Header"]["kind"] == "button"
    assert "form_action" not in by_label["Set Header"]
    assert by_label["Wi-Fi"]["kind"] == "button"
    assert "action_point" not in by_label["Wi-Fi"]
    assert by_label["remove"]["kind"] == "button"


def test_android_commit_metadata_requires_explicit_submission_semantics() -> None:
    xml = """<hierarchy>
      <node class="android.widget.FrameLayout" bounds="[0,0][1080,2400]">
        <node class="android.widget.Button" text="Add members"
              resource-id="channel_post_list.intro_options.add_members.action"
              clickable="true" bounds="[50,1500][360,1680]"/>
        <node class="android.widget.EditText" text="Write a message"
              resource-id="channel.post_draft.post.input"
              bounds="[50,1800][1030,1900]"/>
        <node class="android.widget.Button" content-desc="Camera"
              resource-id="channel.post_draft.quick_actions.camera_action"
              clickable="true" bounds="[400,1950][520,2070]"/>
        <node class="android.widget.Button" content-desc="Send"
              resource-id="channel.post_draft.send_action.send.button"
              clickable="true" bounds="[850,1950][1030,2070]"/>
        <node class="android.widget.Button" text="CREATE" clickable="true"
              bounds="[850,100][1030,220]"/>
        <node class="android.widget.Button" text="Save changes" clickable="true"
              bounds="[600,250][1030,370]"/>
        <node class="android.widget.Button" text="Create New Channel" clickable="true"
              bounds="[50,250][550,370]"/>
      </node>
    </hierarchy>"""

    controls = form_controls_from_semantic_tree(
        semantic_tree_from_uiautomator(xml, viewport_size=(1080, 2400))
    )

    assert controls is not None
    by_label = {item["label"]: item for item in controls}
    assert "form_action" not in by_label["Add members"]
    assert "form_action" not in by_label["Write a message"]
    assert "form_action" not in by_label["Camera"]
    assert by_label["Send"]["form_action"] == "commit"
    assert by_label["CREATE"]["form_action"] == "commit"
    assert by_label["Save changes"]["form_action"] == "commit"
    assert "form_action" not in by_label["Create New Channel"]


def test_clickable_composite_form_wrapper_is_not_projected_as_a_button() -> None:
    xml = """<hierarchy>
      <node class="android.widget.FrameLayout" bounds="[0,0][1080,2400]">
        <node class="android.view.ViewGroup" clickable="true"
              bounds="[53,360][1028,1570]">
          <node class="android.widget.Switch" content-desc="Make Private"
                checkable="true" checked="false" bounds="[906,425][1028,496]"/>
          <node class="android.widget.EditText" content-desc="Name"
                text="Bugs, Marketing" bounds="[100,678][980,741]"/>
          <node class="android.widget.EditText" content-desc="Purpose"
                text="A channel purpose" bounds="[100,875][980,938]"/>
        </node>
      </node>
    </hierarchy>"""

    tree = semantic_tree_from_uiautomator(xml, viewport_size=(1080, 2400))
    controls = form_controls_from_semantic_tree(tree)

    assert controls is not None
    assert [(item["kind"], item["label"]) for item in controls] == [
        ("switch", "Make Private"),
        ("text_input", "Name"),
        ("text_input", "Purpose"),
    ]


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
