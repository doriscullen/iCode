# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Imported HTTP headers must be usable by iCode and its configuration editor."""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from chrys.service.mcp.importers import IMPORT_SOURCES, ImportContext, UnsupportedReason


@pytest.mark.parametrize("source_id", [source.source_id for source in IMPORT_SOURCES])
@pytest.mark.parametrize(
    "value, reason",
    [
        ("", UnsupportedReason.EMPTY_HEADER_VALUE),
        (" \t ", UnsupportedReason.EMPTY_HEADER_VALUE),
        ("café", UnsupportedReason.HEADER_VALUE_ENCODING),
        ("prefix-你好", UnsupportedReason.HEADER_VALUE_ENCODING),
        ("Bearer @TOKEN@ café", UnsupportedReason.HEADER_VALUE_ENCODING),
        ("@TOKEN@", None),
        ("Bearer @TOKEN@", None),
        ("ascii", None),
        (" \tvalue\t ", None),
    ],
)
def test_header_values_are_importable_only_when_the_target_preserves_them(
    tmp_path: Path, source_id: str, value: str, reason: UnsupportedReason | None, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Import must not resolve dynamic values, even if the current value is incompatible.
    monkeypatch.setenv("TOKEN", "你好")
    home = tmp_path / "home"
    template = {"claude_code": "${TOKEN}", "codex": "{{TOKEN}}", "opencode": "{env:TOKEN}", "pi": "$TOKEN"}[source_id]
    value = value.replace("@TOKEN@", template)
    headers = {"X-Test": value}
    server = {"url": "https://example.test/mcp", "headers": headers}
    if source_id == "codex":
        path = home / ".codex/config.toml"
        document = '[mcp_servers.server]\nurl = "https://example.test/mcp"\nhttp_headers = { "X-Test" = '
        document += json.dumps(value, ensure_ascii=False) + " }\n"
        field = "mcp_servers.server.http_headers.X-Test"
        expected = value.replace("{{", "{{{{").replace("}}", "}}}}")
        key = "x-test"
    elif source_id == "opencode":
        path = home / ".config/opencode/opencode.json"
        document = json.dumps({"mcp": {"server": {"type": "remote", **server}}})
        field = "mcp.server.headers.X-Test"
        expected = value.replace(template, "{{TOKEN}}")
        key = "X-Test"
    else:
        path = home / (".claude.json" if source_id == "claude_code" else ".pi/agent/mcp.json")
        if source_id == "claude_code":
            server = {"type": "http", **server}
        document = json.dumps({"mcpServers": {"server": server}})
        field = "mcpServers.server.headers.X-Test"
        expected = value.replace(template, "{{TOKEN}}")
        key = "X-Test"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(document, encoding="utf-8")
    context = ImportContext(None, home, {"OPENCODE_TEST_MANAGED_CONFIG_DIR": str(home / "managed")})
    source = next(source for source in IMPORT_SOURCES if source.source_id == source_id)

    result = source.discover(context)

    assert result.source_errors == ()
    (candidate,) = result.candidates
    assert candidate.unsupported is reason
    if reason is None:
        assert candidate.config is not None
        assert candidate.config.headers == {key: expected.strip(" \t\r\n")}
        assert httpx.Headers(candidate.config.headers)[key] == expected.strip(" \t\r\n")
    else:
        assert candidate.config is None
        assert candidate.warnings == ()
        assert candidate.unsupported_field == field
        assert candidate.unsupported_variable == ""
        assert candidate.entry is not None and candidate.entry["headers"] == headers
