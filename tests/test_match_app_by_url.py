"""match_app_by_url: map a front-tab URL's host to a known app name.

A browser URL's IP:port (http://192.168.31.57:22000/map/list) is opaque to an LLM; the
semantic value is the app NAME (RoboTeam). Each app lives on a distinct host:port, so a
host match identifies the app without parsing page paths. Used to inject a semantic site
into router/decompose instead of a bare IP.
"""

from __future__ import annotations

from gui_agent.core.self_learning import app_summary


def _fake_kb(tmp_path) -> None:
    (tmp_path / "browser" / "RoboTeam").mkdir(parents=True)
    (tmp_path / "browser" / "RoboTeam" / "_deploy.md").write_text(
        "# RoboTeam\n- 入口地址：http://1.2.3.4:22000/\n"
    )
    (tmp_path / "browser" / "shopping_admin").mkdir(parents=True)
    (tmp_path / "browser" / "shopping_admin" / "_deploy.md").write_text(
        "# shopping_admin\n- 入口地址（直接导航到这里）：http://5.6.7.8:7780/admin/\n"
    )


def test_host_match_identifies_app_regardless_of_path(tmp_path, monkeypatch):
    _fake_kb(tmp_path)
    monkeypatch.setattr(app_summary, "KNOWLEDGE_DIR", tmp_path)
    assert app_summary.match_app_by_url("http://1.2.3.4:22000/map/list", "browser") == "RoboTeam"
    assert app_summary.match_app_by_url("http://1.2.3.4:22000/", "browser") == "RoboTeam"
    assert app_summary.match_app_by_url("http://5.6.7.8:7780/admin/orders", "browser") == "shopping_admin"


def test_distinct_ports_distinguish_apps(tmp_path, monkeypatch):
    _fake_kb(tmp_path)
    monkeypatch.setattr(app_summary, "KNOWLEDGE_DIR", tmp_path)
    # Same host IP, different port → different app (host includes :port).
    assert app_summary.match_app_by_url("http://1.2.3.4:22000/", "browser") == "RoboTeam"
    assert app_summary.match_app_by_url("http://1.2.3.4:7780/", "browser") is None


def test_localhost_redirect_can_match_by_unique_port(tmp_path, monkeypatch):
    _fake_kb(tmp_path)
    monkeypatch.setattr(app_summary, "KNOWLEDGE_DIR", tmp_path)
    assert app_summary.match_app_by_url("http://localhost:22000/map/list", "browser") == "RoboTeam"
    assert app_summary.match_app_by_url("http://127.0.0.1:7780/admin/", "browser") == "shopping_admin"


def test_ambiguous_port_returns_none(tmp_path, monkeypatch):
    _fake_kb(tmp_path)
    (tmp_path / "browser" / "Other").mkdir(parents=True)
    (tmp_path / "browser" / "Other" / "_deploy.md").write_text(
        "# Other\n- 入口地址：http://9.9.9.9:22000/\n"
    )
    monkeypatch.setattr(app_summary, "KNOWLEDGE_DIR", tmp_path)
    assert app_summary.match_app_by_url("http://localhost:22000/", "browser") is None


def test_deploy_url_trailing_cjk_punctuation_does_not_break_port_fallback(tmp_path, monkeypatch):
    (tmp_path / "browser" / "shopping_admin").mkdir(parents=True)
    (tmp_path / "browser" / "shopping_admin" / "_deploy.md").write_text(
        "# shopping_admin\n- 入口地址：http://5.6.7.8:7780/admin/。也可以直接访问后台；\n"
    )
    monkeypatch.setattr(app_summary, "KNOWLEDGE_DIR", tmp_path)
    assert app_summary.match_app_by_url("http://localhost:7780/admin/", "browser") == "shopping_admin"


def test_unknown_url_returns_none(tmp_path, monkeypatch):
    _fake_kb(tmp_path)
    monkeypatch.setattr(app_summary, "KNOWLEDGE_DIR", tmp_path)
    assert app_summary.match_app_by_url("https://www.google.com/webhp", "browser") is None
    assert app_summary.match_app_by_url("", "browser") is None
    assert app_summary.match_app_by_url("not-a-url", "browser") is None


def test_wrong_platform_subtree_returns_none(tmp_path, monkeypatch):
    _fake_kb(tmp_path)
    monkeypatch.setattr(app_summary, "KNOWLEDGE_DIR", tmp_path)
    # The app exists under browser/, not iphone/ — platform-scoped.
    assert app_summary.match_app_by_url("http://1.2.3.4:22000/", "iphone") is None
