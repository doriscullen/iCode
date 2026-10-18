# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Tests for importing MCP servers configured in pi 1.3.0."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import pytest

from chrys.foundation.util.env_templates import resolve_env_templates
from chrys.service.mcp.importers import (
    ImportContext,
    ImportWarning,
    ImportWarningKind,
    McpImportCandidate,
    McpImportResult,
    McpSourceError,
    SourceErrorKind,
    UnsupportedReason,
)
from chrys.service.mcp.importers.base import MAX_REQUEST_TIMEOUT_SECONDS, render_template
from chrys.service.mcp.importers.pi import PiSource, _value_parts
from chrys.service.profiles.agents.schema import MCPServerConfig


@pytest.fixture
def home(tmp_path: Path) -> Path:
    path = tmp_path / "home"
    path.mkdir()
    return path


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    path = tmp_path / "project"
    path.mkdir()
    return path


def _write(path: Path, document: Any) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(document if isinstance(document, str) else json.dumps(document), encoding="utf-8")
    return path


def _global(home: Path, servers: dict[str, Any], **top: Any) -> Path:
    return _write(home / ".pi" / "agent" / "mcp.json", {"mcpServers": servers, **top})


def _project(workspace: Path, servers: dict[str, Any], **top: Any) -> Path:
    return _write(workspace / ".pi" / "mcp.json", {"mcpServers": servers, **top})


def _context(home: Path, workspace: Path | None = None, **environ: str) -> ImportContext:
    return ImportContext(workspace_cwd=None if workspace is None else str(workspace), home=home, environ=environ)


def _discover(home: Path, workspace: Path | None = None, **environ: str) -> tuple[McpImportCandidate, ...]:
    return PiSource().discover(_context(home, workspace, **environ)).candidates


def _by_name(candidates: tuple[McpImportCandidate, ...]) -> dict[str, McpImportCandidate]:
    return {candidate.name: candidate for candidate in candidates}


def _trust(home: Path, folder: Path, decision: bool | None) -> None:
    _write(home / ".pi" / "agent" / "trust.json", {os.path.realpath(folder): decision})


def test_detect_needs_the_agent_folder_or_a_project_file(home: Path, workspace: Path) -> None:
    source = PiSource()
    assert source.detect(_context(home, workspace)) is False

    _project(workspace, {})
    assert source.detect(_context(home, workspace)) is True

    (workspace / ".pi" / "mcp.json").unlink()
    (home / ".pi" / "agent").mkdir(parents=True)
    assert source.detect(_context(home, workspace)) is True


def test_stdio_and_http_servers_are_converted(home: Path) -> None:
    _global(
        home,
        {
            "files": {
                "command": "~/bin/files",
                "args": ["--root", "~/notes", "a~b"],
                "env": {"TOKEN": "${FILES_TOKEN}", "RAW": "$HOME/x", "LITERAL": "{{x}}"},
                "description": "Reads notes",
                "timeout": 12.5,
                "exposure": "direct",
            },
            "docs": {"url": "https://docs.test/mcp", "headers": {"Authorization": "Bearer $DOCS_TOKEN"}},
        },
    )

    candidates = _by_name(_discover(home))

    assert candidates["files"].config == MCPServerConfig(
        name="files",
        transport="stdio",
        command=str(home / "bin/files"),
        args=["--root", str(home / "notes"), "a~b"],
        env={"TOKEN": "{{FILES_TOKEN}}", "RAW": "{{HOME}}/x", "LITERAL": "{{{{x}}}}"},
        description="Reads notes",
        request_timeout=13,
    )
    assert (candidates["files"].scope, candidates["files"].transport) == ("global", "stdio")
    assert candidates["files"].warnings == ()
    assert candidates["docs"].config == MCPServerConfig(
        name="docs",
        transport="http",
        url="https://docs.test/mcp",
        headers={"Authorization": "Bearer {{DOCS_TOKEN}}"},
        use_progressive_disclosure=True,
    )
    assert candidates["docs"].transport == "http"


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("${A}-$B_c.d", "a-b.d"),
        ("cost $$5 and $!", "cost $5 and !"),
        ("${A:-x} ${1A} $ ${", "${A:-x} ${1A} $ ${"),
        ("{{A}} $A}", "{{A}} a}"),
    ],
)
def test_values_resolve_in_icode_as_pi_resolves_them(
    monkeypatch: pytest.MonkeyPatch, value: str, expected: str
) -> None:
    monkeypatch.setenv("A", "a")
    monkeypatch.setenv("B_c", "b")
    parts = _value_parts(value)

    assert parts is not None
    assert resolve_env_templates(render_template(parts), location="test") == expected


@pytest.mark.parametrize("transport", ["stdio", "http"])
@pytest.mark.parametrize("value", ["${}}", "${A:-default}}", "{{${1A}}}"])
def test_imported_values_preserve_literal_braces_across_parsed_parts(
    home: Path, monkeypatch: pytest.MonkeyPatch, transport: str, value: str
) -> None:
    monkeypatch.setenv("IMPORT_TEST_TOKEN", "secret")
    original = f"$IMPORT_TEST_TOKEN {value}"
    entry = (
        {"command": "cmd", "env": {"VALUE": original}}
        if transport == "stdio"
        else {"url": "https://x.test/mcp", "headers": {"X-Value": original}}
    )
    _global(home, {"s": entry})

    (candidate,) = _discover(home)

    assert candidate.config is not None
    converted = candidate.config.env["VALUE"] if transport == "stdio" else candidate.config.headers["X-Value"]
    assert resolve_env_templates(converted, location="test") == f"secret {value}"


@pytest.mark.parametrize(
    ("entry", "reason"),
    [
        ({"command": "cmd", "env": {"TOKEN": "!op read secret"}}, UnsupportedReason.VALUE_FROM_COMMAND),
        ({"url": "https://x.test", "headers": {"A": "!cat token"}}, UnsupportedReason.VALUE_FROM_COMMAND),
        ({"command": "cmd", "env": {"A": "{${X}"}}, UnsupportedReason.TEMPLATE_NEXT_TO_BRACE),
    ],
)
def test_servers_icode_cant_run_as_written_are_listed_but_not_importable(
    home: Path, entry: dict[str, Any], reason: UnsupportedReason
) -> None:
    _global(home, {"s": entry})

    (candidate,) = _discover(home)

    assert (candidate.config, candidate.unsupported) == (None, reason)
    assert candidate.entry is not None and set(candidate.entry) <= {"command", "args", "env", "url", "headers"}


def test_settings_icode_lacks_are_reported(home: Path) -> None:
    _global(
        home,
        {
            "worker": {"command": "cmd", "cwd": "tools", "enabled": False},
            "signin": {"url": "https://x.test/mcp", "oauth": {"clientId": "id"}},
            "provider": {"url": "https://y.test/mcp", "auth": {"provider": "radius"}},
        },
    )

    candidates = _by_name(_discover(home))

    worker = candidates["worker"]
    assert worker.config is not None and worker.config.enabled is False
    assert worker.warnings == (
        ImportWarning(ImportWarningKind.CWD_IGNORED, original="tools"),
        ImportWarning(ImportWarningKind.DISABLED),
    )
    assert candidates["signin"].warnings == (ImportWarning(ImportWarningKind.OAUTH_IGNORED),)
    assert candidates["provider"].warnings == (
        ImportWarning(ImportWarningKind.AUTH_PROVIDER_IGNORED, original="radius"),
    )


@pytest.mark.parametrize(
    "entry",
    [
        {"type": "sse", "url": "https://x.test"},
        {"type": "ws", "url": "wss://x.test"},
        {"type": "http", "command": "cmd"},
        {"url": "ftp://x.test"},
        {"url": "not a url"},
        {"command": "cmd", "args": "--flag"},
        {"command": "cmd", "env": {"A": 1}},
        {"command": "cmd", "timeout": 0},
        {"command": "cmd", "timeout": 10**400},  # Infinity to pi; too long for a Python float
        {"command": "cmd", "exposure": "sometimes"},
        {"command": "cmd", "enabled": "no"},
        {"url": "https://x.test", "oauth": {"callbackPort": 0}},
        {"url": "https://x.test", "oauth": {"callbackPort": 10**400}},
        {"url": "https://x.test", "oauth": {"clientRegistration": "cimd", "clientId": "x"}},
        {"url": "http://x.test", "auth": {"provider": "radius"}},
        {"enabled": False},
        "not an object",
        # Any JSON value where pi expects a word.
        {"command": "cmd", "exposure": []},
        {"command": "cmd", "toolExposure": {"t": {}}},
        {"command": "cmd", "type": []},
        {"url": "https://x.test", "oauth": {"callbackPort": 3000, "callbackUrl": "http://localhost:4000/cb"}},
        {"url": "https://x.test", "oauth": {"clientRegistration": "cimd", "callbackUrl": "http://[::1]/callback"}},
        {"url": "https://x.test", "oauth": {"clientRegistration": "cimd", "callbackUrl": "http://localhost/cb"}},
    ],
)
def test_entries_pi_rejects_are_not_listed(home: Path, entry: object) -> None:
    _global(home, {"skipped": entry, "kept": {"command": "cmd"}})

    assert [candidate.name for candidate in _discover(home)] == ["kept"]


def test_oauth_settings_pi_accepts_are_listed(home: Path) -> None:
    _global(
        home,
        {
            # JSON has one number type: 3000.0 is the integer 3000 to pi.
            "port": {
                "url": "https://x.test",
                "oauth": {"callbackPort": 3000.0, "callbackUrl": "http://localhost:3000/cb"},
            },
            "cimd": {
                "url": "https://y.test",
                "oauth": {"clientRegistration": "cimd", "callbackUrl": "http://127.0.0.1/callback"},
            },
        },
    )

    assert [candidate.name for candidate in _discover(home)] == ["port", "cimd"]


def test_a_bad_name_or_a_namespace_clash_is_not_listed(home: Path) -> None:
    _global(home, {"has space": {"command": "a"}, "my-server": {"command": "b"}, "my_server": {"command": "c"}})

    assert [candidate.name for candidate in _discover(home)] == ["my-server"]


def test_an_entry_with_a_url_connects_over_http_whatever_its_type(home: Path) -> None:
    # pi validates by the type but connects to the url whenever there is one [code: CA/extensions/mcp/runtime.ts:98-104].
    _global(
        home,
        {
            "both": {"url": "https://x.test/mcp", "command": "cmd"},
            "typed": {"type": "stdio", "url": "https://y.test/mcp", "command": "cmd"},
            "broken": {"type": "stdio", "url": "not a url", "command": "cmd"},
        },
    )

    candidates = _by_name(_discover(home))

    assert list(candidates) == ["both", "typed"]
    for name, url in (("both", "https://x.test/mcp"), ("typed", "https://y.test/mcp")):
        config = candidates[name].config
        assert candidates[name].transport == "http"
        assert config is not None and (config.transport, config.url) == ("http", url)


@pytest.mark.parametrize(
    ("text", "kind"),
    [
        ('{"mcpServers": {},}', SourceErrorKind.INVALID_JSON),
        ('// note\n{"mcpServers": {}}', SourceErrorKind.INVALID_JSON),
        ('{"mcpServers": {"a": NaN}}', SourceErrorKind.INVALID_JSON),
        ("[]", SourceErrorKind.NOT_AN_OBJECT),
        ('{"mcpServers": []}', SourceErrorKind.INVALID_CONFIG),
    ],
)
def test_a_file_pi_skips_is_reported(home: Path, text: str, kind: SourceErrorKind) -> None:
    path = _write(home / ".pi" / "agent" / "mcp.json", text)

    assert PiSource().discover(_context(home)) == McpImportResult(source_errors=(McpSourceError(str(path), kind),))


def test_a_byte_order_mark_makes_pi_skip_the_file(home: Path) -> None:
    path = home / ".pi" / "agent" / "mcp.json"
    path.parent.mkdir(parents=True)
    path.write_bytes(b"\xef\xbb\xbf" + json.dumps({"mcpServers": {"s": {"command": "cmd"}}}).encode())

    result = PiSource().discover(_context(home))

    assert result == McpImportResult(source_errors=(McpSourceError(str(path), SourceErrorKind.INVALID_JSON),))


def test_project_servers_replace_global_ones_and_overrides_adjust_them(home: Path, workspace: Path) -> None:
    global_file = _global(
        home,
        {
            "first": {"command": "global-first", "env": {"A": "1"}},
            "second": {"command": "global-second", "exposure": "direct"},
            "third": {"command": "global-third"},
        },
    )
    project_file = _project(
        workspace,
        {
            "first": {"command": "project-first"},
            "second": {"enabled": False, "exposure": "hidden"},
            "orphan": {"enabled": False},
            "third": {"enabled": False, "args": ["--x"]},
            "fourth": {"url": "https://x.test", "auth": {"provider": "radius"}},
            "own": {"url": "https://own.test/mcp"},
        },
    )

    candidates = _discover(home, workspace)

    # A replaced server keeps the global one's place.
    assert [c.name for c in candidates] == ["first", "second", "third", "own"]
    first, second, third, own = candidates
    assert first.config is not None and (first.config.command, first.config.env) == ("project-first", {})
    assert (first.scope, first.source_path, first.scope_path) == ("project", str(project_file), str(workspace))
    assert second.config is not None and (second.config.enabled, second.config.allowed_tools) == (False, [])
    assert (second.scope, second.source_path) == ("global", str(global_file))
    assert second.warnings == (
        ImportWarning(ImportWarningKind.MERGED, original=str(project_file)),
        ImportWarning(ImportWarningKind.TOOLS_LIMITED, field="exposure: hidden"),
        ImportWarning(ImportWarningKind.DISABLED),
    )
    # An override may only set enabled, exposure and toolExposure.
    assert third.config is not None and third.config.enabled is True
    assert own.scope == "project"


def test_only_the_workspace_folder_holds_project_servers(home: Path, tmp_path: Path) -> None:
    folder = tmp_path / "project" / "sub"
    folder.mkdir(parents=True)
    _project(tmp_path / "project", {"above": {"command": "cmd"}})

    assert _discover(home, folder) == ()


@pytest.mark.parametrize(
    ("trust", "default", "trusted"),
    [
        (None, None, True),
        (True, "never", True),
        (False, "always", False),
        (None, "never", False),
        (None, "always", True),
    ],
)
def test_project_servers_follow_pis_trust_in_the_folder(
    home: Path, workspace: Path, trust: bool | None, default: str | None, trusted: bool
) -> None:
    _global(home, {"shared": {"command": "global"}})
    _project(workspace, {"shared": {"command": "project"}, "local": {"url": "https://x.test/mcp"}})
    # Trust is looked up from the workspace upwards; a decision on a parent folder counts.
    _trust(home, workspace.parent, trust)
    if default is not None:
        _write(home / ".pi" / "agent" / "settings.json", {"defaultProjectTrust": default})

    candidates = _discover(home, workspace)

    if trusted:
        assert [(c.name, c.scope, c.unsupported) for c in candidates] == [
            ("shared", "project", None),
            ("local", "project", None),
        ]
    else:
        assert [(c.name, c.scope, c.unsupported) for c in candidates] == [
            ("shared", "global", None),
            ("shared", "project", UnsupportedReason.UNTRUSTED_PROJECT),
            ("local", "project", UnsupportedReason.UNTRUSTED_PROJECT),
        ]
        assert candidates[2].transport == "http"


def test_the_agent_dir_variable_is_followed(home: Path, tmp_path: Path, workspace: Path) -> None:
    _global(home, {"default": {"command": "cmd"}})
    _write(tmp_path / "agent" / "mcp.json", {"mcpServers": {"moved": {"command": "cmd"}}})
    _write(home / "tilde" / "mcp.json", {"mcpServers": {"tilde": {"command": "cmd"}}})
    _write(workspace / "relative" / "mcp.json", {"mcpServers": {"relative": {"command": "cmd"}}})

    assert [c.name for c in _discover(home, PI_CODING_AGENT_DIR=str(tmp_path / "agent"))] == ["moved"]
    assert [c.name for c in _discover(home, PI_CODING_AGENT_DIR="~/tilde")] == ["tilde"]
    assert [c.name for c in _discover(home, workspace, PI_CODING_AGENT_DIR="relative")] == ["relative"]
    # Without a workspace, the folder a relative path names is unknown.
    assert _discover(home, PI_CODING_AGENT_DIR="relative") == ()


@pytest.mark.parametrize(
    ("entry", "on_demand", "allowed", "always", "ignored"),
    [
        ({}, True, None, [], ""),
        ({"exposure": "codemode-deferred"}, True, None, [], ""),
        ({"exposure": "deferred", "toolExposure": {"search": "direct", "x*": "direct"}}, True, None, ["search"], "x*"),
        (
            {"exposure": "direct", "toolExposure": {"delete": "hidden", "slow": "deferred"}},
            False,
            None,
            [],
            "delete, slow",
        ),
        ({"exposure": "hidden", "toolExposure": {"read": "direct", "list": "direct"}}, False, ["read", "list"], [], ""),
        (
            {"exposure": "hidden", "toolExposure": {"read": "direct", "find": "codemode", "w*": "direct"}},
            True,
            ["read", "find"],
            ["read"],
            "w*",
        ),
    ],
)
def test_exposure_becomes_icodes_tool_settings(
    home: Path,
    entry: dict[str, Any],
    on_demand: bool,
    allowed: list[str] | None,
    always: list[str],
    ignored: str,
) -> None:
    _global(home, {"s": {"command": "cmd", **entry}})

    (candidate,) = _discover(home)

    config = candidate.config
    assert config is not None
    assert (config.use_progressive_disclosure, config.allowed_tools, config.always_load) == (on_demand, allowed, always)
    ignored_warnings = [w for w in candidate.warnings if w.kind is ImportWarningKind.TOOL_RULES_IGNORED]
    assert ignored_warnings == (
        [ImportWarning(ImportWarningKind.TOOL_RULES_IGNORED, original=ignored)] if ignored else []
    )


def test_codemode_off_is_reported_when_nothing_reaches_the_tools(home: Path, workspace: Path) -> None:
    _global(home, {"a": {"command": "cmd"}, "b": {"command": "cmd", "exposure": "direct"}}, autoEnableCodemode=True)
    # The project value wins.
    _project(workspace, {}, autoEnableCodemode=False)

    candidates = _by_name(_discover(home, workspace))
    assert ImportWarning(ImportWarningKind.CODEMODE_OFF) in candidates["a"].warnings
    assert candidates["b"].warnings == ()

    # A deferred server makes pi turn tool search on, which reaches codemode tools too.
    _global(home, {"a": {"command": "cmd"}, "c": {"command": "cmd", "exposure": "deferred"}})
    candidates = _by_name(_discover(home, workspace))
    assert candidates["a"].warnings == ()


def test_a_timeout_longer_than_icode_can_wait_is_capped(home: Path) -> None:
    _global(home, {"slow": {"command": "cmd", "timeout": 1e300}})

    (candidate,) = _discover(home)

    assert candidate.config is not None
    assert candidate.config.request_timeout == MAX_REQUEST_TIMEOUT_SECONDS
