"""S1 conformance gate for the core+adapter refactor.

Asserts that the existing concrete classes satisfy the platform-neutral
``gui_agent.core.runtime.contracts`` Protocols with ZERO source modifications. This is
the permanent regression boundary: if a later change drifts a method signature
or moves a capability, structural conformance breaks and this fails.

Instances are built via ``__new__`` (no ``__init__``, no I/O) for the
method-only / class-attribute Protocols; ``Observation`` is constructed for real
because its members are Pydantic instance fields.
"""

from gui_agent.core.runtime.contracts import (
    ActionPolicy,
    ActionVisualizer,
    Device,
    FrontierEnumerator,
    KnowledgeAwareSupervisor,
    ObservationLike,
    Perception,
    PerceptionSession,
    ReconNavigator,
    ScrollableDevice,
    SimilarityBackend,
    StateDeduper,
    SupervisorPolicy,
    TransitionDetector,
    TreeObservationLike,
    ZeroPreemptDevice,
)

from gui_agent.adapters.iphone.client.sync_mcp import SyncMCPClient
from gui_agent.adapters.iphone.client.mirror_daemon import MirrorDaemonClient
from gui_agent.adapters.iphone.policies.structured_output import StructuredOutputPolicy
from gui_agent.core.supervisor.statement.policy import StatementSupervisorPolicy
from gui_agent.adapters.iphone.perception import LivePhoneSession, LivePerception
from gui_agent.adapters.iphone.recon.page_parser import PageParser
from gui_agent.adapters.iphone.recon.page_identity import PageIdentity
from gui_agent.adapters.iphone.recon.page_compare import PageComparator, EdgeIoUBackend
from gui_agent.core.schemas import Observation
from gui_agent.core.schemas.recon import IdentityResult, ScreenMatchDecision, ProbeAbortedError
from gui_agent.adapters.iphone.recon.utils import (
    ScreenMatchDecision as _ShimScreenMatchDecision,
    ProbeAbortedError as _ShimProbeAbortedError,
)
from gui_agent.adapters.iphone.recon.page_identity import (
    IdentityResult as _ShimIdentityResult,
)


def _blank(cls):
    """An instance without running ``__init__`` (avoids construction I/O)."""
    return cls.__new__(cls)


# --------------------------------------------------------------------------- #
# Positive conformance: existing classes satisfy their Protocols, zero edits.  #
# --------------------------------------------------------------------------- #
def test_both_clients_are_devices():
    assert isinstance(_blank(SyncMCPClient), Device)
    assert isinstance(_blank(MirrorDaemonClient), Device)


def test_daemon_has_scroll_and_zero_preempt_capabilities():
    md = _blank(MirrorDaemonClient)
    assert isinstance(md, ScrollableDevice)
    assert isinstance(md, ZeroPreemptDevice)


def test_action_policy_conforms():
    # Extra defaulted kwarg (verbose) on the impl still satisfies the narrower Protocol.
    assert isinstance(_blank(StructuredOutputPolicy), ActionPolicy)


def test_supervisors_conform():
    assert isinstance(_blank(StatementSupervisorPolicy), SupervisorPolicy)
    assert isinstance(_blank(StatementSupervisorPolicy), KnowledgeAwareSupervisor)


def test_perception_session_and_observe_conform():
    assert isinstance(_blank(LivePhoneSession), PerceptionSession)
    assert isinstance(_blank(LivePerception), Perception)


def test_pixel_observation_conforms():
    obs = Observation(png_bytes=b"", source="test")
    assert isinstance(obs, ObservationLike)


def test_recon_capabilities_conform():
    assert isinstance(_blank(PageParser), FrontierEnumerator)
    assert isinstance(_blank(PageIdentity), StateDeduper)
    assert isinstance(_blank(PageComparator), TransitionDetector)
    assert isinstance(_blank(EdgeIoUBackend), SimilarityBackend)


def test_statement_prompts_injected_from_adapter():
    # Semantic-neutrality seam: platform prompts are injected by the adapter factory;
    # a direct core construction stays platform-neutral.
    from gui_agent.core.supervisor.statement import StatementPrompts, StatementSupervisorPolicy
    from gui_agent.adapters.iphone.factory import _build_supervisor
    from gui_agent.adapters.iphone.supervisor.statement.prompts import IPHONE_STATEMENT_PROMPTS

    neutral = StatementSupervisorPolicy()
    assert isinstance(neutral._prompts, StatementPrompts)
    assert neutral._prompts is not IPHONE_STATEMENT_PROMPTS

    iphone = _build_supervisor(StatementSupervisorPolicy.name)
    assert iphone._prompts is IPHONE_STATEMENT_PROMPTS
    assert set(StatementPrompts.__dataclass_fields__) == {
        "image_resize",
        "home_identity_markers",
        "transition",
    }
    assert iphone._prompts.image_resize == "retina"


def test_statement_vision_prompts_keep_runtime_data_out_of_templates():
    # Runtime state enters through ContextBlock, not str.format placeholders in the one
    # shared Transition prompt.
    import string

    from gui_agent.prompts import load_prompt_text

    forbidden = {
        "app_name_context", "statement_name", "statement_desc", "success_condition",
        "statement_kind", "completion_strategy", "task_type", "constraints",
        "history_text", "kind_section", "check_status", "check_reason", "issues",
        "missing_evidence", "check_summary", "scroll_stop_condition", "frame_summary",
        "stuck_reason", "retry_count", "failure_hints", "completed_statements",
        "tried_instructions",
    }
    template = load_prompt_text("task.statement.transition")
    used = {field for _, field, _, _ in string.Formatter().parse(template) if field}
    assert not (used & forbidden)


def test_core_statement_is_leaf_without_iphone_prompts():
    # Importing the core statement package must NOT pull the iphone adapter — its
    # prompts are imported lazily only at supervisor construction.
    import subprocess
    import sys

    code = (
        "import sys, gui_agent.core.supervisor.statement; "
        "leaked = [m for m in sys.modules if m.startswith('gui_agent.adapters')]; "
        "assert not leaked, leaked"
    )
    r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr


def test_recon_navigator_conforms():
    # Step 5a: the iphone navigator satisfies
    # the neutral ReconNavigator seam the explore loop will route through in 5b.
    from gui_agent.adapters.iphone.recon.navigator import IPhoneReconNavigator

    assert isinstance(_blank(IPhoneReconNavigator), ReconNavigator)


# --------------------------------------------------------------------------- #
# Intended negatives: prove the capability split is meaningful, not decorative. #
# --------------------------------------------------------------------------- #
def test_syncmcp_lacks_daemon_only_capabilities():
    sm = _blank(SyncMCPClient)
    assert not isinstance(sm, ScrollableDevice)
    assert not isinstance(sm, ZeroPreemptDevice)


def test_pixel_observation_is_not_a_tree_observation():
    obs = Observation(png_bytes=b"", source="test")
    assert not isinstance(obs, TreeObservationLike)


# --------------------------------------------------------------------------- #
# The seam must stay a pure leaf: importing it alone must pull in NO platform   #
# adapter (gui_agent.adapters.*) and no orchestration entrypoint. Drift-proof #
# prefix invariant, verified in a clean subprocess.                             #
# --------------------------------------------------------------------------- #
def test_contracts_is_a_pure_leaf_import():
    import subprocess
    import sys

    code = (
        "import sys, gui_agent.core.runtime.contracts; "
        "leaked = [m for m in sys.modules "
        "if m.startswith('gui_agent.adapters') "
        "or m in ('gui_agent.core.runner', 'gui_agent.core.chat.cli')]; "
        "assert not leaked, leaked"
    )
    r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr


# --------------------------------------------------------------------------- #
# core/schemas/recon (S3): neutral data types lifted out of the iphone adapter. #
# Must stay a pure leaf, and the adapter must re-export the SAME class objects. #
# --------------------------------------------------------------------------- #
def test_core_schema_is_a_pure_leaf_import():
    import subprocess
    import sys

    code = (
        "import sys, gui_agent.core.schemas.recon; "
        "leaked = [m for m in sys.modules if m.startswith('gui_agent.adapters')]; "
        "assert not leaked, leaked"
    )
    r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr


def test_adapter_reexports_core_schema_types():
    # The former adapter locations must re-export the exact same class objects, so
    # isinstance / contract conformance is unaffected by the move to core.schemas.recon.
    assert _ShimIdentityResult is IdentityResult
    assert _ShimScreenMatchDecision is ScreenMatchDecision
    assert _ShimProbeAbortedError is ProbeAbortedError


def test_core_schema_types_construct():
    assert IdentityResult(is_duplicate=False, reason="new").is_duplicate is False
    decision = ScreenMatchDecision(matched=True, similarity=1.0, method="m", reason="r")
    assert decision.matched is True
    assert isinstance(ProbeAbortedError("x", 0, "el", []), RuntimeError)


# --------------------------------------------------------------------------- #
# core/runtime/factory (S3): the platform seam. The neutral dispatcher must stay #
# adapter-free (lazy import); the iphone bundle must be well-formed and the      #
# dispatcher must reject unknown platforms.                                      #
# --------------------------------------------------------------------------- #
def test_core_factory_is_a_pure_leaf_import():
    import subprocess
    import sys

    code = (
        "import sys, gui_agent.core.runtime.factory; "
        "leaked = [m for m in sys.modules if m.startswith('gui_agent.adapters')]; "
        "assert not leaked, leaked"
    )
    r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr


def test_build_platform_returns_iphone_bundle():
    from gui_agent.core.runtime.factory import build_platform, PlatformBundle

    bundle = build_platform("iphone", backend="daemon")
    assert isinstance(bundle, PlatformBundle)
    assert bundle.platform == "iphone"
    assert bundle.default_action_policy and bundle.default_supervisor
    for attr in (
        "open_session",
        "setup_check",
        "make_executor",
        "make_perception",
        "make_action_policy",
        "make_supervisor",
        "make_status_reporter",
        "make_action_visualizer",
    ):
        assert callable(getattr(bundle, attr)), attr
    # iphone has no action visualizer yet (agent_cursor wiring is a follow-up).
    assert bundle.make_action_visualizer(None) is None


def test_build_platform_unknown_raises():
    import pytest

    from gui_agent.core.runtime.factory import build_platform

    with pytest.raises(ValueError):
        build_platform("windows")  # not registered


# --------------------------------------------------------------------------- #
# Browser adapter (VISION-ONLY): the new platform must conform to the SAME       #
# neutral Protocols as iphone, with NO real Chrome connection in any test        #
# (built via __new__, exactly like the iphone conformance above).                #
# --------------------------------------------------------------------------- #
def _import_browser():
    from gui_agent.adapters.browser.device import PlaywrightDevice
    from gui_agent.adapters.browser.perception import (
        BrowserPerception,
        BrowserSession,
    )
    from gui_agent.adapters.browser.policies import BrowserActionPolicy

    return PlaywrightDevice, BrowserSession, BrowserPerception, BrowserActionPolicy


def test_browser_visualizer_conforms():
    # BrowserCursorVisualizer (built via __new__, no daemon needed for structural
    # conformance) satisfies the neutral ActionVisualizer contract; it reuses the
    # agent_cursor OS overlay and is the only browser ActionVisualizer.
    from gui_agent.adapters.browser.visualizer import BrowserCursorVisualizer

    assert isinstance(_blank(BrowserCursorVisualizer), ActionVisualizer)


def test_playwright_device_is_a_scrollable_device():
    PlaywrightDevice, *_ = _import_browser()
    dev = _blank(PlaywrightDevice)
    assert isinstance(dev, Device)
    assert isinstance(dev, ScrollableDevice)


def test_playwright_device_is_not_zero_preempt_capability():
    # The class carries zero_preempt = False as a runtime flag, so core's
    # getattr(client, "zero_preempt", False) probe correctly treats the browser
    # as NON zero-preempt (it drives the user's real Chrome).
    PlaywrightDevice, *_ = _import_browser()
    dev = _blank(PlaywrightDevice)
    assert getattr(dev, "zero_preempt", False) is False


def test_browser_session_and_perception_conform():
    _, BrowserSession, BrowserPerception, _ = _import_browser()
    assert isinstance(_blank(BrowserSession), PerceptionSession)
    assert isinstance(_blank(BrowserPerception), Perception)


def test_browser_action_policy_conforms():
    *_, BrowserActionPolicy = _import_browser()
    assert isinstance(_blank(BrowserActionPolicy), ActionPolicy)
    assert BrowserActionPolicy.name == "browser_vision"


def test_build_platform_returns_browser_bundle():
    from gui_agent.core.runtime.factory import build_platform, PlatformBundle

    bundle = build_platform("browser")
    assert isinstance(bundle, PlatformBundle)
    assert bundle.platform == "browser"
    assert bundle.default_action_policy == "browser_vision"
    assert bundle.default_supervisor == "statement"
    assert bundle.action_policy_choices == ("browser_vision",)
    assert bundle.supervisor_choices == ("statement",)
    for attr in (
        "open_session",
        "setup_check",
        "make_executor",
        "make_perception",
        "make_action_policy",
        "make_supervisor",
        "make_status_reporter",
        "make_action_visualizer",
    ):
        assert callable(getattr(bundle, attr)), attr
    # HUD is created only when enabled; disabled -> None. (Enabling spawns a real
    # tkinter overlay subprocess that locates the Chrome window, so we don't
    # construct it in the test — just assert the disabled path.)
    assert bundle.make_status_reporter(False) is None
    # Browser DOES provide a live DOM action overlay (conforms to ActionVisualizer).
    visualizer = bundle.make_action_visualizer(None)
    assert visualizer is not None
    assert isinstance(visualizer, ActionVisualizer)

def test_core_factory_stays_leaf_without_browser_adapter():
    # Importing the neutral dispatcher must pull in NEITHER any adapter NOR
    # playwright — the browser bundle is reached only lazily inside build_platform.
    import subprocess
    import sys

    code = (
        "import sys, gui_agent.core.runtime.factory; "
        "leaked = [m for m in sys.modules "
        "if m.startswith('gui_agent.adapters') "
        "or m == 'playwright' or m.startswith('playwright.')]; "
        "assert not leaked, leaked"
    )
    r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr


# --------------------------------------------------------------------------- #
# Android adapter (VISION-ONLY): the new platform must conform to the SAME        #
# neutral Protocols as iphone/browser, with NO real adb connection in any test    #
# (built via __new__, exactly like the conformance above). adbutils is reached    #
# only lazily inside AndroidDevice.connect(), never at import.                     #
# --------------------------------------------------------------------------- #
def _import_android():
    from gui_agent.adapters.android.device import AndroidDevice
    from gui_agent.adapters.android.perception import (
        AndroidPerception,
        AndroidSession,
    )
    from gui_agent.adapters.android.policies import AndroidActionPolicy

    return AndroidDevice, AndroidSession, AndroidPerception, AndroidActionPolicy


def test_android_device_is_a_scrollable_device():
    AndroidDevice, *_ = _import_android()
    dev = _blank(AndroidDevice)
    assert isinstance(dev, Device)
    assert isinstance(dev, ScrollableDevice)


def test_android_device_is_not_zero_preempt_capability():
    # adb input injects on-device (zero-preempt by nature) but is NOT the iphone
    # daemon path, so core's getattr(client, "zero_preempt", False) probe treats it
    # as ordinary input (zero_preempt = False).
    AndroidDevice, *_ = _import_android()
    dev = _blank(AndroidDevice)
    assert getattr(dev, "zero_preempt", False) is False


def test_android_session_and_perception_conform():
    _, AndroidSession, AndroidPerception, _ = _import_android()
    assert isinstance(_blank(AndroidSession), PerceptionSession)
    assert isinstance(_blank(AndroidPerception), Perception)


def test_android_action_policy_conforms():
    *_, AndroidActionPolicy = _import_android()
    assert isinstance(_blank(AndroidActionPolicy), ActionPolicy)
    assert AndroidActionPolicy.name == "android_vision"


def test_build_platform_returns_android_bundle():
    from gui_agent.core.runtime.factory import build_platform, PlatformBundle

    bundle = build_platform("android")
    assert isinstance(bundle, PlatformBundle)
    assert bundle.platform == "android"
    assert bundle.default_action_policy == "android_vision"
    assert bundle.default_supervisor == "statement"
    assert bundle.action_policy_choices == ("android_vision",)
    assert bundle.supervisor_choices == ("statement",)
    for attr in (
        "open_session",
        "setup_check",
        "make_executor",
        "make_perception",
        "make_action_policy",
        "make_supervisor",
        "make_status_reporter",
        "make_action_visualizer",
    ):
        assert callable(getattr(bundle, attr)), attr
    # HUD only when enabled (passing False must NOT spawn the tkinter subprocess).
    assert bundle.make_status_reporter(False) is None
    # Android DOES provide a live action visualizer (agent_cursor over scrcpy).
    visualizer = bundle.make_action_visualizer(None)
    assert visualizer is not None
    assert isinstance(visualizer, ActionVisualizer)


def test_android_visualizer_conforms():
    # The android cursor visualizer satisfies the neutral ActionVisualizer contract
    # (built via __new__ — no agent_cursor binary / scrcpy window in the test).
    from gui_agent.adapters.android.visualizer import AndroidActionVisualizer

    assert isinstance(_blank(AndroidActionVisualizer), ActionVisualizer)

def test_core_factory_stays_leaf_without_android_adapter():
    # Importing the neutral dispatcher must pull in NEITHER any adapter NOR
    # adbutils — the android bundle is reached only lazily inside build_platform.
    import subprocess
    import sys

    code = (
        "import sys, gui_agent.core.runtime.factory; "
        "leaked = [m for m in sys.modules "
        "if m.startswith('gui_agent.adapters') "
        "or m == 'adbutils' or m.startswith('adbutils.')]; "
        "assert not leaked, leaked"
    )
    r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr


# --------------------------------------------------------------------------- #
# core/runtime/factory (S3, Step 4): the orchestration entrypoints must now depend on   #
# the platform FACTORY, not the iphone adapter. Importing runner / chat.cli      #
# must pull in NO gui_agent.adapters.* module at module top — adapters are      #
# reached only lazily inside build_platform() when the loop actually runs.        #
# --------------------------------------------------------------------------- #
def test_runner_has_no_eager_adapter_import():
    import subprocess
    import sys

    code = (
        "import sys, gui_agent.core.runner; "
        "leaked = [m for m in sys.modules if m.startswith('gui_agent.adapters')]; "
        "assert not leaked, leaked"
    )
    r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr


def test_chat_cli_has_no_eager_adapter_import():
    import subprocess
    import sys

    code = (
        "import sys, gui_agent.core.chat.cli; "
        "leaked = [m for m in sys.modules if m.startswith('gui_agent.adapters')]; "
        "assert not leaked, leaked"
    )
    r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
