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
from gui_agent.adapters.iphone.supervisor.simple import SimpleSupervisorPolicy
from gui_agent.core.supervisor.milestone.policy import MilestoneSupervisorPolicy
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
    assert isinstance(_blank(SimpleSupervisorPolicy), SupervisorPolicy)
    assert isinstance(_blank(MilestoneSupervisorPolicy), SupervisorPolicy)
    assert isinstance(_blank(MilestoneSupervisorPolicy), KnowledgeAwareSupervisor)


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


def test_milestone_prompts_injected_from_adapter():
    # Semantic-neutrality seam: the iphone-flavored milestone prompts live in the
    # ADAPTER and are injected into the neutral supervisor framework; core holds only
    # the MilestonePrompts shape. Construction falls back to the iphone set (lazy).
    from gui_agent.core.supervisor.milestone import MilestonePrompts, MilestoneSupervisorPolicy
    from gui_agent.adapters.iphone.supervisor.milestone.prompts import IPHONE_MILESTONE_PROMPTS

    sup = MilestoneSupervisorPolicy()
    assert isinstance(sup._prompts, MilestonePrompts)
    assert sup._prompts is IPHONE_MILESTONE_PROMPTS
    assert all(
        getattr(sup._prompts, f)
        for f in (
            "decompose", "single_checker", "check_kind_sections", "check_section_default",
            "check_section_converge", "loop_frame", "plan", "loop_scroll", "replan",
            "stop_condition_patch",
        )
    )
    assert sup._prompts.decompose.startswith("你是 iPhone")  # iphone strings preserved verbatim


def test_milestone_vision_prompts_keep_runtime_data_out_of_templates():
    # Vision milestone prompts are stable task instructions. Runtime state (milestone,
    # constraints, checker result, history, loop summaries) enters through ContextBlock
    # and assemble_messages, not str.format placeholders in prompt assets.
    import string

    from gui_agent.adapters.android.supervisor.milestone.prompts import ANDROID_MILESTONE_PROMPTS
    from gui_agent.adapters.browser.supervisor.milestone.prompts import BROWSER_MILESTONE_PROMPTS
    from gui_agent.adapters.iphone.supervisor.milestone.prompts import IPHONE_MILESTONE_PROMPTS

    fields = ("single_checker", "plan", "loop_frame", "loop_scroll", "replan")
    forbidden = {
        "app_name_context", "milestone_name", "milestone_desc", "success_condition",
        "milestone_kind", "completion_strategy", "task_type", "constraints",
        "history_text", "kind_section", "check_status", "check_reason", "issues",
        "missing_evidence", "check_summary", "scroll_stop_condition", "frame_summary",
        "stuck_reason", "retry_count", "failure_hints", "completed_milestones",
        "tried_instructions",
    }
    for prompts in (IPHONE_MILESTONE_PROMPTS, BROWSER_MILESTONE_PROMPTS, ANDROID_MILESTONE_PROMPTS):
        for field in fields:
            template = getattr(prompts, field)
            used = {fn for _, fn, _, _ in string.Formatter().parse(template) if fn}
            assert not (used & forbidden), f"{field} still contains runtime placeholders: {used & forbidden}"


def test_core_milestone_is_leaf_without_iphone_prompts():
    # Importing the core milestone package must NOT pull the iphone adapter — its
    # prompts are imported lazily only at supervisor construction.
    import subprocess
    import sys

    code = (
        "import sys, gui_agent.core.supervisor.milestone; "
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


def test_simple_supervisor_is_not_knowledge_aware():
    assert not isinstance(_blank(SimpleSupervisorPolicy), KnowledgeAwareSupervisor)


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
    # Both browser visualizers satisfy the neutral ActionVisualizer contract (built
    # via __new__, no page/connection/daemon needed for structural conformance):
    #   - BrowserActionVisualizer : in-page DOM overlay (host-agnostic fallback)
    #   - BrowserCursorVisualizer : reuses the agent_cursor OS overlay (default)
    from gui_agent.adapters.browser.visualizer import (
        BrowserActionVisualizer,
        BrowserCursorVisualizer,
    )

    assert isinstance(_blank(BrowserActionVisualizer), ActionVisualizer)
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
    assert bundle.default_supervisor == "milestone"
    assert bundle.action_policy_choices == ("browser_vision",)
    assert bundle.supervisor_choices == ("milestone",)
    for attr in (
        "open_session",
        "setup_check",
        "make_executor",
        "make_perception",
        "make_action_policy",
        "make_supervisor",
        "make_status_reporter",
        "make_action_visualizer",
        "make_scroll_probe",
        "apply_scroll_profile",
        "make_stitch_accumulator",
        "robust_shift",
        "gray_u8",
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
    # apply_scroll_profile pins a fresh scroll action to a verified scroll point.
    from gui_agent.adapters.browser.scroll_probe import BrowserScrollProfile
    from gui_agent.core.schemas import Action

    _pinned = bundle.apply_scroll_profile(
        Action(action_type="scroll", direction="down", amount="medium"),
        BrowserScrollProfile(x=123.0, y=456.0, direction="down"),
    )
    assert _pinned.x == 123.0 and _pinned.y == 456.0 and _pinned.direction == "down"


def test_browser_scroll_collect_is_implemented():
    # Browser collection reuses the NEUTRAL core.vision.stitch algos (whole-frame content
    # band, no device mask) + a trivial deterministic-wheel scroll-probe. The runner's
    # collection branch must receive real working objects (not the old stubs).
    import io

    import numpy as np
    from PIL import Image

    from gui_agent.core.runtime.factory import build_platform
    from gui_agent.core.vision.stitch import StitchAccumulator

    bundle = build_platform("browser")
    buf = io.BytesIO()
    Image.new("RGB", (400, 800), "white").save(buf, "PNG")
    png = buf.getvalue()

    g = bundle.gray_u8(png)
    assert isinstance(g, np.ndarray)
    assert bundle.robust_shift(g, g) == (0, 0.0)            # identical frame -> no progress

    acc = bundle.make_stitch_accumulator(overlap_px=150)
    assert isinstance(acc, StitchAccumulator)
    # browser uses the whole frame as content (no iOS status bar to crop), no mask
    assert acc._content_top == 0.0 and acc._content_bot == 1.0 and acc._frame_mask is None

    probe = bundle.make_scroll_probe(None, None, None)
    assert hasattr(probe, "probe")


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
    assert bundle.default_supervisor == "milestone"
    assert bundle.action_policy_choices == ("android_vision",)
    assert bundle.supervisor_choices == ("milestone",)
    for attr in (
        "open_session",
        "setup_check",
        "make_executor",
        "make_perception",
        "make_action_policy",
        "make_supervisor",
        "make_status_reporter",
        "make_action_visualizer",
        "make_scroll_probe",
        "apply_scroll_profile",
        "make_stitch_accumulator",
        "robust_shift",
        "gray_u8",
    ):
        assert callable(getattr(bundle, attr)), attr
    # HUD only when enabled (passing False must NOT spawn the tkinter subprocess).
    assert bundle.make_status_reporter(False) is None
    # Android DOES provide a live action visualizer (agent_cursor over scrcpy).
    visualizer = bundle.make_action_visualizer(None)
    assert visualizer is not None
    assert isinstance(visualizer, ActionVisualizer)
    # apply_scroll_profile is the identity pass-through.
    assert bundle.apply_scroll_profile("ACT", "PROF") == "ACT"


def test_android_visualizer_conforms():
    # The android cursor visualizer satisfies the neutral ActionVisualizer contract
    # (built via __new__ — no agent_cursor binary / scrcpy window in the test).
    from gui_agent.adapters.android.visualizer import AndroidActionVisualizer

    assert isinstance(_blank(AndroidActionVisualizer), ActionVisualizer)


def test_android_scroll_collect_fields_are_stubs():
    # The iphone-shaped scroll/stitch helpers are only hit in the runner's
    # collection branch, which the android MVP does not support. They must raise a
    # clear NotImplementedError rather than silently returning bad objects.
    import pytest

    from gui_agent.core.runtime.factory import build_platform

    bundle = build_platform("android")
    with pytest.raises(NotImplementedError):
        bundle.make_scroll_probe(None, None, None)
    with pytest.raises(NotImplementedError):
        bundle.make_stitch_accumulator()
    with pytest.raises(NotImplementedError):
        bundle.robust_shift()
    with pytest.raises(NotImplementedError):
        bundle.gray_u8(b"")


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
