"""_skill.md is a hand-maintained, `_`-prefixed knowledge sibling holding reusable
multi-step orchestrations. Like _deploy.md it must be folded into the always-on
Supervisor navigation context (so the decomposer follows a registered skill), and like
every `_`-prefixed file it must NOT leak into the retrievable per-section bodies.
"""

from __future__ import annotations

from gui_agent.core.self_learning import app_summary


def _make_app(tmp_path, *, with_skill: bool, with_update: bool = False, with_check: bool = False):
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
    if with_check:
        (app_dir / "_check.md").write_text(
            "# 验收观察规则\n- 列表「预设站点」列显示编号短形式：「10」即「s10-站点10」",
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


# ── _check.md（Checker 专用动态验收知识：静态 prompt 留通用原则,app 显示形态按 app 注入）──


def test_check_md_loaded_into_check_channel(tmp_path, monkeypatch):
    _make_app(tmp_path, with_skill=False, with_check=True)
    monkeypatch.setattr(app_summary, "KNOWLEDGE_DIR", tmp_path)
    k = app_summary.auto_discover_knowledge("在 testapp 修改预设站点", "browser")
    assert k is not None
    assert "编号短形式" in k.check          # checker 专用通道
    assert "编号短形式" not in k.navigation  # 不混入 decomposer/planner 的 navigation 通道
    assert "_check" not in k.sections       # 不作为可检索章节
    assert k.summary()["check_chars"] > 0


def test_frontmatter_metadata_is_stripped_from_loaded_knowledge(tmp_path, monkeypatch):
    app_dir = _make_app(tmp_path, with_skill=False, with_check=False)
    (app_dir / "_app.md").write_text(
        "---\n"
        "id: knowledge.browser.testapp.navigation\n"
        "source_type: knowledge_navigation\n"
        "platform: browser\n"
        "app: TestApp\n"
        "---\n"
        "# 导航\n左侧菜单：订单 / 工具",
        encoding="utf-8",
    )
    (app_dir / "_check.md").write_text(
        "---\n"
        "id: knowledge.browser.testapp.check\n"
        "source_type: knowledge_check_rules\n"
        "scope:\n"
        "  - checker\n"
        "---\n"
        "# 验收观察规则\n- 保存成功 toast 表示完成",
        encoding="utf-8",
    )
    monkeypatch.setattr(app_summary, "KNOWLEDGE_DIR", tmp_path)

    k = app_summary.auto_discover_knowledge("在 testapp 做联调实验", "browser")

    assert k is not None
    assert "---" not in k.navigation
    assert "source_type:" not in k.check
    assert k.metadata["_app"]["source_type"] == "knowledge_navigation"
    assert k.metadata["_check"]["scope"] == ["checker"]


def test_check_md_absent_is_empty(tmp_path, monkeypatch):
    _make_app(tmp_path, with_skill=False)
    monkeypatch.setattr(app_summary, "KNOWLEDGE_DIR", tmp_path)
    k = app_summary.auto_discover_knowledge("在 testapp 修改预设站点", "browser")
    assert k is not None
    assert k.check == ""


def test_deploy_frontmatter_aliases_discover_app(tmp_path, monkeypatch):
    app_dir = _make_app(tmp_path, with_skill=False)
    (app_dir / "_deploy.md").write_text(
        "---\n"
        "aliases:\n"
        "  - Magento Admin\n"
        "  - admin backend\n"
        "---\n"
        "# 部署\n入口地址：http://example.test/admin/",
        encoding="utf-8",
    )
    monkeypatch.setattr(app_summary, "KNOWLEDGE_DIR", tmp_path)

    k = app_summary.auto_discover_knowledge("在 Magento Admin 查询订单", "browser")

    assert k is not None
    assert k.app_name == "TestApp"
    assert "入口地址" in k.navigation
    assert "aliases:" not in k.navigation


def test_set_app_knowledge_stores_check():
    from gui_agent.adapters.browser.supervisor.milestone.prompts import BROWSER_MILESTONE_PROMPTS
    from gui_agent.core.supervisor.milestone import MilestoneSupervisorPolicy

    p = MilestoneSupervisorPolicy(prompts=BROWSER_MILESTONE_PROMPTS)
    assert p._check_knowledge == ""  # 未注入时为空,run_checker 不加动态段
    p.set_app_knowledge("nav", app_name="A", check="列内「10」即「s10-站点10」")
    assert "s10-站点10" in p._check_knowledge


# ── list_known_apps（router 知识库感知的数据源）─────────────────────────────


def test_list_known_apps_platform_scoped(tmp_path, monkeypatch):
    _make_app(tmp_path, with_skill=False)
    monkeypatch.setattr(app_summary, "KNOWLEDGE_DIR", tmp_path)
    assert app_summary.list_known_apps("browser") == ["TestApp"]
    assert app_summary.list_known_apps("iphone") == []  # 其他平台目录不存在 → 空


def test_list_known_apps_skips_empty_dirs(tmp_path, monkeypatch):
    _make_app(tmp_path, with_skill=False)
    (tmp_path / "browser" / "EmptyApp").mkdir()  # 无任何 .md 的目录不算已知应用
    monkeypatch.setattr(app_summary, "KNOWLEDGE_DIR", tmp_path)
    assert app_summary.list_known_apps("browser") == ["TestApp"]
