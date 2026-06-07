"""S1 conformance gate for the core+adapter refactor.

Asserts that the existing concrete classes satisfy the platform-neutral
``policy_expr.core.contracts`` Protocols with ZERO source modifications. This is
the permanent regression boundary: if a later change drifts a method signature
or moves a capability, structural conformance breaks and this fails.

Instances are built via ``__new__`` (no ``__init__``, no I/O) for the
method-only / class-attribute Protocols; ``Observation`` is constructed for real
because its members are Pydantic instance fields.
"""

from policy_expr.core.contracts import (
    ActionPolicy,
    Device,
    FrontierEnumerator,
    KnowledgeAwareSupervisor,
    ObservationLike,
    Perception,
    PerceptionSession,
    ScrollableDevice,
    SimilarityBackend,
    StateDeduper,
    SupervisorPolicy,
    TransitionDetector,
    TreeObservationLike,
    ZeroPreemptDevice,
)

from policy_expr.adapters.iphone.client.sync_mcp import SyncMCPClient
from policy_expr.adapters.iphone.client.mirror_daemon import MirrorDaemonClient
from policy_expr.adapters.iphone.policies.structured_output import StructuredOutputPolicy
from policy_expr.adapters.iphone.supervisor.simple import SimpleSupervisorPolicy
from policy_expr.supervisor.milestone.policy import MilestoneSupervisorPolicy
from policy_expr.adapters.iphone.perception import LivePhoneSession, LivePerception
from policy_expr.adapters.iphone.recon.page_parser import PageParser
from policy_expr.adapters.iphone.recon.page_identity import PageIdentity
from policy_expr.adapters.iphone.recon.page_compare import PageComparator, EdgeIoUBackend
from policy_expr.schemas import Observation
from policy_expr.core.schema import IdentityResult, ScreenMatchDecision, ProbeAbortedError
from policy_expr.adapters.iphone.recon.utils import (
    ScreenMatchDecision as _ShimScreenMatchDecision,
    ProbeAbortedError as _ShimProbeAbortedError,
)
from policy_expr.adapters.iphone.recon.page_identity import (
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
# adapter (policy_expr.adapters.*) and no orchestration entrypoint. Drift-proof #
# prefix invariant, verified in a clean subprocess.                             #
# --------------------------------------------------------------------------- #
def test_contracts_is_a_pure_leaf_import():
    import subprocess
    import sys

    code = (
        "import sys, policy_expr.core.contracts; "
        "leaked = [m for m in sys.modules "
        "if m.startswith('policy_expr.adapters') "
        "or m in ('policy_expr.runner', 'policy_expr.chat_cli', 'policy_expr.recon_cli')]; "
        "assert not leaked, leaked"
    )
    r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr


# --------------------------------------------------------------------------- #
# core/schema (S3): neutral data types lifted out of the iphone recon adapter. #
# Must stay a pure leaf, and the adapter must re-export the SAME class objects. #
# --------------------------------------------------------------------------- #
def test_core_schema_is_a_pure_leaf_import():
    import subprocess
    import sys

    code = (
        "import sys, policy_expr.core.schema; "
        "leaked = [m for m in sys.modules if m.startswith('policy_expr.adapters')]; "
        "assert not leaked, leaked"
    )
    r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr


def test_adapter_reexports_core_schema_types():
    # The former adapter locations must re-export the exact same class objects, so
    # isinstance / contract conformance is unaffected by the move to core.schema.
    assert _ShimIdentityResult is IdentityResult
    assert _ShimScreenMatchDecision is ScreenMatchDecision
    assert _ShimProbeAbortedError is ProbeAbortedError


def test_core_schema_types_construct():
    assert IdentityResult(is_duplicate=False, reason="new").is_duplicate is False
    decision = ScreenMatchDecision(matched=True, similarity=1.0, method="m", reason="r")
    assert decision.matched is True
    assert isinstance(ProbeAbortedError("x", 0, "el", []), RuntimeError)


# --------------------------------------------------------------------------- #
# core/factory (S3): the platform seam. The neutral dispatcher must stay        #
# adapter-free (lazy import); the iphone bundle must be well-formed and the      #
# dispatcher must reject unknown platforms.                                      #
# --------------------------------------------------------------------------- #
def test_core_factory_is_a_pure_leaf_import():
    import subprocess
    import sys

    code = (
        "import sys, policy_expr.core.factory; "
        "leaked = [m for m in sys.modules if m.startswith('policy_expr.adapters')]; "
        "assert not leaked, leaked"
    )
    r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr


def test_build_platform_returns_iphone_bundle():
    from policy_expr.core.factory import build_platform, PlatformBundle

    bundle = build_platform("iphone", backend="daemon")
    assert isinstance(bundle, PlatformBundle)
    assert bundle.platform == "iphone"
    assert bundle.default_action_policy and bundle.default_supervisor
    for attr in (
        "open_session",
        "make_executor",
        "make_perception",
        "make_action_policy",
        "make_supervisor",
        "make_status_reporter",
    ):
        assert callable(getattr(bundle, attr)), attr


def test_build_platform_unknown_raises():
    import pytest

    from policy_expr.core.factory import build_platform

    with pytest.raises(ValueError):
        build_platform("android")  # not registered yet


# --------------------------------------------------------------------------- #
# core/factory (S3, Step 4): the orchestration entrypoints must now depend on   #
# the platform FACTORY, not the iphone adapter. Importing runner / chat_cli      #
# must pull in NO policy_expr.adapters.* module at module top — adapters are      #
# reached only lazily inside build_platform() when the loop actually runs.        #
# --------------------------------------------------------------------------- #
def test_runner_has_no_eager_adapter_import():
    import subprocess
    import sys

    code = (
        "import sys, policy_expr.runner; "
        "leaked = [m for m in sys.modules if m.startswith('policy_expr.adapters')]; "
        "assert not leaked, leaked"
    )
    r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr


def test_chat_cli_has_no_eager_adapter_import():
    import subprocess
    import sys

    code = (
        "import sys, policy_expr.chat_cli; "
        "leaked = [m for m in sys.modules if m.startswith('policy_expr.adapters')]; "
        "assert not leaked, leaked"
    )
    r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
