from types import SimpleNamespace

from gui_agent.core.run.instruction_similarity import instruction_entities, instructions_are_repeated
from gui_agent.core.run.progress_monitor import ProgressMonitor


def _turn(instruction: str, *, milestone_id: str = "detail", summary: str = ""):
    return SimpleNamespace(
        supervisor=SimpleNamespace(
            instruction=instruction,
            milestone_id=milestone_id,
            summary=summary,
        )
    )


def test_instruction_entities_extract_runtime_targets():
    assert "ws10-m-yellow" in instruction_entities("点击 SKU 为 WS10-M-Yellow 的 Edit 链接")
    assert "1581" in instruction_entities("打开 ID 1581 的详情页")


def test_different_sku_edit_instructions_are_not_repeated():
    old = "点击产品列表中 SKU 为 WS10-M-Yellow 所在行的 Edit 链接"
    new = "点击产品列表中 SKU 为 WS11-M-Yellow 所在行的 Edit 链接"

    assert not instructions_are_repeated(new, old, threshold=0.6)


def test_same_sku_edit_instructions_are_repeated():
    old = "点击产品列表中 SKU 为 WS10-M-Yellow 所在行的 Edit 链接"
    new = "点击产品列表中 SKU 为 WS10-M-Yellow 所在行的 Edit 链接"

    assert instructions_are_repeated(new, old, threshold=0.6)


def test_progress_monitor_repetition_ignores_different_sku_targets():
    monitor = ProgressMonitor()
    history = [
        _turn("点击产品列表中 SKU 为 WS10-M-Yellow 所在行的 Edit 链接"),
        _turn("点击产品列表中 SKU 为 MS06-M-Yellow 所在行的 Edit 链接"),
        _turn("点击产品列表中 SKU 为 WS11-M-Yellow 所在行的 Edit 链接"),
    ]

    assert monitor.check_instruction_repetition(history, "detail") is None


def test_progress_monitor_repetition_catches_same_target():
    monitor = ProgressMonitor()
    history = [
        _turn("点击产品列表中 SKU 为 WS10-M-Yellow 所在行的 Edit 链接"),
        _turn("点击产品列表中 SKU 为 WS10-M-Yellow 所在行的 Edit 链接"),
        _turn("点击产品列表中 SKU 为 WS10-M-Yellow 所在行的 Edit 链接"),
    ]

    assert monitor.check_instruction_repetition(history, "detail") is not None
