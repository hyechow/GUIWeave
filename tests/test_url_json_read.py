from gui_agent.core.orchestrator.primitives.url_json_read import extract_json_api_reads, read_json_url_returns


def test_extract_stargazers_count_from_json_key():
    reads = extract_json_api_reads(
        {"id": 40835, "stargazers_count": 803, "watchers_count": 803},
        ["stars"],
        'stars: 在 JSON 内容中查找 "stargazers_count" 键对应的整数值。',
    )

    assert reads == {"stars": "803"}


def test_extract_contributors_from_top_level_json_array():
    reads = extract_json_api_reads(
        [{"login": "a"}, {"login": "b"}, {"login": "c"}],
        ["contributors"],
        "contributors: 统计 JSON 数组中的对象数量（即数组长度）。",
    )

    assert reads == {"contributors": "3"}


def test_read_json_url_returns_uses_embedded_url_and_fetcher():
    seen: list[str] = []

    def fetch(url: str):
        seen.append(url)
        return {"stargazers_count": 803}

    reads = read_json_url_returns(
        "在 Chrome 地址栏输入 https://api.github.com/repos/google-research/android_world 并访问",
        ["stars"],
        'stars: 在 JSON 内容中查找 "stargazers_count" 键对应的整数值。',
        fetch_json=fetch,
    )

    assert seen == ["https://api.github.com/repos/google-research/android_world"]
    assert reads == {"stars": "803"}


def test_read_json_url_returns_ignores_non_json_specs():
    reads = read_json_url_returns(
        "打开 https://example.com/page",
        ["title"],
        "title: 读取页面标题。",
        fetch_json=lambda _url: {"title": "ignored"},
    )

    assert reads is None
