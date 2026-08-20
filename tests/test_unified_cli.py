from __future__ import annotations

import sys

from mapi import cli


def test_main_help_lists_cross_platform_commands(capsys, monkeypatch) -> None:
    monkeypatch.setattr(sys, "argv", ["mapi", "--help"])
    cli.main()
    output = capsys.readouterr().out
    assert "mapi init" in output
    assert "mapi maintenance" in output
    assert "mapi capabilities" in output


def test_local_init_validation_does_not_apply_systemd_suffix(tmp_path) -> None:
    from mapi.initialize import InitOptions, validate_init_options

    result = validate_init_options(
        InitOptions(
            root=tmp_path,
            mode="local",
            owner_key="owner",
            agent_subject_key="agent",
            agent_display_name="Agent",
            agent_project_key="agent-self",
            service_name="local-windows-name",
        )
    )
    assert result["service_name"] == "local-windows-name"
