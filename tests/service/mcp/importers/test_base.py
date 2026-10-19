# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Tests for the shared MCP import helpers and the source registry."""

from __future__ import annotations

import hashlib
import itertools
import json
import logging
import math
from dataclasses import dataclass, replace
from datetime import timedelta
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from chrys.foundation.util.env_templates import EnvVarResolutionError, resolve_env_templates
from chrys.service.mcp.adapter import MCPAdapter
from chrys.service.mcp.errors import MCPToolNameCollisionError
from chrys.service.mcp.importers import (
    ImportContext,
    ImportWarning,
    ImportWarningKind,
    McpImportResult,
    SourceErrorKind,
    detected_sources,
)
from chrys.service.mcp.importers.base import (
    MAX_REQUEST_TIMEOUT_SECONDS,
    SourceReadError,
    convert_dollar_templates,
    dollar_template_variables,
    dollar_templates_convertible,
    has_dollar_template,
    json_number,
    read_json_object,
    request_timeout_seconds,
    with_tool_name_prefix,
)
from chrys.service.mcp.owned import MCPTool
from chrys.service.mcp.validation import (
    MAX_MCP_EXPOSED_TOOL_NAME_LENGTH,
    MCP_PROGRESSIVE_CONTROL_TOOL_NAMES,
    validate_mcp_tool_name_prefix,
)
from chrys.service.profiles.agents.schema import MCPServerConfig
from tests.service.mcp._helpers import _FakeConnectionTool, _load_fake_remote_tools, _mcp_remote_tool
from tests.support.symlinks import symlink_or_skip


def test_dollar_placeholder_becomes_icode_placeholder() -> None:
    assert convert_dollar_templates("Bearer ${TOKEN}") == ("Bearer {{TOKEN}}", ())


def test_dollar_placeholder_default_is_dropped_with_a_warning() -> None:
    text, warnings = convert_dollar_templates("${HOST:-localhost}:${PORT}")

    assert text == "{{HOST}}:{{PORT}}"
    assert warnings == (ImportWarning(ImportWarningKind.DEFAULT_DROPPED, "HOST"),)


@pytest.mark.parametrize(
    "literal",
    ["{{not_a_var}}", "a{{b", "c}}d", "{{{x}}}", '{{"kind":"json"}}', "{{{{"],
)
def test_literal_braces_are_escaped_so_icode_reads_them_back_unchanged(literal: str) -> None:
    text, warnings = convert_dollar_templates(literal)

    assert warnings == ()
    assert resolve_env_templates(text, location="test") == literal


def test_converted_placeholder_and_escaped_literal_resolve_together(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("IMPORT_TEST_TOKEN", "secret")

    text, _warnings = convert_dollar_templates("{{literal}} ${IMPORT_TEST_TOKEN}")

    assert resolve_env_templates(text, location="test") == "{{literal}} secret"


@pytest.mark.parametrize(
    ("value", "convertible"),
    [
        ("{${A}", False),
        ("{{{${A}", False),
        ("${A}}}", False),
        ("${A:-x}}}", False),
        ("x{${A}${B}", False),
        ("${A}{${B}", False),
        ("{{${A}", True),
        ("${A}}", True),
        ("${A}}${B}", True),
        ('{"token":"${A}"}', True),
        ("{ ${A} }", True),
        ("{{ plain }}", True),
    ],
)
def test_dollar_templates_convertible_flags_a_single_brace_beside_a_placeholder(value: str, convertible: bool) -> None:
    assert dollar_templates_convertible(value) is convertible


_PLACEHOLDER = "${IMPORT_TEST_VAR}"


def test_dollar_templates_convertible_agrees_with_what_icode_resolves(monkeypatch: pytest.MonkeyPatch) -> None:
    # Every short mix of braces, text and a placeholder: the shape rule must say exactly
    # when iCode's resolver reads the converted value back as Claude Code expands the original.
    monkeypatch.setenv("IMPORT_TEST_VAR", "v")
    tokens = ("{", "}", "x", _PLACEHOLDER)
    for length in range(1, 6):
        for parts in itertools.product(tokens, repeat=length):
            value = "".join(parts)
            converted, _warnings = convert_dollar_templates(value)
            try:
                resolved: str | None = resolve_env_templates(converted, location="test")
            except EnvVarResolutionError:
                resolved = None
            faithful = resolved == value.replace(_PLACEHOLDER, "v")
            assert dollar_templates_convertible(value) is faithful, (value, converted, resolved)


def test_text_without_placeholders_is_unchanged() -> None:
    assert convert_dollar_templates("$HOME and ${ not-a-name}") == ("$HOME and ${ not-a-name}", ())


@pytest.mark.parametrize(
    ("value", "expected"),
    [("${A}", True), ("x${A:-b}y", True), ("$A", False), ("${1A}", False), ("plain", False)],
)
def test_has_dollar_template(value: str, expected: bool) -> None:
    assert has_dollar_template(value) is expected


def test_dollar_template_variables_lists_each_name_once_in_order() -> None:
    assert dollar_template_variables("${B} ${A:-x} $C {{D}} ${B}") == ["B", "A"]
    assert dollar_template_variables("plain") == []


def test_read_json_object_returns_none_for_a_missing_file(tmp_path: Path) -> None:
    assert read_json_object(tmp_path / "missing.json") is None


def test_read_json_object_reads_an_object(tmp_path: Path) -> None:
    path = tmp_path / "config.json"
    path.write_text(json.dumps({"mcpServers": {}}), encoding="utf-8")

    assert read_json_object(path) == {"mcpServers": {}}


@pytest.mark.parametrize(
    ("payload", "kind"),
    [
        (b"{not json", SourceErrorKind.INVALID_JSON),
        (b"\xff\xfe\x00{", SourceErrorKind.INVALID_JSON),
        (b"[1, 2]", SourceErrorKind.NOT_AN_OBJECT),
    ],
)
def test_read_json_object_reports_unusable_content(tmp_path: Path, payload: bytes, kind: SourceErrorKind) -> None:
    path = tmp_path / "config.json"
    path.write_bytes(payload)

    with pytest.raises(SourceReadError) as caught:
        read_json_object(path)

    assert caught.value.kind is kind


def test_read_json_object_refuses_a_file_over_the_cap(tmp_path: Path) -> None:
    path = tmp_path / "config.json"
    path.write_text(json.dumps({"key": "x" * 100}), encoding="utf-8")

    with pytest.raises(SourceReadError) as caught:
        read_json_object(path, max_bytes=50)

    assert caught.value.kind is SourceErrorKind.TOO_LARGE


def test_read_json_object_follows_a_link_to_the_file(tmp_path: Path) -> None:
    target = tmp_path / "dotfiles" / "config.json"
    target.parent.mkdir()
    target.write_text(json.dumps({"mcpServers": {}}), encoding="utf-8")
    link = tmp_path / "config.json"
    symlink_or_skip(link, target)

    assert read_json_object(link) == {"mcpServers": {}}


def test_read_json_object_treats_a_dangling_link_as_no_file(tmp_path: Path) -> None:
    link = tmp_path / "config.json"
    symlink_or_skip(link, tmp_path / "gone.json")

    assert read_json_object(link) is None


def test_read_json_object_reports_a_link_to_a_directory_as_unreadable(tmp_path: Path) -> None:
    target = tmp_path / "folder"
    target.mkdir()
    link = tmp_path / "config.json"
    symlink_or_skip(link, target, target_is_directory=True)

    with pytest.raises(SourceReadError) as caught:
        read_json_object(link)

    assert caught.value.kind is SourceErrorKind.UNREADABLE


def test_read_json_object_reports_a_directory_as_unreadable(tmp_path: Path) -> None:
    path = tmp_path / "config.json"
    path.mkdir()

    with pytest.raises(SourceReadError) as caught:
        read_json_object(path)

    assert caught.value.kind is SourceErrorKind.UNREADABLE


@dataclass
class _FakeSource:
    source_id: str
    display_name: str
    present: bool

    def detect(self, ctx: ImportContext) -> bool:
        return self.present

    def discover(self, ctx: ImportContext) -> McpImportResult:
        return McpImportResult()


def test_detected_sources_keeps_only_present_sources_in_order(tmp_path: Path) -> None:
    first = _FakeSource("first", "First", present=True)
    absent = _FakeSource("absent", "Absent", present=False)
    last = _FakeSource("last", "Last", present=True)
    ctx = ImportContext(workspace_cwd=None, home=tmp_path)

    assert detected_sources(ctx, [first, absent, last]) == [first, last]


@pytest.mark.parametrize(
    ("value", "number"),
    [
        (3000, 3000.0),
        (12.5, 12.5),
        (10**400, float("inf")),  # JavaScript reads it as Infinity; float() would raise
        (-(10**400), float("-inf")),
        (True, None),
        ("3000", None),
        (None, None),
    ],
)
def test_json_number_reads_a_number_as_javascript_holds_it(value: object, number: float | None) -> None:
    assert json_number(value) == number


def test_the_longest_request_timeout_fits_the_mcp_clients_timedelta() -> None:
    # The client builds timedelta(seconds=request_timeout) for every connection.
    assert timedelta(seconds=request_timeout_seconds(1e300)) == timedelta(seconds=MAX_REQUEST_TIMEOUT_SECONDS)
    assert request_timeout_seconds(12.1) == 13
    # An agent's "no time limit".
    assert request_timeout_seconds(math.inf) == MAX_REQUEST_TIMEOUT_SECONDS
    assert request_timeout_seconds(10**400) == MAX_REQUEST_TIMEOUT_SECONDS


@dataclass(frozen=True)
class _BrokenSource:
    source_id: str = "broken"
    display_name: str = "Broken"

    def detect(self, ctx: ImportContext) -> bool:
        raise PermissionError("config folder")

    def discover(self, ctx: ImportContext) -> McpImportResult:
        return McpImportResult()


def test_a_source_whose_check_fails_leaves_the_others_offered(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    first = _FakeSource("first", "First", present=True)
    last = _FakeSource("last", "Last", present=True)
    ctx = ImportContext(workspace_cwd=None, home=tmp_path)

    with caplog.at_level(logging.ERROR, logger="chrys.service.mcp.importers.registry"):
        assert detected_sources(ctx, [first, _BrokenSource(), last]) == [first, last]
    assert [record.getMessage() for record in caplog.records] == ["Looking for Broken's MCP configuration failed"]


def _server(name: str, **fields: Any) -> MCPServerConfig:
    return MCPServerConfig(name=name, transport="stdio", command="server", **fields)


@pytest.mark.parametrize(
    ("name", "prefix"),
    [
        ("github", "github"),
        ("lab-cc-helper", "lab-cc-helper"),
        ("Docs_Search", "Docs_Search"),
        # As opencode writes the name into its tool names: other characters become ``_``.
        ("my server.v2", "my_server_v2"),
        ("Ünïcode", "n_code"),
        # Two for a character beyond U+FFFF, as JavaScript counts it.
        ("a\U0001f600b", "a__b"),
        # Without the ``_`` and ``-`` at either end, which a prefix can't have.
        ("_private-", "private"),
        ("-a--b_", "a--b"),
    ],
)
def test_an_imported_server_is_given_its_name_as_tool_name_prefix(name: str, prefix: str) -> None:
    config = _server(name, allowed_tools=["search"], always_load=[], description="own")

    prefixed = with_tool_name_prefix(config)

    assert prefixed == replace(config, tool_name_prefix=prefix)
    assert config.tool_name_prefix == ""
    assert validate_mcp_tool_name_prefix(prefixed.tool_name_prefix) is None


@pytest.mark.parametrize(
    ("name", "prefix"),
    [("文件系统", "mcp_47b61747"), ("!!!", "mcp_e84c538e"), ("\ud800", "mcp_91a681b9"), ("", "mcp_e3b0c442")],
)
def test_a_name_with_nothing_a_prefix_can_keep_gets_a_prefix_from_its_hash(name: str, prefix: str) -> None:
    prefixed = with_tool_name_prefix(_server(name))

    assert prefixed.tool_name_prefix == prefix
    assert validate_mcp_tool_name_prefix(prefix, generated_suffixes=MCP_PROGRESSIVE_CONTROL_TOOL_NAMES) is None


def test_a_server_that_has_a_prefix_keeps_it() -> None:
    config = _server("github", tool_name_prefix="gh")

    assert with_tool_name_prefix(config) is config


def _name_hash(name: str, digits: int) -> str:
    return hashlib.sha256(name.encode("utf-8", "surrogatepass")).hexdigest()[:digits]


@pytest.mark.parametrize(
    ("name", "prefix"),
    [
        # Up to 24 characters, the prefix is the name.
        ("a" * 24, "a" * 24),
        ("a" * 25, "aaaaaaaaaaaaaaaaa-2f521e"),
        ("a" * 80, "aaaaaaaaaaaaaaaaa-0f45e8"),
        # Measured after the name is written as a prefix: 24 once its ``_`` at either end are left out.
        ("_" + "a" * 24 + "_", "a" * 24),
        ("NODE_REPL_INSTRUCTIONS_USE_CASE_COMPUTER_USE", "NODE_REPL_INSTRUC-ad8485"),
        # The first 17 characters end with ``-`` or ``_``, which a prefix's part before ``-`` drops.
        ("abcdefghijklmnop-qrstuvwxyz0123", "abcdefghijklmnop-6376f8"),
        ("abcdefghijklmnop_-qrstuvwxyz0123", "abcdefghijklmnop-5edbcf"),
        ("abcdefghijklmno_-xyz0123456789", "abcdefghijklmno-84833f"),
        # Two ``_`` for a character beyond U+FFFF count as two.
        ("abcdefghijklmno\U0001f600zzzzzzzzzz", "abcdefghijklmno-65e6f8"),
    ],
)
def test_a_long_name_gives_a_short_prefix_ending_in_a_hash_of_the_name(name: str, prefix: str) -> None:
    result = with_tool_name_prefix(_server(name, use_progressive_disclosure=True, allowed_tools=["search"]))

    assert result.tool_name_prefix == prefix
    assert len(prefix) <= 24
    if prefix != name.strip("_"):
        assert prefix.endswith("-" + _name_hash(name, 6))
    # The same name gives the same prefix each time.
    assert with_tool_name_prefix(_server(name)).tool_name_prefix == prefix


def test_long_names_that_start_alike_get_different_prefixes() -> None:
    names = [
        "my-very-long-server-name-for-github",
        "my-very-long-server-name-for-gitlab",
        "my very long server name, v2",
    ]

    prefixes = [with_tool_name_prefix(_server(name)).tool_name_prefix for name in names]

    assert prefixes == ["my-very-long-serv-d2c488", "my-very-long-serv-3ca110", "my_very_long_serv-f42d31"]


@pytest.mark.parametrize("on_demand", [False, True])
def test_every_imported_prefix_is_short_and_one_icode_accepts(on_demand: bool) -> None:
    names = [
        *("a" * length for length in range(1, 90)),
        *("a-" * length for length in range(1, 45)),
        *("ab_" * length for length in range(1, 30)),
        *("x" * length + "\U0001f600" * 30 for length in range(1, 30)),
        *("文件" * length for length in range(1, 40)),
    ]
    generated = MCP_PROGRESSIVE_CONTROL_TOOL_NAMES if on_demand else ()

    for name in names:
        prefix = with_tool_name_prefix(_server(name, use_progressive_disclosure=on_demand)).tool_name_prefix
        assert 0 < len(prefix) <= 24, name
        assert validate_mcp_tool_name_prefix(prefix, generated_suffixes=generated) is None, name


async def _connect(adapter: MCPAdapter, config: MCPServerConfig, *tool_names: str) -> list[str]:
    """Connect *config* to a server listing *tool_names*, as the agent's start does; the names its tools get."""
    owned = MCPTool(name=config.name, tool_name_prefix=config.tool_name_prefix, allowed_tools=config.allowed_tools)
    await _load_fake_remote_tools(owned, *(_mcp_remote_tool(name) for name in tool_names))
    fake = _FakeConnectionTool(functions=owned.functions)
    with patch("chrys.service.mcp._connection._create_mcp_tool", return_value=fake):
        return sorted(tool.name for tool in await adapter.connect(config))


async def test_prefixed_imports_with_the_same_tool_names_connect_side_by_side() -> None:
    # Two servers the source ran together, whose tools it named after them; one limited to some tools.
    files = _server("lab-cc-helper")
    pi = _server("lab-pi-http", allowed_tools=["read_file", "search"])

    unprefixed = MCPAdapter()
    assert await _connect(unprefixed, files, "read_file", "search") == ["read_file", "search"]
    with pytest.raises(MCPToolNameCollisionError, match="collision with MCP server 'lab-cc-helper'"):
        await _connect(unprefixed, pi, "read_file", "search", "write_file")
    await unprefixed.disconnect_all()

    adapter = MCPAdapter()
    assert await _connect(adapter, with_tool_name_prefix(files), "read_file", "search") == [
        "lab-cc-helper_read_file",
        "lab-cc-helper_search",
    ]
    # The tools the source let it use are still picked by their own names.
    assert await _connect(adapter, with_tool_name_prefix(pi), "read_file", "search", "write_file") == [
        "lab-pi-http_read_file",
        "lab-pi-http_search",
    ]
    await adapter.disconnect_all()


@pytest.mark.parametrize("on_demand", [False, True])
async def test_a_long_named_server_with_long_tool_names_still_connects(on_demand: bool) -> None:
    tool = "create_or_update_repository_webhook"
    assert len(tool) == 35
    config = with_tool_name_prefix(
        _server("enterprise-github-server-for-my-team", use_progressive_disclosure=on_demand, allowed_tools=[tool])
    )

    adapter = MCPAdapter()
    names = await _connect(adapter, config, tool)
    await adapter.disconnect_all()

    assert config.tool_name_prefix == "enterprise-github-a16457"
    if not on_demand:
        assert names == ["enterprise-github-a16457_create_or_update_repository_webhook"]
    # On demand, the agent starts with the loading tools, which find the tool by that name.
    assert names
    assert all(name.startswith("enterprise-github-a16457_") for name in names)
    assert all(len(name) <= MAX_MCP_EXPOSED_TOOL_NAME_LENGTH for name in names)
