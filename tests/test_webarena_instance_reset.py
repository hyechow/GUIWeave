from __future__ import annotations

import pytest

from gui_agent.adapters.browser import webarena


def test_remote_instance_reset_uses_canonical_webarena_config(monkeypatch) -> None:
    calls: list[tuple[str, int, tuple[str, ...]]] = []
    waits: list[tuple[str, float]] = []
    site_waits: list[tuple[str, float]] = []

    def fake_ssh(host: str, port: int, *args: str) -> str:
        calls.append((host, port, args))
        return "new-container-id" if args[:3] == ("docker", "run", "-d") else ""

    monkeypatch.setattr(webarena, "_run_reset_ssh", fake_ssh)
    monkeypatch.setattr(
        webarena,
        "_wait_for_env_ctrl_ready",
        lambda url, *, timeout: waits.append((url, timeout)),
    )
    monkeypatch.setattr(
        webarena,
        "_wait_for_site_ready",
        lambda url, *, timeout: site_waits.append((url, timeout)),
    )

    details = webarena._reset_webarena_instance(
        site="shopping_admin",
        start_url="http://192.168.1.103:7780/admin",
        ssh_port=2222,
        timeout=90,
    )

    assert calls[0][2] == (
        "docker",
        "rm",
        "-f",
        "webarena_verified_shopping_admin",
    )
    run_args = calls[1][2]
    assert run_args[:6] == (
        "docker",
        "run",
        "-d",
        "--name",
        "webarena_verified_shopping_admin",
        "-p",
    )
    assert "7780:80" in run_args
    assert "7781:8877" in run_args
    assert "WA_ENV_CTRL_EXTERNAL_SITE_URL=http://192.168.1.103:7780/" in run_args
    assert run_args[-1] == "am1n3e/webarena-verified-shopping_admin"
    assert waits == [("http://192.168.1.103:7781/status", 90)]
    assert site_waits == [("http://192.168.1.103:7780/admin", 90)]
    assert details["container_id"] == "new-container-id"
    assert details["strategy"] == "webarena_canonical_config"


def test_remote_instance_reset_rejects_unsupported_site_before_ssh(monkeypatch) -> None:
    monkeypatch.setattr(
        webarena,
        "_run_reset_ssh",
        lambda *_args, **_kwargs: pytest.fail("SSH should not be called"),
    )

    with pytest.raises(ValueError, match="not configured"):
        webarena._reset_webarena_instance(
            site="shopping",
            start_url="http://192.168.1.103:7770/",
        )


def test_reset_ssh_rejects_shell_metacharacters_before_subprocess(monkeypatch) -> None:
    monkeypatch.setattr(
        webarena.subprocess,
        "run",
        lambda *_args, **_kwargs: pytest.fail("subprocess should not be called"),
    )

    with pytest.raises(ValueError, match="unsafe remote reset argument"):
        webarena._run_reset_ssh(
            "192.168.1.103",
            2222,
            "docker",
            "rm;reboot",
        )
