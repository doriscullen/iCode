# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Imported STDIO environment values must survive the MCP configuration editor."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from chrys.service.mcp.importers import IMPORT_SOURCES, ImportContext, UnsupportedReason


@pytest.mark.parametrize("source_id", [source.source_id for source in IMPORT_SOURCES])
@pytest.mark.parametrize(
    "value, reason",
    [
        ("", UnsupportedReason.EMPTY_ENV_VALUE),
        ("  ", UnsupportedReason.EMPTY_ENV_VALUE),
        ("\t\n", UnsupportedReason.EMPTY_ENV_VALUE),
        ("\u3000", UnsupportedReason.EMPTY_ENV_VALUE),
        (" secret", UnsupportedReason.ENV_VALUE_WHITESPACE),
        ("secret ", UnsupportedReason.ENV_VALUE_WHITESPACE),
        ("\u3000secret\u3000", UnsupportedReason.ENV_VALUE_WHITESPACE),
        (" @TOKEN@", UnsupportedReason.ENV_VALUE_WHITESPACE),
        ("secret", None),
        ("two words", None),
        ("first\nsecond", None),
        ("@TOKEN@", None),
        ("prefix @TOKEN@ suffix", None),
    ],
)
def test_environment_values_are_importable_only_when_the_editor_preserves_them(
    tmp_path: Path, source_id: str, value: str, reason: UnsupportedReason | None
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    template = {
        "claude_code": "${TOKEN}",
        "codex": "{{TOKEN}}",
        "opencode": "{env:TOKEN}",
        "pi": "$TOKEN",
    }[source_id]
    value = value.replace("@TOKEN@", template)
    env = {"KEY": value}
    if source_id == "codex":
        path = home / ".codex" / "config.toml"
        document = '[mcp_servers.server]\ncommand = "server"\nenv = { KEY = ' + json.dumps(value) + " }\n"
        field = "mcp_servers.server.env.KEY"
        expected = value.replace("{{", "{{{{").replace("}}", "}}}}")
    elif source_id == "opencode":
        path = home / ".config" / "opencode" / "opencode.json"
        document = json.dumps({"mcp": {"server": {"type": "local", "command": ["server"], "environment": env}}})
        field = "mcp.server.environment.KEY"
        expected = value.replace(template, "{{TOKEN}}")
    else:
        path = home / (".claude.json" if source_id == "claude_code" else ".pi/agent/mcp.json")
        document = json.dumps({"mcpServers": {"server": {"command": "server", "env": env}}})
        field = "mcpServers.server.env.KEY"
        expected = value.replace(template, "{{TOKEN}}")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(document, encoding="utf-8")
    context = ImportContext(
        workspace_cwd=None,
        home=home,
        environ={"OPENCODE_TEST_MANAGED_CONFIG_DIR": str(home / "managed")},
    )
    source = next(source for source in IMPORT_SOURCES if source.source_id == source_id)

    result = source.discover(context)

    assert result.source_errors == ()
    (candidate,) = result.candidates
    assert candidate.unsupported is reason
    if reason is None:
        assert candidate.config is not None
        assert candidate.config.env == {"KEY": expected}
    else:
        assert candidate.config is None
        assert candidate.unsupported_field == field
        assert candidate.entry is not None
        assert candidate.entry["env"] == env
