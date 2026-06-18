"""Prompt registry invariants for Markdown-backed prompt assets."""

from __future__ import annotations

import ast
import re
from pathlib import Path

from gui_agent.prompts import iter_prompt_templates, load_prompt_text

ROOT = Path(__file__).resolve().parents[1]


def test_prompt_registry_loads_all_assets():
    prompts = iter_prompt_templates()
    assert prompts
    ids = [p.id for p in prompts]
    assert len(ids) == len(set(ids))
    for prompt in prompts:
        assert prompt.path.exists()
        assert prompt.source_type
        assert prompt.platform
        assert prompt.scope
        assert prompt.owner
        assert prompt.body.strip()


def test_rendered_flag_governs_brace_scanning():
    # The rendered/raw split is authoritative (frontmatter `rendered: true`), not inferred by
    # auto-scanning braces — that misread JSON examples in raw prompts. Contract:
    #   - raw prompts are never brace-scanned (.placeholders == ()), so JSON {...} is safe;
    #   - rendered prompts are consumed via str.format(), so their braces must all be valid
    #     NAMED placeholders (balanced, escaped JSON, no positional {}), else .format() raises.
    import string

    for p in iter_prompt_templates():
        if not p.rendered:
            assert p.placeholders == (), f"raw prompt {p.id} should not expose placeholders"
            continue
        try:
            fields = [fn for _, fn, _, _ in string.Formatter().parse(p.body)]
        except ValueError as exc:
            raise AssertionError(f"rendered prompt {p.id} has malformed/unescaped braces: {exc}")
        assert "" not in fields, f"rendered prompt {p.id} has a positional/empty {{}} placeholder"
        assert p.placeholders, f"rendered prompt {p.id} is marked rendered but has no placeholders"


def test_render_rejects_raw_prompts():
    from gui_agent.prompts import load_prompt
    from gui_agent.prompts.loader import PromptRegistryError

    raw = load_prompt("task.orchestrator.decomposer")   # consumed verbatim, not via .format
    assert raw.rendered is False
    try:
        raw.render(anything="x")
    except PromptRegistryError:
        pass
    else:
        raise AssertionError("render() must refuse a raw prompt")


def test_prompt_eval_suites_point_to_existing_paths():
    for prompt in iter_prompt_templates():
        for suite in prompt.eval_suites:
            assert (ROOT / suite).exists(), f"{prompt.id} references missing suite {suite}"


def test_migrated_prompt_constants_load_from_registry():
    from gui_agent.adapters.browser.router_prompt import BROWSER_ROUTER_SYSTEM
    from gui_agent.adapters.browser.supervisor.milestone.prompts import PLAN_PROMPT
    from gui_agent.adapters.iphone.policies.structured_output import SYSTEM_PROMPT
    from gui_agent.core.llm.reader import SYSTEM_PROMPT as READER_PROMPT

    assert PLAN_PROMPT == load_prompt_text("task.milestone.browser.planner")
    assert SYSTEM_PROMPT == load_prompt_text("task.action_policy.iphone")
    assert BROWSER_ROUTER_SYSTEM == load_prompt_text("task.router.browser")
    assert READER_PROMPT == load_prompt_text("task.reader.screenshot_text")


def test_migrated_modules_do_not_inline_large_prompt_strings():
    migrated = [
        "gui_agent/adapters/iphone/supervisor/milestone/prompts.py",
        "gui_agent/adapters/browser/supervisor/milestone/prompts.py",
        "gui_agent/adapters/android/supervisor/milestone/prompts.py",
        "gui_agent/adapters/iphone/policies/structured_output.py",
        "gui_agent/adapters/browser/policies.py",
        "gui_agent/adapters/android/policies.py",
        "gui_agent/adapters/iphone/router_prompt.py",
        "gui_agent/adapters/browser/router_prompt.py",
        "gui_agent/core/llm/output.py",
        "gui_agent/core/llm/reader.py",
        "gui_agent/core/orchestrator/decomposer.py",
        "gui_agent/core/orchestrator/structured_read.py",
        "gui_agent/core/orchestrator/data_query_repair.py",
        "gui_agent/core/supervisor/milestone/helpers.py",
        "gui_agent/core/vision/target_verify.py",
    ]
    for rel in migrated:
        tree = ast.parse((ROOT / rel).read_text(encoding="utf-8"))
        for node in tree.body:
            if not isinstance(node, ast.Assign):
                continue
            if not isinstance(node.value, ast.Constant) or not isinstance(node.value.value, str):
                continue
            names = [t.id for t in node.targets if isinstance(t, ast.Name)]
            if any(name.endswith(("PROMPT", "SYSTEM", "RULE")) for name in names):
                assert len(node.value.value) < 200, f"{rel}:{names} still inlines a large prompt"


def test_shared_prompts_do_not_embed_app_or_site_facts():
    banned = [
        "微信",
        "WeChat",
        "拼多多",
        "支付宝",
        "美团",
        "Magento",
        "shopping_admin",
        "RoboTeam",
    ]
    for prompt in iter_prompt_templates():
        if prompt.platform != "shared":
            continue
        for term in banned:
            assert term not in prompt.body, f"{prompt.id} embeds app/site fact {term!r}"


def test_adapter_prompts_do_not_embed_specific_app_ui_facts():
    # Adapter prompts may describe platform mechanisms (iPhone app switching, browser DOM,
    # Android back/home), but concrete app/site UI facts belong in knowledge/*.md.
    banned_ui_facts = [
        re.compile(r"(?:微信|WeChat).{0,16}(?:聊天列表|通讯录|发现|底部\s*Tab|tab\s*名)", re.I),
        re.compile(r"(?:拼多多|支付宝|美团).{0,16}(?:底部\s*Tab|tab\s*名|分享面板)", re.I),
        re.compile(r"(?:Magento|shopping_admin).{0,24}(?:Admin|Orders|Products|Customers|Sales|Reports)", re.I),
        re.compile(r"(?:RoboTeam).{0,24}(?:机器人列表|设备列表|控制台)", re.I),
    ]
    allowlist: set[tuple[str, str]] = set()

    for prompt in iter_prompt_templates():
        if prompt.platform in {"", "shared"}:
            continue
        for pattern in banned_ui_facts:
            match = pattern.search(prompt.body)
            if match and (prompt.id, pattern.pattern) not in allowlist:
                raise AssertionError(
                    f"{prompt.id} embeds concrete app UI fact {match.group(0)!r}; "
                    "move it to knowledge or add a narrow allowlist entry"
                )
