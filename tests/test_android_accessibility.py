import pytest

from gui_agent.adapters.android.accessibility import (
    collection_regions_from_uiautomator,
    form_controls_from_semantic_tree,
    semantic_tree_from_uiautomator,
)


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


def _semantic_node(
    ref: str, key: str, x: float, y: float, width: float, height: float, *,
    role: str = "button", clickable: bool = True,
) -> dict:
    return {
        "ref": ref, "key": key, "role": role, "clickable": clickable,
        "in_viewport": True,
        "rect": {"x": x, "y": y, "width": width, "height": height},
    }


def _form_controls(xml: str) -> list[dict]:
    tree = semantic_tree_from_uiautomator(
        f'<hierarchy><node class="android.widget.FrameLayout" '
        f'bounds="[0,0][1080,2400]">{xml}</node></hierarchy>',
        viewport_size=(1080, 2400),
    )
    controls = form_controls_from_semantic_tree(tree)
    assert controls is not None
    return controls


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
              bounds="[0,2056][1080,2176]">
          <node class="android.widget.TextView" text="Create New Channel"
                bounds="[72,2072][650,2160]"/>
        </node>
        <node class="android.widget.LinearLayout" clickable="true"
              bounds="[0,2176][1080,2296]">
          <node class="android.widget.TextView" text="Open Direct Message"
                bounds="[72,2192][700,2280]"/>
        </node>
      </node>
    </hierarchy>"""

    tree = semantic_tree_from_uiautomator(xml, viewport_size=(1080, 2400))
    controls = form_controls_from_semantic_tree(tree)

    assert controls is not None
    by_label = {control["label"]: control for control in controls}
    assert set(by_label) == {
        "Browse Channels", "Create New Channel", "Open Direct Message",
    }
    create = by_label["Create New Channel"]
    assert create["kind"] == "button"
    assert create["bounds"] == pytest.approx((0, 856.6667, 1000, 906.6667))
    assert create["rect"] == pytest.approx({
        "x": 500,
        "y": 881.6667,
        "w": 1000,
        "h": 50,
    })


def test_glyph_backed_multiselect_rows_expose_selection_action_points() -> None:
    xml = """<hierarchy>
      <node class="android.widget.FrameLayout" bounds="[0,0][1080,2400]">
        <node class="android.widget.LinearLayout" content-desc="alex, &#xF05E0;"
              clickable="true" selected="false" bounds="[0,510][1080,657]">
          <node class="android.widget.TextView" text="alex"
                bounds="[145,540][260,610]"/>
          <node class="android.widget.TextView" text="&#xF05E0;"
                bounds="[950,540][1032,613]"/>
        </node>
        <node class="android.widget.LinearLayout" content-desc="arjun, &#xF0766;"
              clickable="true" selected="false" bounds="[0,657][1080,804]">
          <node class="android.widget.TextView" text="arjun"
                bounds="[145,687][280,757]"/>
          <node class="android.widget.TextView" text="&#xF0766;"
                bounds="[950,687][1032,760]"/>
        </node>
      </node>
    </hierarchy>"""

    controls = form_controls_from_semantic_tree(
        semantic_tree_from_uiautomator(xml, viewport_size=(1080, 2400))
    )

    assert controls is not None
    rows = {control["label"]: control for control in controls}
    assert rows["alex"]["kind"] == "checkbox"
    assert rows["alex"]["selected"] is True
    assert rows["arjun"]["selected"] is False
    assert rows["alex"]["selection_mode"] == "multiple"
    assert rows["alex"]["action_point"] == pytest.approx({
        "x": 917.5926,
        "y": 240.2083,
    })


def test_repeated_unlabeled_leading_selectors_are_named_checkboxes() -> None:
    nodes = [
        _semantic_node("android:0.0.0.0", "scrollable region", 64.8148, 160.4167, 74.0741, 29.1667),
        _semantic_node(
            "android:0.0.0.1", "First item", 643.5185, 160.4167, 564.8148, 37.5,
            role="text", clickable=False,
        ),
        _semantic_node("android:0.0.1.0", "scrollable region", 64.8148, 285.4167, 74.0741, 29.1667),
        _semantic_node(
            "android:0.0.1.1", "Second item", 643.5185, 285.4167, 564.8148, 37.5,
            role="text", clickable=False,
        ),
        _semantic_node("android:0.1.0", "scrollable region", 64.8148, 35.4167, 74.0741, 29.1667),
        _semantic_node(
            "android:0.1.1", "Page title", 643.5185, 35.4167, 564.8148, 37.5,
            role="text", clickable=False,
        ),
    ]

    controls = form_controls_from_semantic_tree(nodes)

    assert controls is not None
    choices = {control["label"]: control for control in controls}
    assert set(choices) == {"First item", "Second item"}
    assert all(control["kind"] == "checkbox" for control in choices.values())
    assert all(control["selection_mode"] == "multiple" for control in choices.values())
    assert choices["First item"]["rect"]["y"] == pytest.approx(160.4167)


def test_documentsui_label_override_does_not_leak_to_other_apps() -> None:
    controls = _form_controls("""
      <node class="android.widget.LinearLayout" resource-id="app:id/item_root"
            content-desc="Open report" clickable="true" bounds="[20,480][520,760]">
        <node class="android.widget.ImageButton" resource-id="app:id/preview_icon"
              clickable="true" bounds="[20,480][120,580]"/>
        <node class="android.widget.TextView" resource-id="app:id/title" text="report.pdf"
              bounds="[80,650][360,720]"/>
      </node>""")
    assert controls[0]["label"] == "Open report"


def test_documentsui_root_rows_use_full_row_center() -> None:
    controls = _form_controls("""
      <node class="android.widget.ListView"
            resource-id="com.google.android.documentsui:id/roots_list"
            bounds="[0,258][735,2337]">
        <node class="android.widget.LinearLayout" bounds="[0,1228][735,1375]">
          <node class="android.widget.TextView" text="sdk_gphone64_x86_64"
                resource-id="android:id/title" bounds="[168,1276][714,1327]"/>
        </node>
      </node>""")

    assert len(controls) == 1
    row = controls[0]
    assert (row["label"], row["ref"], row["kind"]) == (
        "sdk_gphone64_x86_64", "android:0.0.0", "button",
    )
    assert row["bounds"] == pytest.approx((0, 511.6667, 680.5556, 572.9167))
    assert row["rect"] == pytest.approx({
        "x": 340.2778, "y": 542.2917, "w": 680.5556, "h": 61.25,
    })


def test_documentsui_distinguishes_file_tile_from_preview_button() -> None:
    controls = _form_controls("""
      <node class="android.widget.LinearLayout"
            resource-id="com.google.android.documentsui:id/item_root"
            content-desc="Preview the file review_v2.pdf" clickable="true"
            bounds="[551,1302][1016,1903]">
        <node class="android.widget.TextView" text="review_v2.pdf"
              resource-id="com.google.android.documentsui:id/title"
              bounds="[610,1750][900,1815]"/>
        <node class="android.widget.ImageButton"
              resource-id="com.google.android.documentsui:id/preview_icon"
              content-desc="Preview the file review_v2.pdf" clickable="true"
              bounds="[890,1302][1016,1428]"/>
      </node>""")

    assert [(control["label"], control["resource"]) for control in controls] == [
        ("review_v2.pdf", "item_root"),
        ("Preview the file review_v2.pdf", "preview_icon"),
    ]
    assert controls[0]["rect"]["x"] == pytest.approx(725.463)
    assert controls[1]["rect"]["x"] == pytest.approx(882.407)


def test_android_autocomplete_search_is_text_input() -> None:
    controls = _form_controls("""
      <node class="android.widget.AutoCompleteTextView" text="review"
            content-desc="Search documents" resource-id="search_src_text"
            clickable="true" focused="true" bounds="[210,147][1059,242]"/>""")

    assert controls[0]["kind"] == "text_input"
    assert controls[0]["is_filter"] is True
    assert controls[0]["value"] == "review"


def test_android_commit_metadata_requires_explicit_submission_semantics() -> None:
    xml = """<hierarchy>
      <node class="android.widget.FrameLayout" bounds="[0,0][1080,2400]">
        <node class="android.widget.EditText" text="Search"
              resource-id="member_search_input" bounds="[50,100][1030,240]"/>
        <node class="android.widget.Button" text="Add members"
              resource-id="channel_post_list.intro_options.add_members.action"
              clickable="true" bounds="[50,1500][360,1680]"/>
        <node class="android.widget.Button" text="Add Members"
              resource-id="add_members.selected.start.button"
              clickable="true" bounds="[50,1700][1030,1850]"/>
        <node class="android.widget.Button" content-desc="Send"
              resource-id="channel.post_draft.send_action.send.button"
              clickable="true" bounds="[850,1950][1030,2070]"/>
        <node class="android.widget.Button" text="Create New Channel"
              clickable="true" bounds="[50,250][550,370]"/>
      </node>
    </hierarchy>"""

    controls = form_controls_from_semantic_tree(
        semantic_tree_from_uiautomator(xml, viewport_size=(1080, 2400))
    )

    assert controls is not None
    by_label = {item["label"]: item for item in controls}
    assert next(item for item in controls if item["kind"] == "text_input")["is_filter"] is True
    assert "form_action" not in by_label["Add members"]
    assert by_label["Add Members"]["form_action"] == "commit"
    assert by_label["Send"]["form_action"] == "commit"
    assert "form_action" not in by_label["Create New Channel"]


def test_android_hides_trailing_action_occluded_by_row_controls() -> None:
    controls = form_controls_from_semantic_tree([
        _semantic_node("android:0.0.0.0", "+", 925.9259, 233.3333, 74.0741, 33.3333),
        _semantic_node("android:0.0.1", "Delete", 893.5185, 208.3333, 212.963, 166.6667),
        _semantic_node("android:0.1", "Visible Delete", 893.5185, 375, 212.963, 83.3333),
        _semantic_node("android:0.2", "Clipped Delete", 893.5185, 10, 212.963, 20),
    ])

    assert controls is not None
    assert {control["ref"] for control in controls} == {
        "android:0.0.0.0", "android:0.1",
    }
    assert "Visible Delete" in {control["label"] for control in controls}


def test_android_hides_thin_scrolled_action_fragments() -> None:
    controls = form_controls_from_semantic_tree([
        _semantic_node("android:0.0.0", "Favorite", 565.7, 117.1, 155.6, 1.7),
        _semantic_node("android:0.0.1", "Favorite", 565.7, 662.3, 155.6, 37.1),
    ])

    assert controls is not None
    assert [(item["label"], item["rect"]["y"]) for item in controls] == [
        ("Favorite", pytest.approx(662.3)),
    ]


@pytest.mark.parametrize("role", ["checkbox", "button"])
def test_android_hides_scrolled_control_under_fixed_overlay(role: str) -> None:
    clipped = _semantic_node(
        "android:0.0.0.0", "Clipped item", 64.8148, 868.75, 74.0741, 29.1667,
        role=role, clickable=role == "button",
    )
    if role == "checkbox":
        clipped["value"] = False
    controls = form_controls_from_semantic_tree([
        clipped,
        _semantic_node("android:0.1.0", "Select all", 64.8148, 856.25, 74.0741, 29.1667),
    ])

    assert controls is not None
    assert {control["label"] for control in controls} == {"Select all"}




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
