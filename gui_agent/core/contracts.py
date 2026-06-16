"""Platform-neutral contracts for the iphone-use core orchestration (S1).

THE SINGLE SEAM
---------------
This module is the ONE platform-neutral boundary between core orchestration
(runner / perception / executor-dispatch / recon explore loops) and the concrete
platform adapters (iphone today; browser / android later). Core depends ONLY on
the Protocols defined here; each adapter provides classes that satisfy them
*structurally* (PEP 544 ``typing.Protocol``).

DESIGN RULES (S1 = zero behaviour change)
-----------------------------------------
1.  Every Protocol member is the CORE-REQUIRED, platform-neutral *intersection*
    of what core actually calls AND what every adapter backend can provide.
2.  The existing concrete classes
        - gui_agent/client/sync_mcp.py:SyncMCPClient
        - gui_agent/client/mirror_daemon.py:MirrorDaemonClient
        - gui_agent/policies/structured_output.py:StructuredOutputPolicy
        - gui_agent/supervisor/simple.py:SimpleSupervisorPolicy
        - gui_agent/supervisor/milestone/policy.py:MilestoneSupervisorPolicy
        - gui_agent/perception.py:LivePhoneSession / LivePerception
        - gui_agent/recon/page_parser.py:PageParser
        - gui_agent/recon/page_identity.py:PageIdentity
        - gui_agent/recon/page_compare.py:PageComparator
    MUST satisfy these Protocols with NO source edits. Method names and
    signatures below are copied verbatim from those classes for that reason.
3.  Members that only ONE backend implements, or that are iphone-only
    affordances (scroll, app_switcher, spotlight, key, zero_preempt, picker,
    home/app-switch, mirror-window), are deliberately kept OFF the base
    ``Device`` Protocol. Core already probes them at the call sites via
    ``hasattr(client, "scroll")`` / ``getattr(client, "zero_preempt", False)``
    — that getattr/hasattr probe IS the de-facto capability mechanism. Folding
    them into ``Device`` would break SyncMCPClient's structural conformance, so
    they live on separate, optional capability Protocols instead.
4.  No behaviour. Pure type / Protocol definitions + short docstrings.

IMPORT HYGIENE
--------------
``contracts`` must NOT import executor / runner / perception / recon (that would
create an import cycle, since those depend on core). It imports ONLY the leaf
``gui_agent.core.schemas`` module at runtime. Recon return types are forward refs
under ``TYPE_CHECKING``: the neutral ones (IdentityResult, ScreenMatchDecision)
from ``gui_agent.core.schema``; the pixel/model-bearing ones (PageKnowledge,
PageEmbedding) from the iphone adapter -- so no heavy / cyclic import at load.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Optional, Protocol, runtime_checkable

# Leaf, dependency-free schema types are safe to import at runtime.
from gui_agent.core.schemas import (
    Action,
    ActionDecision,
    Observation,
    PolicyTurn,
    SupervisorStep,
)

if TYPE_CHECKING:
    # Imported for typing only. These live in modules that (transitively) pull
    # in heavy / adapter-coupled deps, so we never import them at runtime to
    # keep `contracts` a pure leaf seam with no cycles.
    from gui_agent.adapters.iphone.recon.cascade_matcher import PageEmbedding
    from gui_agent.adapters.iphone.recon.page_parser import PageKnowledge
    from gui_agent.core.schema import IdentityResult, ScreenMatchDecision


# ===========================================================================
# Device — platform-neutral I/O backend (phone.client)
# ===========================================================================
@runtime_checkable
class Device(Protocol):
    """Platform-neutral I/O backend, held by the perception session as
    ``.client`` and driven by the executor adapter.

    The members below are the intersection that BOTH ``SyncMCPClient`` and
    ``MirrorDaemonClient`` already satisfy structurally with zero code changes,
    AND that core orchestration actually calls.

    Capability-only / single-backend members are deliberately EXCLUDED so that
    ``SyncMCPClient`` still conforms:
      - ``scroll``      -> only MirrorDaemonClient; core guards via
                           ``hasattr(client, "scroll")`` (see ``ScrollableDevice``).
      - ``zero_preempt``-> only MirrorDaemonClient; core reads via
                           ``getattr(client, "zero_preempt", False)``
                           (see ``ZeroPreemptDevice``).
      - ``app_switcher`` / ``spotlight`` / ``key`` -> iphone-only, single
                           backend, gated by the zero_preempt path.
      - ``call_tool`` / ``list_targets`` / ``status`` -> transport-leaking or
                           diagnostic; not core-required.

    Coordinates ``x`` / ``y`` are window-local logical pixels (the executor
    converts normalized 0-1000 -> logical px before calling the Device). Every
    input method returns a status ``str`` that the executor scans for
    ``"paused"`` / ``"interrupted"`` / ``"failed"`` substrings.
    """

    # --- lifecycle (LivePhoneSession.__enter__ / __exit__) ---
    def connect(self): ...
    def close(self): ...

    # --- perception ---
    def screenshot(self) -> bytes:
        """Return the current screen as PNG bytes."""
        ...

    # --- input primitives (executor + recon nav) ---
    def tap(self, x: float, y: float) -> str: ...

    def type_text(self, text: str) -> str: ...

    def drag(
        self,
        from_x: float,
        from_y: float,
        to_x: float,
        to_y: float,
        duration_ms: int = 1000,
        cursor_mode: str | None = None,
    ) -> str: ...

    # --- navigation root ---
    # BORDERLINE: the name is iOS-flavored ("Home button"), but BOTH backends
    # expose it with an identical signature and core calls it generically as the
    # "go to root" primitive. A neutral rename (e.g. go_home) would require code
    # changes, so for S1 zero-change it stays as press_home. Flagged for a later
    # neutral pass.
    def press_home(self) -> str: ...


@runtime_checkable
class ScrollableDevice(Protocol):
    """Optional capability: zero-preempt wheel scroll through the client.

    Only ``MirrorDaemonClient`` implements ``scroll``; ``SyncMCPClient`` scrolls
    via raw Quartz in ``executor._scroll``. Core already guards with
    ``hasattr(client, "scroll")`` (executor.py). Kept OFF ``Device`` so
    SyncMCPClient still conforms to the base Protocol.

    NOTE: divergence between backends — only one implements this; the
    ``hasattr`` probe is the runtime capability check.
    """

    def scroll(
        self,
        direction: str,
        amount: int = 5,
        x: float = 159,
        y: float = 350,
    ) -> str: ...


@runtime_checkable
class ZeroPreemptDevice(Protocol):
    """Optional capability marker for the zero-preempt daemon backend.

    Only ``MirrorDaemonClient`` defines ``zero_preempt = True``. Core reads it
    via ``getattr(client, "zero_preempt", False)`` to choose the daemon vs
    mirroir input path (type_text-via-client vs paste_text; native
    app_switcher; skip osascript activate+hover before drag). It is a CAPABILITY
    ATTRIBUTE, not a method — kept separate so ``SyncMCPClient`` (which lacks the
    attr) still satisfies the base ``Device`` Protocol.
    """

    zero_preempt: bool


# ===========================================================================
# Perception — session lifecycle + observe()
# ===========================================================================
@runtime_checkable
class PerceptionSession(Protocol):
    """The neutral perception/lifecycle surface core orchestration holds as
    ``phone``.

    Satisfied structurally (zero changes) by
    ``gui_agent/perception.py:LivePhoneSession``, which already provides
    ``__enter__`` / ``__exit__`` and ``screenshot() -> bytes``. Backend
    selection (AGENT_MODE daemon/mirroir), Quartz window bounds, AX sheet
    dismissal, device-frame masking and picker geometry all stay inside the
    iphone adapter — none of that crosses this seam.
    """

    def screenshot(self) -> bytes:
        """Return the current screen as PNG bytes (no element_tree, no page_key)."""
        ...

    def __enter__(self) -> "PerceptionSession": ...

    def __exit__(self, *exc: object) -> None: ...


@runtime_checkable
class Perception(Protocol):
    """Higher-level ``observe()`` used by the agent loop.

    Satisfied structurally by ``gui_agent/perception.py:LivePerception``,
    whose ``observe()`` screenshots the session and wraps the bytes in an
    ``Observation``. The disk-write side effect is incidental, not contract.
    """

    def observe(self) -> Observation: ...


@runtime_checkable
class ObservationLike(Protocol):
    """Platform-neutral observation: pixels always; structure optionally.

    iphone: ``png_bytes`` set, ``element_tree`` / ``page_key`` left None.
    browser / android: ``png_bytes`` set, ``element_tree`` (DOM / a11y tree) and
    ``page_key`` (route / URL / activity id) populated.

    The CURRENT ``gui_agent.core.schemas.Observation`` carries only ``png_bytes`` +
    ``source``. ``runtime_checkable`` Protocols enforce attribute PRESENCE on the
    instance, so to keep the live ``Observation`` conformant with ZERO edits this
    Protocol requires ONLY ``png_bytes`` + ``source``. The optional structured
    perception (``element_tree`` / ``page_key``) is intentionally NOT declared as
    a required member here — declaring it would make ``isinstance(obs,
    ObservationLike)`` False for the current iphone ``Observation`` (which lacks
    those attrs), breaking the zero-change constraint.

    HOW TREE PLATFORMS GET THE OPTIONALS: add ``element_tree`` and ``page_key``
    as additive ``Optional[...] = None`` fields to the shared
    ``gui_agent.core.schemas.Observation`` (a one-line, behaviour-neutral change).
    Once the model declares them with None defaults, ALL instances (including
    iphone, where they stay None) carry the attrs, and the richer
    ``TreeObservationLike`` Protocol below becomes satisfiable everywhere.
    iphone simply leaves the optionals None.

    OPEN QUESTION (see design notes): land the two additive Optional fields on
    ``Observation`` now (preferred), or keep tree-carrying observations on the
    separate ``TreeObservationLike`` shape until a tree adapter exists.
    """

    png_bytes: bytes
    source: str


@runtime_checkable
class TreeObservationLike(ObservationLike, Protocol):
    """Richer observation for DOM / a11y-tree platforms (browser / android).

    Requires the optional structured perception fields in ADDITION to the
    universal ``png_bytes`` / ``source``. The current iphone ``Observation`` does
    NOT satisfy this (it has pixels only) — by design. A backend conforms once
    its observation exposes both ``element_tree`` and ``page_key`` (e.g. after
    the additive fields land on the shared ``Observation`` model, populated by
    the tree adapter and left None by iphone).
    """

    element_tree: Optional[object]
    page_key: Optional[str]


# ===========================================================================
# ActionPolicy — observation + instruction -> one action
# ===========================================================================
@runtime_checkable
class ActionPolicy(Protocol):
    """Platform-neutral action policy: maps one observation + one instruction to
    one ``ActionDecision``.

    Satisfied structurally with ZERO code changes by:
      - gui_agent/policies/base.py:BaseActionPolicy
      - gui_agent/policies/structured_output.py:StructuredOutputPolicy
        (its ``decide`` has an EXTRA defaulted kwarg ``verbose: bool = True``
         which is fine: core never passes it and a wider signature still
         conforms. The neutral Protocol must NOT include ``verbose``.)

    The keyword-only hints (``direction`` / ``drag_column`` / ``drag_steps``)
    keep neutral types here. Their concrete picker / scroll semantics
    (drag_column = year/month/day, drag_steps = picker grid cells) are
    iphone-specific; non-iphone adapters may ignore them.

    ``name`` is a class attribute used as a registry key and for logging
    (runner / chat_cli).
    """

    name: str

    def decide(
        self,
        observation: Observation,
        instruction: str,
        *,
        direction: Optional[str] = None,
        drag_column: Optional[str] = None,
        drag_steps: Optional[int] = None,
    ) -> ActionDecision:
        """Return the best action for the current observation and instruction."""
        ...


# ===========================================================================
# SupervisorPolicy — decide whether / what to do next
# ===========================================================================
@runtime_checkable
class SupervisorPolicy(Protocol):
    """Supervises task execution: observe history, decide whether/what to do next.

    Platform-neutral. ``name`` is a class attribute used for the registry/logging;
    ``step()`` drives decisions; ``runtime_state_snapshot()`` is the public
    persistence surface for supervisor-owned runtime state. Optional richer
    integrations stay in separate protocols such as ``KnowledgeAwareSupervisor``.
    """

    name: str

    def step(
        self,
        observation: Observation,
        goal: str,
        history: list[PolicyTurn],
    ) -> SupervisorStep:
        """Given current screen, goal, and full history, decide what to do next."""
        ...

    def runtime_state_snapshot(self) -> dict:
        """Return supervisor-owned runtime state for persistence."""
        ...


@runtime_checkable
class KnowledgeAwareSupervisor(Protocol):
    """Optional extension: a supervisor that accepts injected app knowledge.

    Only ``MilestoneSupervisorPolicy`` implements ``set_app_knowledge``; core
    calls it behind ``hasattr(supervisor, "set_app_knowledge")``. Kept OFF the
    strict ``SupervisorPolicy`` so ``SimpleSupervisorPolicy`` still conforms.
    """

    def set_app_knowledge(
        self, text: str, app_name: str = "", elements: str = ""
    ) -> None: ...


# ===========================================================================
# ActionVisualizer — optional cross-platform action / cursor visualization
# ===========================================================================
@runtime_checkable
class ActionVisualizer(Protocol):
    """Optional platform capability: render WHERE / WHAT the agent is acting so a
    human watching the live screen sees each action (a cursor / ripple / arrow /
    drag trace).

    SAME SPLIT AS THE REST OF THIS SEAM. The "what" is platform-neutral — the
    neutral ``Action`` (normalized 0-1000 coords + type). The "how" is
    platform-specific and lives in each adapter. The RENDERER (the agent_cursor OS
    overlay) is shared across platforms; only the coordinate mapping differs:
      - browser: BrowserCursorVisualizer drives agent_cursor via CDP window-bounds
                 -> screen mapping (DOM overlay is a host-agnostic fallback).
      - iphone : ALSO uses agent_cursor, but driven at the DEVICE layer
                 (MirrorDaemonClient), NOT through this runner-level seam — so its
                 make_action_visualizer returns None BY DESIGN. The device layer is
                 the only place with post-snap coords (OCR/YOLO snap runs inside the
                 executor, after show_action) AND coverage of every runner path
                 (main / scroll-probe / cached-scroll). Routing it here would regress
                 both. See adapters/iphone/factory.py.
      - android: TBD.

    Constructed per-session via ``PlatformBundle.make_action_visualizer(session)``
    so the renderer can hold the device / page it draws on; the factory returns
    ``None`` when a platform has no RUNNER-DRIVEN visualizer (the loop skips the hook).

    LIFECYCLE: the agent loop calls ``show_action`` immediately before the executor
    runs each action, and ``clear`` at teardown. BOTH must be best-effort and MUST
    NOT raise into the loop — visualization is cosmetic, so a render failure
    (restricted page, detached client, mid-navigation) must never abort a turn.
    """

    def show_action(self, action: Action) -> None:
        """Render the impending ``action`` at its location (flash a cursor on
        tap/type, an arrow on scroll, a trace on drag). Best-effort; never raises."""
        ...

    def clear(self) -> None:
        """Remove any active visualization (teardown / between runs). Best-effort."""
        ...


# ===========================================================================
# Recon explore capabilities (enumerate_frontier + page_key / is_new_state)
# ===========================================================================
# The two platform needs the explore strategies (dfs / bfs) lift today:
#   (1) enumerate_frontier : screen pixels -> list of tappable elements
#   (2) is_new_state       : have I seen THIS screen before? (global dedup)
# plus the closely-related transition check:
#   (2b) transition        : did a tap change the screen vs same-page refresh?
#
# These reference recon return types via forward refs (TYPE_CHECKING only) to
# avoid importing the heavy recon package into this leaf seam.


@runtime_checkable
class FrontierEnumerator(Protocol):
    """Capability (1): given the current screen (PNG), list interactable
    elements (the frontier the explore loop iterates and taps).

    Satisfied structurally by ``gui_agent/recon/page_parser.py:PageParser``,
    whose ``analyze_screen`` returns ``PageKnowledge`` (``.areas`` is the
    frontier; each ``InteractiveArea`` carries a normalized 0-1000 ``center_xy``,
    a label and a dominant ``element_type``). The contract (png -> frontier) is
    neutral; the LLM SYSTEM_PROMPT behind it is iphone-tuned (an adapter concern,
    swapped per platform).
    """

    def analyze_screen(self, png_bytes: bytes) -> "PageKnowledge": ...


@runtime_checkable
class StateDeduper(Protocol):
    """Capability (2): decide whether the current screen is a NEW state or one
    already visited, and register visited states (the ``page_key`` /
    is_new_state seam).

    Satisfied structurally by
    ``gui_agent/recon/page_identity.py:PageIdentity`` with ZERO code changes.
    DFS splits ``check`` / ``add`` (rather than ``check_and_add``) so it can
    interleave the LLM page-parse between them; the contract mirrors that split.
    Fully platform-neutral (operates on png bytes via an injected
    CascadeMatcher). The semantic-fingerprint prompt behind it is iphone-tuned
    (adapter concern). NOTE: BFS does not use this; only DFS dedups.
    """

    def check(
        self, png: bytes, precomputed: "PageEmbedding | None" = None
    ) -> "IdentityResult": ...

    def add(
        self,
        png: bytes,
        name: str = "",
        emb: "PageEmbedding | None" = None,
        form: str | None = None,
    ) -> "PageEmbedding": ...


@runtime_checkable
class TransitionDetector(Protocol):
    """Capability (2b): detect whether a tap changed the screen (navigation) vs
    a same-page state refresh — the "did this element open a new state" half of
    frontier expansion.

    Satisfied structurally by
    ``gui_agent/recon/page_compare.py:PageComparator`` with ZERO code changes.
    Used by BOTH explore loops. Distinct from ``StateDeduper`` (global "have I
    seen this screen before?"): this is per-transition ("did the screen
    change?"). Platform-neutral (png bytes only).
    """

    def detect_navigation(
        self, initial_png: bytes, after_png: bytes | None
    ) -> tuple[bool, str]: ...

    def is_same_page(
        self, reference_png: bytes, candidate_png: bytes | None
    ) -> "ScreenMatchDecision": ...


@runtime_checkable
class SimilarityBackend(Protocol):
    """Pluggable image-similarity scorer behind ``PageComparator``.

    Mirror of the already-existing neutral Protocol in
    ``gui_agent/recon/page_compare.py:SimilarityBackend``; re-declared here so
    the seam is self-contained. ``EdgeIoUBackend`` satisfies it. Platform-neutral
    by construction (png bytes in, float score out).
    """

    def similarity(self, png_a: bytes, png_b: bytes) -> float: ...


@runtime_checkable
class ReconNavigator(Protocol):
    """Capability (3): the platform-specific navigation the explore loop needs
    BEYOND tap + perceive — the part with no contract yet (Step 5a). After tapping
    an element and recording a child page, the loop must get BACK to a known page,
    dismiss popups that block exploration, describe an element for back-context, and
    escalate to manual recovery when automatic back fails. All of that is
    platform-specific:
      - iphone : back-arrow / close-button taps + edge gestures
                 (recon/back_nav · planned_back_nav · popup_nav).
      - browser: browser back / history + modal dismissal (future).

    Satisfied structurally by the iphone adapter's
    ``recon/navigator.py:IPhoneReconNavigator``, whose methods DELEGATE to the
    existing module functions with ZERO behaviour change. Today the explore loop
    (dfs/bfs) imports those functions directly; Step 5b lifts the loop to core and
    routes them through this Protocol so a browser navigator can plug in.

    ``client`` is a ``Device``; ``screenshot`` is a ``Callable[[], bytes]``;
    ``nav_stack`` is the explore loop's stack of ``(png, tap_xy)`` frames. Signatures
    mirror the current functions verbatim so the delegating impl conforms unchanged.
    """

    def make_nav_context(self, label: str, element_type: str) -> str: ...

    def return_to_initial(
        self,
        client: object,
        screenshot: object,
        nav_stack: list,
        before_back_bytes: Optional[bytes] = None,
        out_dir: object = None,
        tap_index: int = 0,
        nav_context: str = "",
        status_cb: object = None,
        selected_tab: str = "",
    ) -> tuple[bool, list]: ...

    def close_popup(
        self,
        client: object,
        screenshot_fn: object,
        current_png: Optional[bytes] = None,
        threshold: float = 0.25,
    ) -> bool: ...

    def manual_recover(
        self,
        client: object,
        screenshot: object,
        nav_stack: list,
        top_level: int,
        prompt: str = "",
        max_attempts: int = 3,
    ) -> bool: ...


__all__ = [
    # Device + capabilities
    "Device",
    "ScrollableDevice",
    "ZeroPreemptDevice",
    # Perception
    "PerceptionSession",
    "Perception",
    "ObservationLike",
    "TreeObservationLike",
    # Policies
    "ActionPolicy",
    "SupervisorPolicy",
    "KnowledgeAwareSupervisor",
    # Presentation
    "ActionVisualizer",
    # Recon explore capabilities
    "FrontierEnumerator",
    "StateDeduper",
    "TransitionDetector",
    "SimilarityBackend",
    "ReconNavigator",
]
