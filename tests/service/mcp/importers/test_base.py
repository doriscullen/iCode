# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Tests for the shared MCP import helpers and the source registry."""

from __future__ import annotations

import itertools
import json
from dataclasses import dataclass
from pathlib import Path

import pytest

from chrys.foundation.util.env_templates import EnvVarResolutionError, resolve_env_templates
from chrys.service.mcp.importers import (
    ImportContext,
    ImportWarning,
    ImportWarningKind,
    McpImportResult,
    SourceErrorKind,
    detected_sources,
)
from chrys.service.mcp.importers.base import (
    SourceReadError,
    convert_dollar_templates,
    dollar_template_variables,
    dollar_templates_convertible,
    has_dollar_template,
    json_number,
    read_json_object,
)
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
