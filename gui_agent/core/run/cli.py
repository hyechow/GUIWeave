"""Command-line entrypoint for the agent loop runner."""

from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path

if __package__ is None or __package__ == "":
    sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from dotenv import load_dotenv
load_dotenv()

from gui_agent.core.runtime.factory import build_platform
from gui_agent.core.llm.output import generate_reply
from gui_agent.core.run.io import EscStopSignal, create_run_dir, tee_stdio
from gui_agent.core.self_learning.app_summary import (
    auto_discover_knowledge,
    load_knowledge_for_app,
    match_app_by_url,
)
from gui_agent.core.run.state import write_final_run_state
from llm.structured import get_llm_call_count, get_llm_token_usage


def main(
    *,
    run_loop=None,
    policy_builder=None,
    supervisor_builder=None,
) -> None:
    if run_loop is None or policy_builder is None or supervisor_builder is None:
        from gui_agent.core.run.loop import build_policy, build_supervisor, run_agent_loop

        run_loop = run_agent_loop
        policy_builder = build_policy
        supervisor_builder = build_supervisor

    # The platform bundle supplies the default/choice names so argparse stays in
    # sync with the registry without importing any adapter at module top.
    bundle = build_platform()
    parser = argparse.ArgumentParser(description="测试手机策略运行模式")
    parser.add_argument(
        "prompt",
        nargs="?",
        default="打开微信",
        help="目标指令，如「打开微信」「发一条朋友圈」",
    )
    parser.add_argument(
        "--policy",
        default=bundle.default_action_policy,
        choices=list(bundle.action_policy_choices),
        help="动作策略模块",
    )
    parser.add_argument(
        "--supervisor",
        default=bundle.default_supervisor,
        choices=list(bundle.supervisor_choices),
        help="监督者策略模块",
    )
    parser.add_argument(
        "--context",
        type=Path,
        help="agent-loop 可选的 context 加载路径",
    )
    parser.add_argument(
        "--max-turns",
        type=int,
        default=20,
        help="agent-loop 最大自动执行轮数，防止无限循环",
    )
    parser.add_argument(
        "--no-dynamic-max-turns",
        action="store_true",
        help="DSL 编排器模式下不按 Program 复杂度自动上调 max_turns",
    )
    parser.add_argument(
        "--auto-continue",
        action="store_true",
        help="agent-loop 动作执行后自动进入下一轮；默认手动确认",
    )
    parser.add_argument(
        "--stop-on-esc",
        action="store_true",
        help="auto-continue 模式下监听 ESC，并在当前 turn 收尾后安全停止",
    )
    parser.add_argument(
        "--headless",
        action="store_true",
        help="全后台模式：屏蔽 HUD 与动作可视化（光标/覆盖层），三平台统一；"
             "browser 还会以 headless Chromium 运行。也可用环境变量 AGENT_HEADLESS=1 开启。"
             "默认（不加）为交互模式，HUD 与可视化均开启",
    )
    parser.add_argument(
        "--no-router",
        action="store_true",
        help="跳过 router 意图改写，直接把输入当作目标（默认经 router，与 chat 模式一致）",
    )
    parser.add_argument(
        "--orchestrator",
        action="store_true",
        help="DSL 编排器模式：先把目标 decompose 成 run/if/finish 程序，由解释器排序驱动各 milestone"
             "（默认走 DAG 路径）",
    )
    args = parser.parse_args()

    # Unified headless switch (--headless flag OR env AGENT_HEADLESS): suppress the HUD and the
    # action visualizer on ALL platforms. Exported back to the env so the lazily-resolved browser
    # bundle (setup_check / open_session / visualizer all read env at call time) also runs headless
    # Chromium and skips its DOM/cursor overlay — one switch, three platforms.
    headless = args.headless or str(os.environ.get("AGENT_HEADLESS") or "").strip().lower() in {
        "1", "true", "yes", "on",
    }
    if headless:
        os.environ["AGENT_HEADLESS"] = "1"

    action_policy = policy_builder(args.policy)
    supervisor = supervisor_builder(args.supervisor)

    input_context_path = args.context
    log_dir = create_run_dir("agent-loop", bundle.platform)
    context_path = log_dir / "context.json"
    hud = None

    # ── 启动初始化:先 setup_check + 连接 device,observe 一次拿当前前台 tab 的 url/title ──
    # router 和 decompose 过去都在 device 连接前跑(router_prompt.py 自己都写「默认在当前已打开
    # 的页面」却根本不知是哪个),连截图都没有(DSL decompose 在 cli、连接在 loop)。现在提前连接、
    # observe 一次,把 url 注入 router/decompose(截图看不到地址栏,以此 url 为 ground truth),再把
    # 已开的 session 传给 run_agent_loop 复用(loop 见 platform 非空就跳过自己的 setup_check/open_session)。
    setup = bundle.setup_check()
    if not setup.ok:
        print(f"环境检查未通过：{setup.summary}")
        return

    with bundle.open_session() as platform:
        cur_url = ""
        cur_title = ""
        initial_png = None
        initial_tables = None
        cur_site = ""  # init OUTSIDE the try: an observe() failure must not leave it unbound
        try:
            initial_obs = bundle.make_perception(
                platform, log_dir / "screenshot_initial.png"
            ).observe()
            cur_url = initial_obs.url or ""
            cur_title = initial_obs.title or ""
            initial_png = initial_obs.png_bytes
            initial_tables = getattr(initial_obs, "tables", None)
            # Map the url's host to a known app name (semantic site) — the IP itself is opaque
            # to router/decompose, but "RoboTeam" / "shopping_admin" carries meaning.
            if cur_url:
                cur_site = match_app_by_url(cur_url, bundle.platform) or ""
            if cur_url or cur_site:
                _shown = cur_site or cur_url
                print(f"当前前台页面：{_shown}" + (f"（{cur_title}）" if cur_title else ""))
        except Exception as exc:  # noqa: BLE001 — never block the run on a page-info probe
            print(f"（初始页面探测失败，router/decompose 将不感知当前站点：{exc}）")

        # Route the raw input through the LLM router (now with the current front-tab url so
        # it can resolve "当前页面/这里" to a concrete site). Skipped on --context / --no-router.
        raw_input = args.prompt
        router_result = None
        goal = args.prompt
        if not args.context and not args.no_router:
            try:
                from gui_agent.core.chat.session import route_message
                router_result = route_message(
                    raw_input, session=[], prefs_context="", platform=bundle.platform,
                    current_url=cur_url, current_title=cur_title, current_site=cur_site,
                )
            except Exception as exc:
                print(f"Router  : 调用失败，回退原始输入（{exc}）")
            if router_result is not None:
                if not router_result.goal:
                    if router_result.needs_clarification:
                        print(f"Router  : 需要补充信息 — {router_result.clarification}")
                    else:
                        print("Router  : 非任务（闲聊/问答），已跳过")
                    return
                goal = router_result.goal
                print(f"Router  : {raw_input!r} → {goal!r}")

        hud = bundle.make_status_reporter(not headless)
        with tee_stdio(log_dir):
            for _line in setup.lines:
                print(_line)
            print(f"Platform: {bundle.platform}")
            print(f"Log Dir : {log_dir}")
            print(f"Context : {input_context_path if input_context_path else None}")

            # Auto-discover app knowledge from the resolved goal. Done INSIDE the tee so the
            # match / load lines land in stdout.log (they used to print before the tee and vanish);
            # the summary is also persisted to context.json (context.knowledge) for offline analysis.
            knowledge_summary: dict | None = None
            knowledge = auto_discover_knowledge(goal, bundle.platform)
            if knowledge is None and cur_site:
                knowledge = load_knowledge_for_app(cur_site, bundle.platform)
            if knowledge and hasattr(supervisor, "set_app_knowledge"):
                supervisor.set_app_knowledge(
                    knowledge.navigation,
                    app_name=knowledge.app_name,
                    elements=knowledge.elements,
                    sections=knowledge.sections,
                    check=knowledge.check,
                )
                knowledge_summary = knowledge.summary()
                print(
                    f"Knowledge: auto-loaded app={knowledge_summary['app_name']} "
                    f"(nav={knowledge_summary['nav_chars']} chars, "
                    f"elements={knowledge_summary['elements_chars']} chars, "
                    f"sections={knowledge_summary['section_count']})"
                )

            # DSL orchestrator mode (opt-in): decompose the goal into a run/if/finish program; the
            # interpreter sequences milestones instead of the supervisor's DAG walker. program=None
            # (default) → the DAG path is unchanged.
            program = None
            orchestrator_context_reports: list[dict] = []
            orchestrator_metrics: dict = {}
            run_max_turns = args.max_turns
            _subdecompose = None  # per-row sub-goal decomposer; set inside the orchestrator block
            if args.orchestrator:
                from gui_agent.core.orchestrator import decompose, estimate_program_turns
                from gui_agent.core.supervisor.milestone.helpers import resolve_file_refs
                # Resolve @<path> refs once (config field values the goal only points at) and feed
                # them to the decomposer — mirrors the DAG path, which the orchestrator's decompose
                # otherwise skipped (the LLM only saw the literal @token, never the field values).
                file_section = resolve_file_refs(goal)
                orch_started = time.perf_counter()
                orch_calls_before = get_llm_call_count()
                orch_tokens_before = get_llm_token_usage()
                # decompose finalizes the L2 structural gates centrally (passes.finalize_gates:
                # confirm-read dispatch gate + precondition ensure-state gate) — no caller wrap.
                program = decompose(goal, knowledge=knowledge.navigation if knowledge else "",
                                    file_section=file_section,
                                    current_url=cur_url, current_title=cur_title,
                                    current_site=cur_site, table_summaries=initial_tables,
                                    png_bytes=initial_png,
                                    prepare_vision_prompt_png=bundle.prepare_vision_prompt_png,
                                    context_reports=orchestrator_context_reports)
                orch_tokens_after = get_llm_token_usage()
                orchestrator_metrics = {
                    "timings": {"orchestrator.decompose": time.perf_counter() - orch_started},
                    "token_usage": {
                        "orchestrator.decompose": {
                            "input": orch_tokens_after[0] - orch_tokens_before[0],
                            "output": orch_tokens_after[1] - orch_tokens_before[1],
                        }
                    },
                    "llm_calls": get_llm_call_count() - orch_calls_before,
                }
                # The config must ALSO reach the execution-time planner deterministically — the
                # supervisor's constraints flow to every milestone's planner, and reseed never clears
                # them (LLM distillation of config into constraints proved unstable; see DAG path).
                if file_section and hasattr(supervisor, "_global_constraints"):
                    _CAP = 3000
                    supervisor._global_constraints.append(
                        file_section if len(file_section) <= _CAP
                        else file_section[:_CAP] + "\n…（配置过长已截断，其余以分解结果为准）"
                    )
                print(f"Orchestrator: 分解为 {len(program.statements)} 条语句")

                # Per-row agentic sub-goal (ForEach.body_goal): decompose a row-templated sub-goal
                # fresh at runtime, reusing the same app knowledge + normalize passes as the main
                # decompose. The runtime depth guard enforces one-level-only; the decomposer is told
                # not to nest. Lets the full agent loop solve complex per-row sub-tasks (derive →
                # search → disambiguate → open → read) instead of the decomposer pre-baking brittle
                # micro-steps. See memory typed-returns-validation / webarena-185.
                def _subdecompose(sub_goal: str):
                    return decompose(sub_goal, knowledge=knowledge.navigation if knowledge else "",
                                     current_site=cur_site,
                                     prepare_vision_prompt_png=bundle.prepare_vision_prompt_png)
                if not args.no_dynamic_max_turns:
                    run_max_turns = estimate_program_turns(program, floor=args.max_turns)
                    if run_max_turns != args.max_turns:
                        print(
                            f"Orchestrator: max_turns {args.max_turns} -> {run_max_turns} "
                            "based on program complexity"
                        )

            try:
                stop_on_esc = args.stop_on_esc and args.auto_continue
                if args.stop_on_esc and not args.auto_continue:
                    print("Interrupt: --stop-on-esc 仅在 --auto-continue 模式下启用")
                with EscStopSignal(enabled=stop_on_esc) as esc_stop:
                    if args.stop_on_esc:
                        if esc_stop.enabled:
                            print("Interrupt: 按 ESC 将在当前 turn 收尾后停止")
                        elif stop_on_esc:
                            print("Interrupt: stdin 不是 TTY，ESC 停止未启用")
                    result: dict | None = run_loop(
                        goal,
                        action_policy,
                        supervisor,
                        input_context_path,
                        log_dir,
                        context_path,
                        max_turns=run_max_turns,
                        auto_continue=args.auto_continue,
                        hud=hud,
                        raw_input=raw_input,
                        router=router_result.model_dump() if router_result else None,
                        knowledge=knowledge_summary,
                        program=program,
                        subdecompose=_subdecompose,
                        orchestrator_context_reports=[*orchestrator_context_reports, {
                            "kind": "orchestrator_metrics",
                            **orchestrator_metrics,
                        }] if orchestrator_metrics else orchestrator_context_reports,
                        stop_requested=esc_stop.requested if esc_stop.enabled else None,
                        platform=platform,
                        headless=headless,
                    )
                if result:
                    if program is not None:
                        # Orchestrator mode: the answer is the interpreter's reply (finish /
                        # auto-summary from the program's persisted reads), not a re-derivation
                        # from content_notes — that's the whole point of the structured program.
                        output = result.get("result_summary") or "（编排器未产生答复）"
                    else:
                        # Reply to the user's ORIGINAL input (parity with chat, which
                        # passes user_msg) rather than the router-rewritten goal.
                        output = generate_reply(
                            raw_input,
                            result,
                            content_notes=result.get("content_notes"),
                            collection_context=result.get("collection_context"),
                        )
                    print("\n" + "=" * 50)
                    print("最终输出")
                    print("=" * 50)
                    print(output.rstrip())
                    print("=" * 50)
                    # Persist the final reply and structured run state so the HTML report can render it.
                    try:
                        write_final_run_state(context_path, result, output)
                    except Exception as exc:
                        print(f"（输出未写入 context: {exc}）")

                # Auto-generate HTML report
                if (log_dir / "context.json").exists():
                    try:
                        from gui_agent.reports import RunnerReportBuilder, save_report
                        report_data = RunnerReportBuilder().build(log_dir)
                        report_path = save_report(report_data, log_dir / "report.html")
                        print(f"\nReport  : {report_path}")
                    except Exception as exc:
                        print(f"\nReport  : 生成失败 ({exc})")
            finally:
                if hud:
                    hud.close()
