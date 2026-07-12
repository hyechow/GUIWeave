from gui_agent.adapters.browser.webarena import _rebase_deployment_origin


def test_deployment_origin_rebases_only_labelled_same_app_urls():
    navigation = """# deployment
- 入口地址（直接导航到这里）：http://192.168.31.57:7780/admin/
- external docs: https://example.com/help
- detail: http://192.168.31.57:7780/admin/catalog/product/
"""

    rebased = _rebase_deployment_origin(
        navigation,
        "http://192.168.1.102:7780/admin",
    )

    assert "http://192.168.1.102:7780/admin/" in rebased
    assert "http://192.168.1.102:7780/admin/catalog/product/" in rebased
    assert "https://example.com/help" in rebased
    assert "192.168.31.57" not in rebased


def test_navigation_without_deployment_entry_is_not_rewritten():
    navigation = "Open https://example.com from the current application."

    assert _rebase_deployment_origin(
        navigation,
        "http://192.168.1.102:7780/admin",
    ) == navigation
