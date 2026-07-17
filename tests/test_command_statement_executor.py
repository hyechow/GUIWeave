from types import SimpleNamespace

from gui_agent.core.orchestrator import Command, OutputSpec
from gui_agent.core.orchestrator.runner import StatementInvocation
from gui_agent.core.run.statements.command import execute_command
from gui_agent.core.run.statements.observation import ObservationCursor
from gui_agent.core.schemas import Observation


class _Client:
    def __init__(self):
        self.opened = []

    def navigate(self, url):
        self.opened.append(url)


def test_command_executor_calls_deterministic_capability_and_reports_location(tmp_path):
    client = _Client()
    observation = Observation(
        png_bytes=b"png",
        source="browser",
        url="https://example.test/records",
        title="Records",
    )
    bundle = SimpleNamespace(
        make_perception=lambda _platform, _path: SimpleNamespace(
            observe=lambda: observation
        )
    )
    platform = SimpleNamespace(client=client)
    cursor = ObservationCursor(bundle=bundle, platform=platform, log_dir=tmp_path)
    invocation = StatementInvocation(
        statement=Command(
            id="open",
            capability="open_url",
            args={"url": "https://example.test/records"},
            returns={
                "url": OutputSpec(type="url"),
                "title": OutputSpec(type="text"),
            },
        ),
        args={"url": "https://example.test/records"},
    )

    outcome = execute_command(
        invocation,
        statement_index=0,
        cursor=cursor,
        platform=platform,
        status=lambda _message: None,
        say=lambda _message: None,
    )

    assert client.opened == ["https://example.test/records"]
    assert outcome.outputs == {
        "url": "https://example.test/records",
        "title": "Records",
    }


def test_command_executor_returns_kickback_when_platform_lacks_capability(tmp_path):
    observation = Observation(png_bytes=b"png", source="mobile")
    bundle = SimpleNamespace(
        make_perception=lambda _platform, _path: SimpleNamespace(
            observe=lambda: observation
        )
    )
    platform = SimpleNamespace(client=object())
    cursor = ObservationCursor(
        bundle=bundle,
        platform=platform,
        log_dir=tmp_path,
        observation=observation,
    )
    outcome = execute_command(
        StatementInvocation(
            statement=Command(id="back", capability="back")
        ),
        statement_index=0,
        cursor=cursor,
        platform=platform,
        status=lambda _message: None,
        say=lambda _message: None,
    )

    assert outcome.phase == "infeasible"
    assert outcome.kickback
