# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Imported literal command templates stay inert through persistence and MCP setup."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import create_autospec

import pytest

import chrys.service.command_credentials as credentials_module
from chrys.foundation.util.env_templates import command_handles_in, resolve_env_templates
from chrys.service.mcp._connection import _create_mcp_tool
from chrys.service.mcp._http_transport import _HTTPMCPTool
from chrys.service.mcp._stdio_transport import _SafeStdioTool
from chrys.service.mcp.cache import resolve_mcp_effective_config
from chrys.service.mcp.importers import IMPORT_SOURCES, ImportContext
from chrys.service.profiles.agents.loader import load_profile_from_yaml
from chrys.service.profiles.agents.schema import AgentProfile, ToolsConfig
from chrys.service.profiles.agents.serializer import save_profile

_COMMAND = "{{!credential-import-probe}}"


def _write_source(home: Path, source_id: str, transport: str, value: str) -> None:
    entry = (
        {"type": "http", "url": "https://example.test/mcp", "headers": {"X-Value": value}}
        if transport == "http"
        else {"command": "server", "env": {"VALUE": value}}
    )
    if source_id == "codex":
        path = home / ".codex" / "config.toml"
        setting = (
            'url = "https://example.test/mcp"\nhttp_headers = { "X-Value" = '
            if transport == "http"
            else 'command = "server"\nenv = { VALUE = '
        )
        text = "[mcp_servers.probe]\n" + setting + json.dumps(value) + " }\n"
    else:
        if source_id == "claude_code":
            path = home / ".claude.json"
        elif source_id == "pi":
            path = home / ".pi" / "agent" / "mcp.json"
        else:
            path = home / ".config" / "opencode" / "opencode.json"
            entry = (
                {"type": "remote", "url": "https://example.test/mcp", "headers": {"X-Value": value}}
                if transport == "http"
                else {"type": "local", "command": ["server"], "environment": {"VALUE": value}}
            )
        text = json.dumps({"mcp" if source_id == "opencode" else "mcpServers": {"probe": entry}})
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


@pytest.mark.parametrize("source_id", ["claude_code", "codex", "opencode", "pi"])
@pytest.mark.parametrize("transport", ["http", "stdio"])
@pytest.mark.parametrize("value", [_COMMAND, f"Bearer {_COMMAND}", "{{{{!credential-import-probe}}}}"])
async def test_literal_command_templates_survive_import_save_load_and_connection_setup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, source_id: str, transport: str, value: str
) -> None:
    home = tmp_path / "home"
    managed = tmp_path / "managed"
    managed.mkdir()
    _write_source(home, source_id, transport, value)
    context = ImportContext(None, home, {"OPENCODE_TEST_MANAGED_CONFIG_DIR": str(managed)})
    run_command = create_autospec(credentials_module._run_command)
    monkeypatch.setattr(credentials_module, "_run_command", run_command)
    # A real command with the same spelling is known already; the imported text must still stay literal.
    handle = resolve_env_templates(_COMMAND, location="another profile")
    assert command_handles_in([handle]) == {handle}

    source = next(source for source in IMPORT_SOURCES if source.source_id == source_id)
    result = source.discover(context)
    assert result.source_errors == ()
    (candidate,) = result.candidates
    assert candidate.config is not None
    profile = AgentProfile(name="Imported", tools=ToolsConfig(mcp=[candidate.config]))
    saved = save_profile(profile, target_dir=tmp_path / "profiles")
    loaded = load_profile_from_yaml(saved)
    assert loaded.tools.mcp == [candidate.config]

    (config,) = loaded.tools.mcp
    effective = resolve_mcp_effective_config(config, stdio_cwd=str(tmp_path))
    resolved = effective.http_headers if transport == "http" else effective.stdio_env
    key = ("x-value" if source_id == "codex" else "X-Value") if transport == "http" else "VALUE"
    expected = {key: value}
    assert resolved is not None
    # The effective stdio environment also includes inherited variables; inspect only this setting.
    selected = {key: resolved[key]}
    assert selected == expected
    assert command_handles_in(selected.values()) == set()
    await credentials_module.ensure_fresh(selected.values())
    assert {key: credentials_module.substitute(item) for key, item in selected.items()} == expected

    tool = _create_mcp_tool(config)
    if transport == "http":
        assert isinstance(tool, _HTTPMCPTool)
        assert tool._static_headers == expected
        assert tool._headers_hold_commands is False
    else:
        assert isinstance(tool, _SafeStdioTool)
        assert tool.env == expected
    run_command.assert_not_called()
