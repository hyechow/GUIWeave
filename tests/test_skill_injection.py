"""_skill.md is a hand-maintained, `_`-prefixed knowledge sibling holding reusable
multi-step orchestrations. Like _deploy.md it must be folded into the always-on
Supervisor navigation context (so the decomposer follows a registered skill), and like
every `_`-prefixed file it must NOT leak into the retrievable per-section bodies.
"""

from __future__ import annotations

from gui_agent.core.self_learning import app_summary


def _make_app(tmp_path, *, with_skill: bool, with_update: bool = False):
    app_dir = tmp_path / "browser" / "TestApp"
    app_dir.mkdir(parents=True)
    (app_dir / "_app.md").write_text("# 导航\n左侧菜单：订单 / 工具", encoding="utf-8")
    (app_dir / "_elements.md").write_text("# 元素\n订单列表表格", encoding="utf-8")
    # a real retrievable section page
    (app_dir / "如何创建订单.md").write_text("在订单列表点快速建单", encoding="utf-8")
    if with_skill:
        (app_dir / "_skill.md").write_text(
            "# 技能\n## skill：联调实验\n1. 建车 2. 摆位 3. 连通性 4. 建单 5. 查状态",
            encoding="utf-8",
        )
    if with_update:
        (app_dir / "_update.md").write_text(
            "# 版本更新\n## 连通性（实测）\n群组现为必填，以本文件和实际界面为准",
            encoding="utf-8",
        )
    return app_dir


def test_skill_md_folded_into_navigation(tmp_path, monkeypatch):
    _make_app(tmp_path, with_skill=True)
    monkeypatch.setattr(app_summary, "KNOWLEDGE_DIR", tmp_path)
    k = app_summary.auto_discover_knowledge("在 testapp 做联调实验", "browser")
    assert k is not None
    assert "联调实验" in k.navigation  # skill folded into the always-on nav context
    assert "左侧菜单" in k.navigation  # nav structure still present alongside it


def test_skill_md_not_loaded_as_retrievable_section(tmp_path, monkeypatch):
    _make_app(tmp_path, with_skill=True)
    monkeypatch.setattr(app_summary, "KNOWLEDGE_DIR", tmp_path)
    k = app_summary.auto_discover_knowledge("在 testapp 做联调实验", "browser")
    assert k is not None
    # `_`-prefixed files are excluded from per-section bodies; only real pages remain
    assert "_skill" not in k.sections
    assert "如何创建订单" in k.sections


def test_update_md_folded_into_navigation(tmp_path, monkeypatch):
    _make_app(tmp_path, with_skill=False, with_update=True)
    monkeypatch.setattr(app_summary, "KNOWLEDGE_DIR", tmp_path)
    k = app_summary.auto_discover_knowledge("在 testapp 做联调实验", "browser")
    assert k is not None
    assert "群组现为必填" in k.navigation  # version-update overlay folded into nav
    assert "以本文件和实际界面为准" in k.navigation  # its precedence header rides along
    assert "_update" not in k.sections  # not a retrievable section


def test_navigation_unaffected_when_no_skill(tmp_path, monkeypatch):
    _make_app(tmp_path, with_skill=False)
    monkeypatch.setattr(app_summary, "KNOWLEDGE_DIR", tmp_path)
    k = app_summary.auto_discover_knowledge("在 testapp 做联调实验", "browser")
    assert k is not None
    assert "联调实验" not in k.navigation
    assert "左侧菜单" in k.navigation
