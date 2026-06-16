"""Deterministic guard: rewrite a tap aimed at a file-upload control to ``upload``.

A plain tap on a dropzone / file-input opens a NATIVE OS file chooser that is outside
the page and gets cancelled by the device's file-chooser interceptor (a wasted turn —
see log 20260616_200258 Turn5, and commits 1e6426f / cee3a84). The ``upload`` action
instead injects the file via the chooser. This guard catches the vision LLM
occasionally tapping such controls even when the supervisor's instruction names a real
upload path — it is a hard backstop, not a prompt nudge (prompt rules alone did not
break the lock; cee3a84 added the same guard).
"""

from __future__ import annotations

from gui_agent.adapters.browser.actions import BrowserAction, BrowserActionDecision
from gui_agent.adapters.browser.policies import BrowserActionPolicy


def _tap_decision(x=500, y=489, description="点上传区域") -> BrowserActionDecision:
    return BrowserActionDecision(
        action=BrowserAction(action_type="tap", x=x, y=y, description=description)
    )


def _upload_decision(path: str) -> BrowserActionDecision:
    return BrowserActionDecision(
        action=BrowserAction(
            action_type="upload", x=500, y=489, file_path=path, description="上传"
        )
    )


def test_tap_on_upload_control_with_path_is_rewritten_to_upload():
    # The exact Turn5 instruction from log 20260616_200258 — a tap aimed at the dropzone.
    policy = BrowserActionPolicy()
    decision = _tap_decision()

    result = policy._postprocess(
        decision,
        "点击弹窗中显示「点击上传或者拖放」的上传区域以唤起系统文件选择器，"
        "文件路径 /Users/hyde/Downloads/交管测试专用地图_2楼.map_export",
    )

    assert result.action.action_type == "upload"
    assert result.action.file_path == "/Users/hyde/Downloads/交管测试专用地图_2楼.map_export"
    assert result.action.x == 500 and result.action.y == 489  # control center preserved


def test_tap_on_upload_control_without_path_is_left_untouched():
    # No real path in the instruction → must NOT fabricate one. Leave the tap as-is.
    policy = BrowserActionPolicy()
    decision = _tap_decision()

    result = policy._postprocess(
        decision, "点击弹窗中显示「点击上传或者拖放」的上传区域以唤起系统文件选择器"
    )

    assert result.action.action_type == "tap"
    assert result.action.file_path is None


def test_plain_import_button_tap_is_not_misrewritten():
    # Turn4: clicking the 「导入地图文件」 BUTTON opens the upload dialog — a legitimate
    # tap (opens a page dialog, NOT the native OS chooser). Trigger cues must not match.
    policy = BrowserActionPolicy()
    decision = _tap_decision(x=291, y=84, description="点击导入地图文件按钮")

    result = policy._postprocess(decision, "点击顶部的「导入地图文件」蓝色按钮")

    assert result.action.action_type == "tap"
    assert result.action.x == 291 and result.action.y == 84


def test_explicit_upload_decision_is_not_touched():
    # An already-correct upload decision passes through unchanged.
    policy = BrowserActionPolicy()
    path = "/tmp/data/report.csv"
    decision = _upload_decision(path)

    result = policy._postprocess(decision, "上传文件 /tmp/data/report.csv 到上传区域")

    assert result.action.action_type == "upload"
    assert result.action.file_path == path


def test_upload_keyword_alone_is_not_a_control():
    # 「上传成功」confirmation bar / 「上传记录」list item — the word 上传 by itself is NOT
    # a dropzone/file-input. Only true upload-control cues (上传区域/点击上传/拖放/选择文件/
    # 文件选择器) trigger.
    policy = BrowserActionPolicy()
    decision = _tap_decision(x=120, y=30, description="关闭上传成功提示")

    result = policy._postprocess(decision, "点击顶部「上传成功」提示右侧的关闭按钮")

    assert result.action.action_type == "tap"


def test_longest_path_wins_when_instruction_has_other_slashes():
    # A breadcrumb like 首页/看板 must not be mistaken for the upload path.
    policy = BrowserActionPolicy()
    decision = _tap_decision()

    result = policy._postprocess(
        decision, "点击上传区域，路径 /Users/hyde/Downloads/交管测试专用地图_2楼.map_export（首页/看板）"
    )

    assert result.action.action_type == "upload"
    assert result.action.file_path == "/Users/hyde/Downloads/交管测试专用地图_2楼.map_export"


def test_replan_prompt_knows_upload_action():
    # Regression guard: Turn5 of 20260616_200258 wasted a turn because REPLAN_PROMPT
    # had no upload rule while PLAN_PROMPT did. The two prompts must stay symmetric.
    from gui_agent.adapters.browser.supervisor.milestone.prompts import REPLAN_PROMPT

    assert "上传文件" in REPLAN_PROMPT
