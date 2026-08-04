"""Prompt registry invariants for Markdown-backed prompt assets."""

from __future__ import annotations

import ast
import json
import re
from pathlib import Path

from gui_agent.prompts import iter_prompt_templates, load_prompt, load_prompt_text

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

    raw = load_prompt("task.orchestrator.coding")
    assert raw.rendered is False
    try:
        raw.render(anything="x")
    except PromptRegistryError:
        pass
    else:
        raise AssertionError("render() must refuse a raw prompt")


def test_coding_contract_exposes_only_public_ctx_api():
    prompt = load_prompt_text("task.orchestrator.coding")

    for method in ("ctx.reach", "ctx.query", "ctx.read", "ctx.commit"):
        assert method in prompt
    assert "filters={}" in prompt
    assert "match={" not in prompt
    assert "match_mode" not in prompt
    for retired in (
        "ctx.gui",
        "ctx.write",
        "ctx.lookup",
        "ctx.acquire",
        "ctx.interact",
        "redecompose",
    ):
        assert retired not in prompt


def test_coding_contract_keeps_settings_atomic_and_visual_retrieval_on_source():
    from gui_agent.core.orchestrator.planner import _unstructured_visual_block

    prompt = load_prompt_text("task.orchestrator.coding")
    visual_contract = _unstructured_visual_block().content

    assert "emit exactly one `commit` with all requested setting" in prompt
    assert "Do not precede it with `reach` or `read`" in prompt
    assert "Never replace a requested" in prompt
    assert "in-application search or visible-page lookup with an API" in prompt
    assert "every requested field must already" in prompt
    assert 'literal `success["fields"]` list' in prompt
    assert "must be an inline dictionary literal at every call" in prompt
    assert "never translate it into an inverse mutation" in prompt
    assert "one semantic result/view `ctx.reach`" in visual_contract
    assert "same requested fields" in visual_contract
    assert "Generic whole-page fields" in visual_contract
    assert "schema-free, source-derived creation" in prompt
    assert "no preparatory entity/view" in prompt
    assert "runtime source expression itself" in prompt
    assert "only valid call shape" in prompt
    assert "`Interface contract` is the compiler-facing resource contract" in prompt
    assert "named month without a year remains month-only" in prompt
    assert "do not inspect `.year`" in prompt


def test_statement_transition_separates_commit_boundary_from_action_family():
    prompt = load_prompt_text("task.statement.transition")

    assert "write-through 控件" in prompt
    assert "`activate + commit`" in prompt
    assert "`activate + write`" in prompt
    assert "不要求存在名为 Save/Submit 的独立按钮" in prompt


def test_coding_prompt_separates_projection_fields_from_source_filters():
    prompt = load_prompt_text("task.orchestrator.coding")

    assert "projected field is not automatically source-filterable" in prompt.lower()
    assert "completely acquired rows in Python" in prompt


def test_prompt_eval_suites_point_to_existing_paths():
    for prompt in iter_prompt_templates():
        for suite in prompt.eval_suites:
            assert (ROOT / suite).exists(), f"{prompt.id} references missing suite {suite}"


def test_migrated_prompt_constants_load_from_registry():
    from gui_agent.adapters.android.router_prompt import ANDROID_ROUTER_SYSTEM
    from gui_agent.adapters.browser.router_prompt import BROWSER_ROUTER_SYSTEM
    from gui_agent.adapters.iphone.policies.structured_output import SYSTEM_PROMPT
    from gui_agent.core.self_learning.app_summary import (
        _ELEMENTS_SYSTEM as ELEMENTS_SYS,
        _NAV_SYSTEM as NAV_SYS,
    )
    from gui_agent.core.self_learning.manual_pdf import _SECTION_SYSTEM as SECTION_SYS

    assert SYSTEM_PROMPT == load_prompt_text("task.action_policy.iphone")
    assert ANDROID_ROUTER_SYSTEM == load_prompt_text("task.router.android")
    assert BROWSER_ROUTER_SYSTEM == load_prompt_text("task.router.browser")
    assert NAV_SYS == load_prompt_text("task.self_learning.app_summary.nav_system")
    assert ELEMENTS_SYS == load_prompt_text("task.self_learning.app_summary.elements_system")
    assert SECTION_SYS == load_prompt_text("task.self_learning.manual_pdf.section_system")


def test_every_platform_router_includes_shared_goal_semantics():
    from gui_agent.core.chat.session import _router_system_for

    shared = load_prompt_text("context.router.shared_goal")
    for platform in ("android", "browser", "iphone"):
        system, _known_apps_rule = _router_system_for(platform)
        assert shared in system

    assert "Router host's current date" in shared
    assert "fixed number of days" in shared
    assert "optional implementation alternatives" in shared
    assert "original language and spelling" in shared


def test_android_router_preserves_input_language_and_literals():
    prompt = load_prompt_text("task.router.android")
    normalized = " ".join(prompt.split())

    assert "same language as the current user instruction" in prompt
    assert "Do not translate an English request into Chinese" in normalized
    assert "quoted reply text" in load_prompt_text("context.router.shared_goal")


def test_android_orchestrator_declares_launch_app_as_platform_capability():
    template = load_prompt("task.orchestrator.android")
    prompt = template.render(app_list='["Alpha", "Beta"]')

    assert 'ctx.command("launch_app", app=<application name>)' in prompt
    assert "only changes the foreground application" in prompt
    assert '["Alpha", "Beta"]' in prompt
    assert "never use an Android package" in prompt
    assert "Calendar" not in prompt


def test_large_inline_prompt_constants_are_explicitly_allowlisted():
    """Default-deny guardrail: inline LLM prompt strings live in gui_agent/prompts/,
    not in code — except for an explicit, shrinking legacy allowlist.

    FAILS when a MODULE-LEVEL assignment whose name ends in *_PROMPT / *_SYSTEM /
    *_RULE holds a string literal of >= MIN_CHARS, unless that (file, name) pair is
    in LEGACY_INLINE_PROMPTS. Scans ALL of gui_agent/ and fails by default, so an
    un-migrated or freshly-added inline prompt is caught — the old migrated-allowlist
    check only guarded files it already knew about and let new violations slip in.

    RATCHET: exceptions are pinned to specific constants. Migrate one to
    gui_agent/prompts/ and delete its line, or the test fails "stale". You also
    cannot silently add a new inline prompt to an already-allowlisted file.

    Necessary, not sufficient — a content scan has too many false positives
    (HTML/CSS/JS templates, schema prose), so the guard keys on the naming
    convention. Deliberate gaps:
      - Naming blind spot: only *_PROMPT/*_SYSTEM/*_RULE names are scanned. The
        largest LIVE blind spot is adapters/browser/webarena.py (_synthesize_response
        assigns ~1k-char system/human blocks to LOCALS sys_msg/human — no suffix AND
        function-local). Migrate those to close it (top should_migrate_soon item).
      - Function-local: HumanMessage/SystemMessage built from inline f-strings inside
        functions (core/llm/output.py, core/chat/*) are out of scope by design.
      - BinOp/JoinedStr concatenations are excluded, so a registry-backed
        `_COMMON + load_prompt_text(...)` (self_learning/knowledge.py) is not flagged.
      - Schema Field(description=...) prose is intentionally allowed in code (205
        today, longest 174 — all under threshold, none *_PROMPT/*_SYSTEM/*_RULE).
    See memory: prompts-isolation-remaining-inline.
    """
    SUFFIXES = ("PROMPT", "SYSTEM", "RULE")
    MIN_CHARS = 200
    # (repo-relative file, constant name): the ONLY module-level *_PROMPT/
    # *_SYSTEM/*_RULE string literals >= MIN_CHARS allowed in code. Migrate one
    # -> delete its line -> the ratchet tightens. All three are the iphone recon
    # pipeline (dormant since the package rename; see the audit's should_migrate_soon).
    LEGACY_INLINE_PROMPTS = {
        ("gui_agent/adapters/iphone/recon/back_nav.py", "BACK_PROMPT"),
        ("gui_agent/adapters/iphone/recon/cascade_matcher.py", "SEMANTIC_PROMPT"),
        ("gui_agent/adapters/iphone/recon/page_parser.py", "SYSTEM_PROMPT"),
    }

    actual = set()
    for path in (ROOT / "gui_agent").rglob("*.py"):
        if "__pycache__" in path.parts:
            continue
        rel = str(path.relative_to(ROOT))
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in tree.body:
            if not isinstance(node, ast.Assign):
                continue
            if not isinstance(node.value, ast.Constant) or not isinstance(node.value.value, str):
                continue
            matched = [n for n in (t.id for t in node.targets if isinstance(t, ast.Name))
                       if n.endswith(SUFFIXES)]
            if not matched:
                continue
            if len(node.value.value) >= MIN_CHARS:
                for name in matched:
                    actual.add((rel, name))

    new_violations = actual - LEGACY_INLINE_PROMPTS
    stale = LEGACY_INLINE_PROMPTS - actual
    assert not new_violations, (
        f"unallowlisted large inline prompt constant(s): {sorted(new_violations)} — "
        "move the prompt into gui_agent/prompts/ and load via load_prompt_text(), "
        "or add an explicit (file, name) entry to LEGACY_INLINE_PROMPTS if it is genuine legacy."
    )
    assert not stale, (
        f"stale allowlist entr(ies): {sorted(stale)} — the inline prompt no longer "
        "exists (migrated/renamed); delete the line from LEGACY_INLINE_PROMPTS."
    )


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
        "WebArena",
        "Olivia",
        "zip jacket",
        "tanks products",
        "customer nickname(s)",
    ]
    for prompt in iter_prompt_templates():
        if prompt.platform != "shared":
            continue
        for term in banned:
            assert term not in prompt.body, f"{prompt.id} embeds app/site fact {term!r}"


def test_non_webarena_prompts_do_not_embed_benchmark_examples():
    banned = [
        "WebArena",
        "Olivia",
        "zip jacket",
        "tanks products",
        "customer nickname(s)",
        "rating of 3 stars or below",
    ]
    for prompt in iter_prompt_templates():
        if prompt.id.startswith("task.webarena."):
            continue
        for term in banned:
            assert term not in prompt.body, f"{prompt.id} embeds benchmark example {term!r}"


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
