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
from policy_expr.policies.structured_output import StructuredOutputPolicy
from policy_expr.supervisor.simple import SimpleSupervisorPolicy
from policy_expr.supervisor.milestone.policy import MilestoneSupervisorPolicy
from policy_expr.adapters.iphone.perception import LivePhoneSession, LivePerception
from policy_expr.recon.page_parser import PageParser
from policy_expr.recon.page_identity import PageIdentity
from policy_expr.recon.page_compare import PageComparator, EdgeIoUBackend
from policy_expr.schemas import Observation


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
# The seam must stay a pure leaf: importing it alone pulls in no adapter/cyclic #
# modules (executor / runner / perception / recon). Checked in a clean process. #
# --------------------------------------------------------------------------- #
def test_contracts_is_a_pure_leaf_import():
    import subprocess
    import sys

    code = (
        "import sys, policy_expr.core.contracts; "
        "leaked = [m for m in "
        "('policy_expr.adapters.iphone.executor', 'policy_expr.runner', "
        "'policy_expr.adapters.iphone.perception', 'policy_expr.recon') "
        "if m in sys.modules]; "
        "assert not leaked, leaked"
    )
    r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
