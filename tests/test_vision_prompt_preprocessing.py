from __future__ import annotations

import base64
import importlib
import io
from types import SimpleNamespace

from PIL import Image


def _png_size(width: int, height: int) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (width, height), "white").save(buf, format="PNG")
    return buf.getvalue()


def _image_size_from_parts(parts: list[dict]) -> tuple[int, int]:
    for part in parts:
        if part.get("type") != "image_url":
            continue
        url = part["image_url"]["url"]
        data = base64.b64decode(url.split(",", 1)[1])
        return Image.open(io.BytesIO(data)).size
    raise AssertionError("message has no image_url part")


def _fake_cfg():
    return SimpleNamespace(model="fake-model", api_key="fake-key", base_url="http://fake")


def test_platform_bundle_vision_prompt_preprocessing():
    from gui_agent.adapters.android.factory import build_android_bundle
    from gui_agent.adapters.browser.factory import build_browser_bundle
    from gui_agent.adapters.iphone.factory import build_iphone_bundle

    png = _png_size(200, 100)

    iphone_size = Image.open(
        io.BytesIO(build_iphone_bundle().prepare_vision_prompt_png(png))
    ).size
    browser_size = Image.open(
        io.BytesIO(build_browser_bundle().prepare_vision_prompt_png(png))
    ).size
    android_size = Image.open(
        io.BytesIO(build_android_bundle().prepare_vision_prompt_png(png))
    ).size

    assert iphone_size == (100, 50)
    assert browser_size == (200, 100)
    assert android_size == (200, 100)


def test_orchestrator_decompose_uses_injected_vision_preprocessor(monkeypatch):
    mod = importlib.import_module("gui_agent.core.orchestrator.decomposer")

    captured: dict[str, tuple[int, int]] = {}

    def fake_invoke(_llm, messages, _schema):
        captured["size"] = _image_size_from_parts(messages[1].content)
        return mod._PlanDraft(goal="g", steps=[mod._StepDraft(op="finish", message="done")])

    monkeypatch.setattr(mod, "resolve_llm_config", lambda _key: _fake_cfg())
    monkeypatch.setattr(mod, "ChatOpenAI", lambda **_kwargs: object())
    monkeypatch.setattr(mod, "invoke_structured", fake_invoke)

    def prepare(_png: bytes) -> bytes:
        return _png_size(123, 45)

    mod.decompose("goal", png_bytes=_png_size(200, 100), prepare_vision_prompt_png=prepare)

    assert captured["size"] == (123, 45)


def test_structured_read_uses_injected_vision_preprocessor(monkeypatch):
    mod = importlib.import_module("gui_agent.core.orchestrator.structured_read")

    captured: dict[str, tuple[int, int]] = {}

    def fake_invoke(_llm, messages, _schema):
        captured["size"] = _image_size_from_parts(messages[1].content)
        return SimpleNamespace(reads=[])

    monkeypatch.setattr(mod, "resolve_llm_config", lambda _key: _fake_cfg())
    monkeypatch.setattr(mod, "ChatOpenAI", lambda **_kwargs: object())
    monkeypatch.setattr(mod, "invoke_structured", fake_invoke)

    def prepare(_png: bytes) -> bytes:
        return _png_size(150, 60)

    result = mod.structured_read(
        _png_size(200, 100),
        ["字段"],
        prepare_vision_prompt_png=prepare,
    )

    assert captured["size"] == (150, 60)
    assert result == {"字段": ""}


def test_content_reader_uses_injected_vision_preprocessor(monkeypatch):
    mod = importlib.import_module("gui_agent.core.llm.reader")

    captured: dict[str, tuple[int, int]] = {}

    class FakeLLM:
        def invoke(self, messages):
            captured["size"] = _image_size_from_parts(messages[1].content)
            return SimpleNamespace(content="ok")

    monkeypatch.setattr(mod, "resolve_llm_config", lambda _key: _fake_cfg())
    monkeypatch.setattr(mod, "ChatOpenAI", lambda **_kwargs: FakeLLM())

    def prepare(_png: bytes) -> bytes:
        return _png_size(177, 66)

    reader = mod.ContentReader(prepare_vision_prompt_png=prepare)

    assert reader.read(_png_size(200, 100), "goal") == "ok"
    assert captured["size"] == (177, 66)
