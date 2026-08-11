from llm.provider_config import (
    dashscope_extra_body,
    enable_thinking_for_model,
    resolve_chat_provider_config,
)


def test_enable_thinking_is_off_for_all_models():
    # qwen3.7-* 曾被强制 thinking 开（假设该族拒收 False），但 2026-08-04 在 token-plan
    # cn-beijing 端点实测 qwen3.7-max / qwen3.7-plus 均接受 enable_thinking=false，
    # 故全部模型默认关：agent 每步调用省数百 reasoning_tokens。
    assert enable_thinking_for_model("qwen3.7-max") is False
    assert enable_thinking_for_model("qwen3.7-plus") is False
    assert enable_thinking_for_model("qwen3.5-35b-a3b") is False
    assert enable_thinking_for_model(None) is False
    assert dashscope_extra_body("qwen3.7-plus") == {"enable_thinking": False}
    assert dashscope_extra_body(None) == {"enable_thinking": False}


def test_standard_provider_reads_standard_environment(monkeypatch):
    monkeypatch.setenv("STANDARD_MODEL", "qwen3.7-plus")
    monkeypatch.setenv("STANDARD_API_KEY", "sk-test-standard")
    monkeypatch.setenv("STANDARD_BASE_URL", "http://standard.example/v1")

    cfg = resolve_chat_provider_config(provider="standard")

    assert cfg.provider == "standard"
    assert cfg.model == "qwen3.7-plus"
    assert cfg.api_key == "sk-test-standard"
    assert cfg.base_url == "http://standard.example/v1"
