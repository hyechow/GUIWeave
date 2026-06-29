"""Browser grid column self-heal (Layer 2 of the column-completeness fix).

Root case = WebArena shopping_admin task 63: the foreach declared
``returns=['ID','Customer Email','Status']`` but the Magento Orders grid did not render the
``Customer Email`` column, so the AX extractor silently dropped it → the grouping column was
empty → data_query produced no answer (live run 20260629_130033).

The collector now, before paginating, detects a declared column the grid isn't rendering,
opens the grid's "Columns" visibility control, enables the missing toggle(s), and re-reads.
General to any grid/column (matches a "Columns" button + per-column checkboxes), not hardcoded
to Magento or to Customer Email.
"""

from __future__ import annotations

from gui_agent.adapters.browser.page_read import read_grid_complete
from gui_agent.adapters.browser.semantic_page import (
    find_column_toggle_refs,
    find_columns_control_ref,
)
from gui_agent.core.schemas import Observation


def _node(role, key="", depth=0, ref=0, value=""):
    return {"role": role, "key": key, "value": value, "url": "", "ref": ref, "depth": depth}


def _grid_tree(headers, data_rows, extra=None):
    """Flat AX node list (the shape read_grid_from_tree consumes): a table at depth 0, a header
    row + data rows at depth 1, cells/headers at depth 2. `extra` appends sibling nodes."""
    tree = [_node("table", depth=0), _node("row", depth=1)]
    tree += [_node("columnheader", key=h, depth=2) for h in headers]
    for row in data_rows:
        tree.append(_node("row", depth=1))
        tree += [_node("cell", key=c, depth=2) for c in row]
    if extra:
        tree += extra
    return tree


# ── finder unit tests ────────────────────────────────────────────────────────
def test_find_columns_control_ref_matches_label():
    tree = [
        _node("button", key="Filters", ref=10),
        _node("button", key="Columns 13 of 20", ref=42),
        _node("link", key="Customer Email", ref=11),
    ]
    assert find_columns_control_ref(tree) == 42


def test_find_columns_control_ref_none_when_absent():
    tree = [_node("button", key="Filters", ref=10), _node("button", key="Export", ref=11)]
    assert find_columns_control_ref(tree) is None


def test_find_column_toggle_refs_matches_missing_fields_only():
    panel = [
        _node("menuitemcheckbox", key="ID", ref=1),
        _node("menuitemcheckbox", key="Customer Email", ref=2),
        _node("menuitemcheckbox", key="Status", ref=3),
        _node("menuitemcheckbox", key="Grand Total (Purchased)", ref=4),
    ]
    assert find_column_toggle_refs(panel, ["Customer Email"]) == [2]
    assert sorted(find_column_toggle_refs(panel, ["Customer Email", "Status"])) == [2, 3]
    assert find_column_toggle_refs(panel, ["Nonexistent Column"]) == []


def test_find_column_toggle_refs_skips_already_enabled():
    """Real Magento column checkboxes carry checked-state in `value` ("true"/"false", confirmed
    against a live AX tree). An already-ON toggle must NOT be returned — clicking it would
    un-check (revert) an enabled column."""
    panel = [
        _node("checkbox", key=" Status", value="true", ref=1),       # already shown
        _node("checkbox", key="Customer Email", value="false", ref=2),     # hidden → enable
    ]
    assert find_column_toggle_refs(panel, ["Status", "Customer Email"]) == [2]


# ── heal integration: fakes ──────────────────────────────────────────────────
class _FakeClient:
    def __init__(self):
        self.clicks: list[int] = []

    # presence of read_semantic_tree is what makes _make_collect_fn pick browser; not used here.
    def read_semantic_tree(self):  # pragma: no cover - marker only
        return []

    def click_by_ref(self, ref: int) -> str:
        self.clicks.append(ref)
        return "ok"

    def wait_settled(self, action_type=None):
        return (0.0, False)


class _FakePerc:
    def __init__(self, obs):
        self._obs = obs

    def observe(self):
        return self._obs


class _FakeBundle:
    """Returns scripted observations in call order (heal does panel-observe then grid-observe)."""

    def __init__(self, obs_seq):
        self._seq = list(obs_seq)

    def make_perception(self, platform, path):
        return _FakePerc(self._seq.pop(0))


class _FakePlatform:
    def __init__(self, client):
        self.client = client


def _obs(tree):
    return Observation(png_bytes=b"", source="eval", semantic_tree=tree)


def test_read_grid_complete_self_heals_missing_column(tmp_path):
    returns = ["ID", "Customer Email", "Status"]
    # Initial grid renders ID + Status only — Customer Email column hidden.
    grid_missing = _grid_tree(
        ["ID", "Status"],
        [["1", "Complete"], ["2", "Complete"]],
        extra=[_node("button", key="Columns", ref=900)],
    )
    # After opening the Columns control, a per-column checkbox for the hidden column appears.
    panel = _grid_tree(
        ["ID", "Status"],
        [["1", "Complete"], ["2", "Complete"]],
        extra=[
            _node("button", key="Columns", ref=900),
            _node("menuitemcheckbox", key="Customer Email", ref=901),
        ],
    )
    # After enabling it, the grid now renders Customer Email.
    grid_healed = _grid_tree(
        ["ID", "Customer Email", "Status"],
        [["1", "a@x.com", "Complete"], ["2", "b@x.com", "Complete"]],
    )

    client = _FakeClient()
    platform = _FakePlatform(client)
    bundle = _FakeBundle([_obs(panel), _obs(grid_healed)])  # panel-observe, then grid-observe

    rows = read_grid_complete(
        _obs(grid_missing), returns,
        bundle=bundle, platform=platform, log_dir=tmp_path,
    )

    assert rows is not None
    # The previously-missing column is now collected with real values for every row.
    assert [r.get("Customer Email") for r in rows] == ["a@x.com", "b@x.com"]
    # Clicks: open control (900) → enable toggle (901). The panel is NOT closed afterwards —
    # the grid updates live and is read straight through the overlay (per Admin_grid_controls:
    # never re-click/Cancel, which would toggle/revert the just-enabled column).
    assert client.clicks == [900, 901]


def test_read_grid_complete_no_columns_control_leaves_rows_for_layer1(tmp_path):
    """When there is no Columns control to heal with, the collector returns what it has (the
    column stays missing) — the platform-general foreach safety net (Layer 1) then fails
    honestly rather than the collector inventing data."""
    returns = ["ID", "Customer Email", "Status"]
    grid_missing = _grid_tree(["ID", "Status"], [["1", "Complete"], ["2", "Complete"]])

    client = _FakeClient()
    platform = _FakePlatform(client)
    bundle = _FakeBundle([])  # heal must NOT observe anything

    rows = read_grid_complete(
        _obs(grid_missing), returns,
        bundle=bundle, platform=platform, log_dir=tmp_path,
    )

    assert rows is not None
    assert all("Customer Email" not in r for r in rows)  # column genuinely absent
    assert client.clicks == []  # no heal attempted
