# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Tests for importing MCP servers configured in pi 1.4.0."""

from __future__ import annotations

import json
import math
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
from chrys.service.profiles.agents.loader import load_profile_from_yaml
from chrys.service.profiles.agents.schema import AgentProfile, MCPServerConfig, ToolsConfig
from chrys.service.profiles.agents.serializer import save_profile


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
    # pi creates its agent folder on start; an empty one holds no servers to import.
    (home / ".pi" / "agent").mkdir(parents=True)
    assert source.detect(_context(home, workspace)) is False

    _write(home / ".pi" / "agent" / "mcp.json", {"mcpServers": {}})
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
    assert candidates["files"].warnings == (ImportWarning(ImportWarningKind.LITERAL_BRACES_ESCAPED),)
    assert candidates["files"].timeout_source_seconds == 12.5
    assert candidates["docs"].config == MCPServerConfig(
        name="docs",
        transport="http",
        url="https://docs.test/mcp",
        headers={"Authorization": "Bearer {{DOCS_TOKEN}}"},
        request_timeout=60,
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


def test_header_values_are_imported_trimmed_as_pi_sends_them(home: Path) -> None:
    # pi's fetch strips spaces, tabs and line breaks from both ends of a header value (observed with pi 1.3.0);
    # iCode's HTTP client refuses to send them.
    _global(
        home,
        {
            "docs": {
                "url": "https://docs.test/mcp",
                "headers": {"Authorization": "Bearer ", "X-Pad": " \tpadded $T\r\n"},
            },
            "files": {"command": "cmd", "env": {"PAD": " kept "}},
        },
    )

    candidates = _by_name(_discover(home))

    docs, files = candidates["docs"].config, candidates["files"].config
    assert docs is not None and files is not None
    assert docs.headers == {"Authorization": "Bearer", "X-Pad": "padded {{T}}"}
    # Environment values are passed to the process as written.
    assert files.env == {"PAD": " kept "}


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
    assert worker.warnings == (ImportWarning(ImportWarningKind.CWD_IGNORED, original="tools"),)
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
        {"command": "cmd", "timeout": -(10**400)},  # -Infinity to pi; too long for a Python float
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


@pytest.mark.parametrize(
    "settings, warned",
    [
        ({}, False),
        ({"headers": {"authorization": "Bearer example"}}, False),
        ({"auth": {"provider": "example"}}, False),
        ({"oauth": {"clientId": "id"}}, True),
    ],
)
def test_only_explicit_oauth_settings_have_a_migration_warning(home: Path, settings: dict, warned: bool) -> None:
    _global(home, {"server": {"url": "https://example.test/mcp", **settings}})
    (candidate,) = _discover(home)
    assert candidate.config is not None
    assert any(w.kind is ImportWarningKind.OAUTH_IGNORED for w in candidate.warnings) is warned


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
    ("text", "kind", "field", "line", "column"),
    [
        ('{"mcpServers": {},}', SourceErrorKind.INVALID_JSON, "", 1, 18),
        ('// note\n{"mcpServers": {}}', SourceErrorKind.INVALID_JSON, "", 1, 1),
        ('{"mcpServers": {"a": NaN}}', SourceErrorKind.INVALID_JSON, "", None, None),
        ("[]", SourceErrorKind.NOT_AN_OBJECT, "", None, None),
        ('{"mcpServers": []}', SourceErrorKind.INVALID_CONFIG, "mcpServers", None, None),
    ],
)
def test_a_file_pi_skips_is_reported(
    home: Path, text: str, kind: SourceErrorKind, field: str, line: int | None, column: int | None
) -> None:
    path = _write(home / ".pi" / "agent" / "mcp.json", text)

    assert PiSource().discover(_context(home)) == McpImportResult(
        source_errors=(McpSourceError(str(path), kind, field, line, column),)
    )


def test_pi_drops_one_leading_byte_order_mark(home: Path) -> None:
    path = home / ".pi" / "agent" / "mcp.json"
    path.parent.mkdir(parents=True)
    path.write_bytes(b"\xef\xbb\xbf" + json.dumps({"mcpServers": {"s": {"command": "cmd"}}}).encode())

    assert [candidate.name for candidate in _discover(home)] == ["s"]


def test_a_second_byte_order_mark_makes_pi_skip_the_file(home: Path) -> None:
    path = home / ".pi" / "agent" / "mcp.json"
    path.parent.mkdir(parents=True)
    path.write_bytes(b"\xef\xbb\xbf" * 2 + json.dumps({"mcpServers": {"s": {"command": "cmd"}}}).encode())

    result = PiSource().discover(_context(home))

    assert result == McpImportResult(
        source_errors=(McpSourceError(str(path), SourceErrorKind.INVALID_JSON, line=1, column=1),)
    )


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
    ("trust", "default"),
    [
        (None, None),
        (True, "never"),
        (False, "always"),
        (None, "never"),
        (None, "always"),
    ],
)
def test_project_servers_can_be_imported_regardless_of_pis_trust(
    home: Path, workspace: Path, trust: bool | None, default: str | None
) -> None:
    _global(home, {"shared": {"command": "global"}})
    _project(workspace, {"shared": {"command": "project"}, "local": {"url": "https://x.test/mcp"}})
    # Trust is looked up from the workspace upwards; a decision on a parent folder counts.
    _trust(home, workspace.parent, trust)
    if default is not None:
        _write(home / ".pi" / "agent" / "settings.json", {"defaultProjectTrust": default})

    candidates = _discover(home, workspace)

    assert [(c.name, c.scope, c.unsupported) for c in candidates] == [
        ("shared", "project", None),
        ("local", "project", None),
    ]
    assert all(candidate.config is not None for candidate in candidates)


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


def test_codemode_runtime_settings_do_not_change_imported_servers(home: Path, workspace: Path) -> None:
    _global(home, {"a": {"command": "cmd"}, "b": {"command": "cmd", "exposure": "direct"}}, autoEnableCodemode=True)
    baseline = _discover(home, workspace)
    _project(workspace, {}, autoEnableCodemode=False)

    assert _discover(home, workspace) == baseline
    candidates = _by_name(baseline)
    assert candidates["a"].config is not None and candidates["a"].config.use_progressive_disclosure is True
    assert candidates["b"].config is not None and candidates["b"].config.use_progressive_disclosure is False
    assert all(candidate.warnings == () for candidate in baseline)

    # A deferred server makes pi turn tool search on, which reaches codemode tools too.
    _global(home, {"a": {"command": "cmd"}, "c": {"command": "cmd", "exposure": "deferred"}})
    candidates = _by_name(_discover(home, workspace))
    assert candidates["a"].warnings == ()


def test_a_timeout_longer_than_icode_can_wait_is_capped(home: Path) -> None:
    _global(home, {"slow": {"command": "cmd", "timeout": 1e300}})

    (candidate,) = _discover(home)

    assert candidate.config is not None
    assert candidate.config.request_timeout == MAX_REQUEST_TIMEOUT_SECONDS


@pytest.mark.parametrize("timeout", ["1" + "0" * 400, "1e400"], ids=["overlong-integer", "overlong-exponent"])
def test_a_timeout_pi_reads_as_infinity_is_listed_without_a_limit(home: Path, timeout: str) -> None:
    # pi reads both as Infinity, accepts it as positive and sets no time limit
    # [code: CA/core/mcp-servers.ts:267; packages/mcp/src/client.ts:553-559]; observed with pi 1.3.0.
    _write(
        home / ".pi" / "agent" / "mcp.json",
        '{"mcpServers": {"endless": {"command": "cmd", "timeout": ' + timeout + '}, "kept": {"command": "cmd"}}}',
    )

    candidates = _by_name(_discover(home))

    assert list(candidates) == ["endless", "kept"]
    endless = candidates["endless"].config
    assert endless is not None
    assert endless.request_timeout == MAX_REQUEST_TIMEOUT_SECONDS
    assert candidates["endless"].timeout_source_seconds == math.inf


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("  https://example.test/mcp  ", "https://example.test/mcp"),
        ("\x00\t\nhttps://example.test/mcp\r\n", "https://example.test/mcp"),
        ("https://example.test/m\tc\np?label=a b", "https://example.test/mcp?label=a b"),
        ("https://example.test/a b", "https://example.test/a b"),
    ],
)
def test_urls_keep_valid_path_spaces_and_normalize_fetch_whitespace(home: Path, url: str, expected: str) -> None:
    _global(home, {"server": {"url": url, "headers": {"Authorization": "Bearer fixture"}}})

    (candidate,) = _discover(home)

    assert candidate.config is not None
    assert candidate.config.url == expected


@pytest.mark.parametrize("url", ["https://bad host.test/mcp", "https://example.test:bad/mcp"])
def test_invalid_url_authorities_are_not_listed(home: Path, url: str) -> None:
    _global(home, {"server": {"url": url}})

    assert _discover(home) == ()


def test_case_aliased_headers_combine_in_source_order_and_keep_references(home: Path) -> None:
    _global(
        home,
        {
            "server": {
                "url": "https://example.test/mcp",
                "headers": {"X-Test": " first ", "x-test": "$DYNAMIC", "X-TEST": "", "Authorization": "fixture"},
            }
        },
    )

    (candidate,) = _discover(home)

    assert candidate.config is not None
    assert candidate.config.headers == {"X-Test": "first, {{DYNAMIC}},", "Authorization": "fixture"}


def test_empty_header_alias_keeps_internal_separator_but_not_trailing_wire_whitespace(home: Path) -> None:
    _global(
        home,
        {
            "middle": {"url": "https://example.test", "headers": {"X-Test": "first", "x-test": "", "X-TEST": "last"}},
            "end": {"url": "https://example.test", "headers": {"X-Test": "first", "x-test": ""}},
        },
    )

    candidates = _by_name(_discover(home))

    middle, end = candidates["middle"].config, candidates["end"].config
    assert middle is not None and middle.headers == {"X-Test": "first, , last"}
    assert end is not None and end.headers == {"X-Test": "first,"}


@pytest.mark.parametrize("value", ["café", "prefix-你好", "${TOKEN} café", "\udcff"])
def test_non_ascii_header_literals_refuse_import_without_copying_values(home: Path, value: str) -> None:
    _global(home, {"server": {"url": "https://example.test/mcp", "headers": {"X-Test": value}}})

    (candidate,) = _discover(home)

    assert candidate.config is None
    assert candidate.unsupported is UnsupportedReason.HEADER_VALUE_ENCODING
    assert candidate.unsupported_field == "mcpServers.server.headers.X-Test"
    assert candidate.unsupported_variable == ""


def test_dynamic_header_values_are_not_read_at_import(home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TOKEN", "café")
    _global(home, {"server": {"url": "https://example.test/mcp", "headers": {"X-Test": "$TOKEN"}}})

    (candidate,) = _discover(home)

    assert candidate.config is not None
    assert candidate.config.headers == {"X-Test": "{{TOKEN}}"}
    assert candidate.warnings == ()


@pytest.mark.parametrize("value", ["{{literal}}", "${BAD-NAME}}", "{{!never-run}}"])
def test_escaped_literal_braces_are_explained_even_across_parser_parts(home: Path, value: str) -> None:
    _global(home, {"server": {"command": "cmd", "env": {"VALUE": value}}})

    (candidate,) = _discover(home)

    assert candidate.warnings == (ImportWarning(ImportWarningKind.LITERAL_BRACES_ESCAPED),)


@pytest.mark.parametrize("value", ["literal", "$TOKEN", "$!literal", "${TOKEN:-fallback}"])
def test_values_without_brace_escaping_have_no_escape_notice(home: Path, value: str) -> None:
    _global(home, {"server": {"command": "cmd", "env": {"VALUE": value}}})

    (candidate,) = _discover(home)

    assert candidate.warnings == ()


@pytest.mark.parametrize(
    ("entry", "field"),
    [
        ({"command": "cmd", "env": {"TOKEN": "!do-not-run"}}, "env.TOKEN"),
        ({"url": "https://example.test", "headers": {"X-Test": "!do-not-run"}}, "headers.X-Test"),
        ({"command": "cmd", "env": {"TOKEN": "{${TOKEN}"}}, "env.TOKEN"),
    ],
)
def test_unsupported_fields_are_named_without_values(home: Path, entry: dict[str, Any], field: str) -> None:
    _global(home, {"server": entry})

    (candidate,) = _discover(home)

    assert candidate.config is None
    assert candidate.unsupported_field == "mcpServers.server." + field


def test_relative_and_home_cwd_notices_name_the_source_folder(home: Path, workspace: Path) -> None:
    _global(home, {"relative": {"command": "cmd", "cwd": "tools"}, "home": {"command": "cmd", "cwd": "~/tools"}})

    candidates = _by_name(_discover(home, workspace))

    assert candidates["relative"].warnings == (
        ImportWarning(ImportWarningKind.CWD_IGNORED, field=str(workspace), original="tools"),
    )
    assert candidates["home"].warnings == (ImportWarning(ImportWarningKind.CWD_IGNORED, original=str(home / "tools")),)


@pytest.mark.parametrize("timeout", [None, 1, 1.501, 10**30])
def test_timeout_preview_metadata_only_records_changed_source_seconds(home: Path, timeout: int | float | None) -> None:
    _global(home, {"server": {"command": "cmd", **({"timeout": timeout} if timeout is not None else {})}})

    (candidate,) = _discover(home)

    expected = timeout if timeout in (1.501, 10**30) else None
    assert candidate.timeout_source_seconds == expected


def test_invalid_utf8_has_an_encoding_diagnostic(home: Path) -> None:
    path = home / ".pi" / "agent" / "mcp.json"
    path.parent.mkdir(parents=True)
    path.write_bytes(b'{"mcpServers":{"secret-value":"\xff"}}')

    assert PiSource().discover(_context(home)) == McpImportResult(
        source_errors=(McpSourceError(str(path), SourceErrorKind.INVALID_ENCODING),)
    )


def test_pi_default_timeout_and_explicit_timeout_survive_profile_roundtrip(home: Path, tmp_path: Path) -> None:
    _global(home, {"default": {"command": "cmd"}, "explicit": {"command": "cmd", "timeout": 1}})

    candidates = _by_name(_discover(home))
    default, explicit = candidates["default"].config, candidates["explicit"].config
    assert default is not None and default.request_timeout == 60
    assert explicit is not None and explicit.request_timeout == 1
    path = save_profile(
        AgentProfile(name="Pi Import", tools=ToolsConfig(mcp=[default, explicit])), target_dir=tmp_path / "profiles"
    )
    loaded = {server.name: server for server in load_profile_from_yaml(path).tools.mcp}

    assert loaded["default"].request_timeout == 60
    assert loaded["explicit"].request_timeout == 1
