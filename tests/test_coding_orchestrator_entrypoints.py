from __future__ import annotations

import sys
from types import SimpleNamespace

import pytest

from gui_agent.adapters.browser import webarena
from gui_agent.core.run import cli


def test_cli_has_one_implicit_coding_orchestrator(monkeypatch, capsys) -> None:
    bundle = SimpleNamespace(
        default_action_policy="policy",
        action_policy_choices=("policy",),
        default_supervisor="supervisor",
        supervisor_choices=("supervisor",),
    )
    monkeypatch.setattr(cli, "build_platform", lambda: bundle)
    monkeypatch.setattr(sys, "argv", ["agent-loop", "--help"])

    with pytest.raises(SystemExit) as exc:
        cli.main(
            run_loop=lambda *args, **kwargs: None,
            policy_builder=lambda name: name,
            supervisor_builder=lambda name: name,
        )

    assert exc.value.code == 0
    assert "--orchestrator" not in capsys.readouterr().out


def test_webarena_has_one_implicit_coding_orchestrator(monkeypatch, capsys) -> None:
    monkeypatch.setattr(sys, "argv", ["webarena", "--help"])

    with pytest.raises(SystemExit) as exc:
        webarena.main()

    assert exc.value.code == 0
    assert "--orchestrator" not in capsys.readouterr().out
