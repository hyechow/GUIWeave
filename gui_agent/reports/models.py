"""Report data models."""

from __future__ import annotations

from dataclasses import dataclass, field

@dataclass
class ReportStep:
    label: str
    action_type: str  # tap, home, swipe, text, none, scroll, drag
    x: float | None   # 0-1000 normalized
    y: float | None
    description: str
    annotated_before_url: str  # path to annotated screenshot (downscaled thumbnail)
    display_label: str = ""  # optional thumbnail/detail marker; defaults to T{turn}
    annotated_full_url: str = ""  # full-resolution annotated frame for click-to-zoom
    raw_screenshot_url: str = ""  # path to raw screenshot (no annotations)
    after_url: str | None = None      # screenshot after action
    status: str = ""  # ✓ ✗ ↩ ""
    timestamp: str = ""  # ISO timestamp
    index: int = 0              # context turn index
    statement_id: str = ""
    instance_id: str = ""       # statement invocation instance (foreach/retry isolate here)
    statement_executor: str = ""
    instruction: str = ""
    summary: str = ""
    timings: dict[str, float] = field(default_factory=dict)
    token_usage: dict[str, dict[str, int]] = field(default_factory=dict)  # per-module {input, output}
    llm_calls: int = 0
    action_direction: str | None = None
    action_text: str | None = None
    action_to_x: float | None = None
    action_to_y: float | None = None
    snap: dict | None = None
    sections_loaded: list[str] = field(default_factory=list)  # knowledge injected into Transition
    llm_context: list[dict] = field(default_factory=list)  # prompt snapshots + context budgets
    operation_mode: str = "interactive"  # interactive | observation | non_interactive
    non_ui: dict | None = None
    no_effect: bool = False
    action_batch: dict | None = None  # ordered atomic actions from one Worker decision


@dataclass
class ReportPage:
    title: str
    steps: list[ReportStep] = field(default_factory=list)
    statement_id: str = ""
    instance_id: str = ""
    statement_executor: str = ""
    statement_name: str = ""
    statement_description: str = ""
    statement_success: str = ""
    checklist: list[dict] = field(default_factory=list)
    verify_url: str = ""  # verification screenshot (first turn of next statement)
    verify_outcome: dict = field(default_factory=dict)  # terminal Outcome/Transition projection
    outcome_after_turn: int = 0
    outcome_timings: dict[str, float] = field(default_factory=dict)
    outcome_token_usage: dict[str, dict[str, int]] = field(default_factory=dict)
    outcome_context: list[dict] = field(default_factory=list)


@dataclass
class ReportData:
    title: str
    pages: list[ReportPage] = field(default_factory=list)
    stats: dict = field(default_factory=dict)
    statements: list[dict] = field(default_factory=list)
    orchestration_summary: str = ""  # Compact statement sequence for the report
    orchestrator: dict = field(default_factory=dict)
    models: dict[str, str] = field(default_factory=dict)  # config_key → model used this run
    raw_input: str = ""  # original human input (title); empty for old logs
    goal: str = ""       # resolved goal that drove the run
    router: dict = field(default_factory=dict)  # optional input-resolution metadata
    program_output: str = ""
    reply: str = ""
    summary: str = ""      # ProgramOutcome.summary
    phase: str = ""        # ProgramOutcome.phase
    verification: str = ""
    platform: str = ""   # run platform (browser/android); empty for old logs
    wall_clock_s: float = 0.0    # true end-to-end runner elapsed (context.wall_clock_s); 0 for old logs
    settle_s_total: float = 0.0  # Σ per-turn settle waits (post-action screen-settle)
    knowledge: dict = field(default_factory=dict)  # injected app-knowledge summary {app_name, nav_chars, elements_chars, section_count}
    knowledge_sections: list[dict] = field(default_factory=list)  # sections injected ≥1 turn: {stem, title, body} (body read from knowledge dir for the click-to-view modal)
    webarena: dict = field(default_factory=dict)  # WebArena artifact summary + agent_response.json payload
    mobileworld: dict = field(default_factory=dict)  # MobileWorld task + state-based eval verdict {task_name, goal, score, reason, base_url, adb_serial}


# ── Recon data classes ─────────────────────────────────────────

@dataclass
class ReconTap:
    index: int
    label: str
    x: float
    y: float
    navigated: bool
    after_url: str | None  # screenshot after tap
    back_seq: list[dict] = field(default_factory=list)  # {"src": data_url, "subtitle": str, "success": bool}
    identity: dict = field(default_factory=dict)  # {"is_new", "phase", "visual_sim", "text_sim", "library_size", "matched_name"}


@dataclass
class ReconFlow:
    target_page: str
    target_description: str
    flow_description: str


@dataclass
class ReconPageInfo:
    name: str            # directory name
    title: str           # from recon_result.json
    page_type: str       # list, chat, detail, etc.
    description: str
    elements_count: int
    signature: str
    annotated_url: str   # initial.png with all taps annotated
    parent: str = ""          # parent page name (for leaf/overlay pages)
    taps: list[ReconTap] = field(default_factory=list)
    flows: list[ReconFlow] = field(default_factory=list)
    knowledge: str = ""
    error: dict | None = None  # structured error if probe was aborted
    error_annotated_url: str = ""  # failed-tap screenshot annotated with back-attempt coords


@dataclass
class AppReconData:
    app_name: str
    pages: list[ReconPageInfo] = field(default_factory=list)
    stats: dict = field(default_factory=dict)
    trace: list[dict] | None = None  # from trace.json if available
    dup_warnings: list[dict] = field(default_factory=list)


@dataclass
class NavNode:
    name: str
    page: ReconPageInfo | None  # None = unexplored (not yet visited by BFS)
    via_tap: str | None = None  # label of the tap that led to this page
    children: list["NavNode"] = field(default_factory=list)
