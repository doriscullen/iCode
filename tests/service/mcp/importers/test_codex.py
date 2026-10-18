# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Codex importer behavior against openai/codex tag rust-v0.163.0."""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest

from chrys.foundation.platform import get_platform
from chrys.foundation.util.env_templates import resolve_env_templates
from chrys.service.mcp.importers import (
    IMPORT_SOURCES,
    ImportContext,
    ImportWarningKind,
    McpSourceError,
    SourceErrorKind,
    UnsupportedReason,
    codex,
    detected_sources,
)
from chrys.service.mcp.importers.base import MAX_REQUEST_TIMEOUT_SECONDS
from chrys.service.mcp.importers.codex import CodexSource
from chrys.service.profiles.agents.schema import MCPServerConfig
from tests.support.symlinks import symlink_or_skip


def _write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


@pytest.fixture
def ctx(tmp_path: Path) -> ImportContext:
    home = tmp_path / "home"
    workspace = tmp_path / "workspace"
    home.mkdir()
    workspace.mkdir()
    return ImportContext(str(workspace.resolve()), home, {})


def _user(ctx: ImportContext, text: str) -> Path:
    return _write(ctx.home / ".codex/config.toml", text)


def _project(ctx: ImportContext, text: str) -> Path:
    assert ctx.workspace_cwd is not None
    return _write(Path(ctx.workspace_cwd) / ".codex/config.toml", text)


def _trust(folder: str | Path, decision: str = "trusted") -> str:
    return f"\n[projects.{json.dumps(str(folder))}]\ntrust_level = {json.dumps(decision)}\n"


def test_registry_detects_codex_without_parsing(ctx: ImportContext) -> None:
    source = next(source for source in IMPORT_SOURCES if source.source_id == "codex")
    assert source.display_name == "Codex"
    assert not source.detect(ctx)
    _user(ctx, "not TOML")
    assert source in detected_sources(ctx)


def test_stdio_literals_disabled_state_and_default_timeout(ctx: ImportContext) -> None:
    _user(
        ctx,
        """
[mcp_servers.files]
command = "~/bin/server"
args = ["${HOME}", "{{literal}}"]
env = { DOLLAR = "${TOKEN}", BRACES = "{{TOKEN}}", EMPTY = "" }
enabled = false
""",
    )
    (candidate,) = CodexSource().discover(ctx).candidates
    assert candidate.config == MCPServerConfig(
        name="files",
        transport="stdio",
        command="~/bin/server",
        args=["${HOME}", "{{literal}}"],
        env={"DOLLAR": "${TOKEN}", "BRACES": "{{{{TOKEN}}}}", "EMPTY": ""},
        enabled=False,
        request_timeout=300,
    )
    assert candidate.scope == "user"
    assert candidate.scope_path == ""
    assert [warning.kind for warning in candidate.warnings] == [ImportWarningKind.DISABLED]


def test_env_references_stay_dynamic_and_explicit_values_win(ctx: ImportContext) -> None:
    _user(
        ctx,
        """
[mcp_servers.files]
command = "server"
env_vars = ["TOKEN", {name = "OTHER", source = "local"}, "OVERRIDE"]
env = { OVERRIDE = "literal {{text}}" }
""",
    )
    context = ImportContext(ctx.workspace_cwd, ctx.home, {"TOKEN": "do-not-copy-this-secret"})
    (candidate,) = CodexSource().discover(context).candidates
    assert candidate.config is not None
    assert candidate.config.env == {"TOKEN": "{{TOKEN}}", "OTHER": "{{OTHER}}", "OVERRIDE": "literal {{{{text}}}}"}
    assert "do-not-copy-this-secret" not in repr(candidate)
    assert [(warning.kind, warning.variable) for warning in candidate.warnings] == [
        (ImportWarningKind.OPTIONAL_VARIABLE, "TOKEN"),
        (ImportWarningKind.OPTIONAL_VARIABLE, "OTHER"),
    ]


def test_http_header_precedence_literal_escaping_and_bearer_token(
    ctx: ImportContext, monkeypatch: pytest.MonkeyPatch
) -> None:
    _user(
        ctx,
        """
[mcp_servers.remote]
url = "https://example.test/mcp"
tool_timeout_sec = 12.1
http_headers = { Authorization = "static", "X-Token" = "static", "X-Raw" = "${TOKEN} {{literal}}" }
env_http_headers = { "x-token" = "TOKEN", AUTHORIZATION = "IGNORED-INVALID-NAME" }
bearer_token_env_var = "BEARER"
""",
    )
    (candidate,) = CodexSource().discover(ctx).candidates
    config = candidate.config
    assert config is not None
    assert config.headers == {
        "authorization": "Bearer {{BEARER}}",
        "x-token": "{{TOKEN}}",
        "x-raw": "${TOKEN} {{{{literal}}}}",
    }
    assert config.request_timeout == 13
    monkeypatch.setenv("TOKEN", "fresh-token")
    assert resolve_env_templates(config.headers["x-token"], location="header") == "fresh-token"
    assert resolve_env_templates(config.headers["x-raw"], location="header") == "${TOKEN} {{literal}}"


def test_tool_allow_list_applies_deny_list_afterwards(ctx: ImportContext) -> None:
    _user(
        ctx,
        """
[mcp_servers.tools]
command = "server"
enabled_tools = ["read", "write", "search"]
disabled_tools = ["write"]
[mcp_servers.empty]
command = "other"
enabled_tools = []
[mcp_servers.deny_only]
command = "third"
disabled_tools = ["delete"]
""",
    )
    candidates = CodexSource().discover(ctx).candidates
    assert [candidate.config.allowed_tools for candidate in candidates if candidate.config] == [
        ["read", "search"],
        [],
        [],
    ]
    assert [(warning.kind, warning.original) for warning in candidates[2].warnings] == [
        (ImportWarningKind.DENY_LIST, "delete")
    ]


def test_dropped_settings_warn_without_copying_credentials_or_running_helpers(ctx: ImportContext) -> None:
    _user(
        ctx,
        """
[mcp_servers.remote]
url = "https://example.test/mcp"
http_headers_helper = "never-execute --token secret-helper-token"
required = true
startup_timeout_sec = 9
default_tools_approval_mode = "prompt"
[mcp_servers.remote.oauth]
client_id = "client"
client_secret = "secret-oauth-value"
[mcp_servers.remote.tools.write]
approval_mode = "prompt"
output_token_limit = 500
""",
    )
    (candidate,) = CodexSource().discover(ctx).candidates
    assert candidate.config is not None
    assert "secret-" not in repr(candidate)
    warnings = {warning.kind: warning.original for warning in candidate.warnings}
    assert ImportWarningKind.OAUTH_IGNORED in warnings
    assert warnings[ImportWarningKind.TOOL_RULES_IGNORED] == "write"
    assert any(warning.original == "http_headers_helper" for warning in candidate.warnings)
    assert "required" in warnings[ImportWarningKind.UNKNOWN_FIELDS_IGNORED]


@pytest.mark.parametrize(
    "setting",
    [
        'environment_id = "remote-executor"',
        'env_vars = [{name = "TOKEN", source = "remote"}]',
    ],
)
def test_remote_execution_cannot_become_a_local_command(ctx: ImportContext, setting: str) -> None:
    _user(ctx, f'[mcp_servers.s]\ncommand = "server"\n{setting}')
    (candidate,) = CodexSource().discover(ctx).candidates
    assert candidate.config is None
    assert candidate.unsupported == UnsupportedReason.REMOTE_ENVIRONMENT
    assert candidate.entry == {"command": "server"}


@pytest.mark.parametrize(
    "entry",
    [
        'command = "server"\nurl = "https://example.test"',
        "enabled = true",
        "command = 12",
        'url = "https://example.test"\nargs = []',
        'url = "https://example.test"\nenv = {}',
        'url = "https://example.test"\ncwd = "/tmp"',
        'command = "server"\nhttp_headers = {}',
        'command = "server"\noauth = {}',
        'command = "server"\nauth = "oauth"',
        'command = "server"\nargs = [1]',
        'command = "server"\nenv = { TOKEN = 1 }',
        'command = "server"\nenabled = "false"',
        'command = "server"\ntool_timeout_sec = nan',
        'command = "server"\ntool_timeout_sec = -1',
        'command = "server"\ntool_timeout_sec = true',
        'command = "server"\nenv_vars = [{name = "TOKEN", source = "invalid"}]',
        'command = "server"\nenv_vars = [{name = "TOKEN", unexpected = true}]',
        'url = "https://example.test"\nbearer_token = "secret"',
        'url = "https://example.test"\nhttp_headers_helper = "  "',
        'url = "https://example.test"\noauth = {client_secret = "secret"}',
        'url = "https://example.test"\noauth = {client_id = "client", client_secret = ""}',
        'url = "https://example.test"\noauth = {callback_port = 65536}',
        'url = "https://example.test"\noauth = {authorization_server_issuer = "issuer"}',
        'command = "server"\ntools = {write = {output_token_limit = 0}}',
        'command = "server"\nomit_tools_from = ["invalid"]',
    ],
)
def test_invalid_entries_are_not_candidates(ctx: ImportContext, entry: str) -> None:
    _user(ctx, f'[mcp_servers.invalid]\n{entry}\n[mcp_servers.good]\ncommand = "good"')
    result = CodexSource().discover(ctx)
    assert [candidate.name for candidate in result.candidates] == ["good"]


@pytest.mark.parametrize(
    "entry",
    [
        'command = "server"\nenv_vars = ["BAD-NAME"]',
        'url = "https://example.test"\nbearer_token_env_var = "BAD-NAME"',
        'url = "https://example.test"\nenv_http_headers = {"X-Key" = "BAD-NAME"}',
    ],
)
def test_unrepresentable_variable_names_are_locked(ctx: ImportContext, entry: str) -> None:
    _user(ctx, f"[mcp_servers.s]\n{entry}")
    (candidate,) = CodexSource().discover(ctx).candidates
    assert candidate.unsupported == UnsupportedReason.VARIABLE_NAME


@pytest.mark.parametrize("text", ["[mcp_servers", "\ufeff[mcp_servers]", 'mcp_servers = "oops"'])
def test_source_errors_are_reported(ctx: ImportContext, text: str) -> None:
    path = _user(ctx, text)
    result = CodexSource().discover(ctx)
    kind = SourceErrorKind.INVALID_CONFIG if "oops" in text else SourceErrorKind.INVALID_TOML
    assert result.candidates == ()
    assert result.source_errors == (McpSourceError(str(path), kind),)


def test_invalid_encoding_is_toml_error(ctx: ImportContext) -> None:
    path = _user(ctx, "")
    path.write_bytes(b"\xff")
    assert CodexSource().discover(ctx).source_errors == (McpSourceError(str(path), SourceErrorKind.INVALID_TOML),)


def test_codex_home_override_and_symlink(ctx: ImportContext, tmp_path: Path) -> None:
    _user(ctx, '[mcp_servers.ignored]\ncommand = "ignored"')
    real = _write(tmp_path / "dotfiles/config.toml", '[mcp_servers.actual]\ncommand = "actual"')
    custom = tmp_path / "custom"
    custom.mkdir()
    symlink_or_skip(custom / "config.toml", real)
    context = ImportContext(ctx.workspace_cwd, ctx.home, {"CODEX_HOME": str(custom)})
    assert [candidate.name for candidate in CodexSource().discover(context).candidates] == ["actual"]


def test_relative_codex_home_uses_workspace_without_expanding_tilde(ctx: ImportContext) -> None:
    assert ctx.workspace_cwd is not None
    _write(Path(ctx.workspace_cwd) / "~/config.toml", '[mcp_servers.actual]\ncommand = "actual"')
    context = ImportContext(ctx.workspace_cwd, ctx.home, {"CODEX_HOME": "~"})
    assert [candidate.name for candidate in CodexSource().discover(context).candidates] == ["actual"]
    assert CodexSource().discover(ImportContext(None, ctx.home, {"CODEX_HOME": "relative"})).candidates == ()


@pytest.mark.parametrize("decision", [None, "untrusted", "trusted"])
def test_project_trust_gates_import_and_cannot_grant_itself_trust(ctx: ImportContext, decision: str | None) -> None:
    assert ctx.workspace_cwd is not None
    _user(ctx, "" if decision is None else _trust(ctx.workspace_cwd, decision))
    _project(ctx, '[mcp_servers.project]\ncommand = "project"' + _trust(ctx.workspace_cwd))
    (candidate,) = CodexSource().discover(ctx).candidates
    assert candidate.scope_path == ctx.workspace_cwd
    if decision == "trusted":
        assert candidate.config is not None
    else:
        assert candidate.config is None
        assert candidate.unsupported == UnsupportedReason.UNTRUSTED_PROJECT


def test_project_layers_merge_nested_values_replace_arrays_and_preserve_disabled_override(ctx: ImportContext) -> None:
    assert ctx.workspace_cwd is not None
    root = Path(ctx.workspace_cwd)
    child = root / "child"
    child.mkdir()
    _write(root / ".git/HEAD", "ref: refs/heads/main")
    _user(
        ctx,
        """
[mcp_servers.shared]
command = "server"
args = ["base"]
env = { BASE = "base", OVERRIDE = "old" }
"""
        + _trust(root),
    )
    project = _project(
        ctx,
        """
[mcp_servers.shared]
args = ["project"]
env = { OVERRIDE = "new", PROJECT = "project" }
""",
    )
    deepest = _write(child / ".codex/config.toml", "[mcp_servers.shared]\nenabled = false")
    context = ImportContext(str(child), ctx.home)
    (candidate,) = CodexSource().discover(context).candidates
    assert candidate.config is not None
    assert candidate.config.args == ["project"]
    assert candidate.config.env == {"BASE": "base", "OVERRIDE": "new", "PROJECT": "project"}
    assert candidate.config.enabled is False
    assert candidate.source_path == str(deepest)
    assert candidate.scope_path == str(child)
    assert any(str(project) in warning.original for warning in candidate.warnings)


def test_untrusted_project_does_not_override_user_server(ctx: ImportContext) -> None:
    _user(ctx, '[mcp_servers.shared]\ncommand = "user"')
    _project(ctx, '[mcp_servers.shared]\ncommand = "project"')
    user, project = CodexSource().discover(ctx).candidates
    assert user.config is not None and user.config.command == "user"
    assert project.config is None and project.unsupported == UnsupportedReason.UNTRUSTED_PROJECT


def test_recursive_merge_does_not_silently_change_transports(ctx: ImportContext) -> None:
    assert ctx.workspace_cwd is not None
    _user(ctx, '[mcp_servers.shared]\ncommand = "server"' + _trust(ctx.workspace_cwd))
    _project(ctx, '[mcp_servers.shared]\nurl = "https://example.test"')
    assert CodexSource().discover(ctx).candidates == ()


def test_custom_root_markers_limit_discovery_and_nearest_trust_wins(ctx: ImportContext) -> None:
    assert ctx.workspace_cwd is not None
    workspace = Path(ctx.workspace_cwd)
    child = workspace / "child"
    child.mkdir()
    _write(workspace / "root.marker", "")
    _user(ctx, 'project_root_markers = ["root.marker"]' + _trust(workspace) + _trust(child, "untrusted"))
    _project(ctx, '[mcp_servers.root]\ncommand = "root"')
    _write(child / ".codex/config.toml", '[mcp_servers.child]\ncommand = "child"')
    result = CodexSource().discover(ImportContext(str(child), ctx.home))
    root, locked = result.candidates
    assert root.config is not None and root.name == "root"
    assert locked.unsupported == UnsupportedReason.UNTRUSTED_PROJECT
    assert locked.name == "child"


def test_without_root_marker_parent_project_config_is_not_read(ctx: ImportContext) -> None:
    assert ctx.workspace_cwd is not None
    child = Path(ctx.workspace_cwd) / "child"
    child.mkdir()
    _user(ctx, _trust(ctx.workspace_cwd))
    _project(ctx, '[mcp_servers.parent]\ncommand = "parent"')
    assert CodexSource().discover(ImportContext(str(child), ctx.home)).candidates == ()


def test_unknown_fields_warn_by_name_without_leaking_values(ctx: ImportContext) -> None:
    _user(ctx, '[mcp_servers.s]\ncommand = "server"\nfuture_setting = "secret-value"')
    (candidate,) = CodexSource().discover(ctx).candidates
    assert candidate.config is not None
    assert [warning.original for warning in candidate.warnings] == ["future_setting"]
    assert "secret-value" not in repr(candidate)


def test_codex_filtered_variables_are_not_copied(ctx: ImportContext) -> None:
    _user(
        ctx,
        """
[mcp_servers.s]
command = "server"
env_vars = ["NODE_REPL_AUTH_TOKEN"]
env = { openai_identity_token_file = "secret-location", NORMAL = "value" }
""",
    )
    (candidate,) = CodexSource().discover(ctx).candidates
    assert candidate.config is not None
    assert candidate.config.env == {"NORMAL": "value"}
    assert "secret-location" not in repr(candidate)
    assert {warning.original for warning in candidate.warnings} == {
        "env.openai_identity_token_file",
        "env_vars.NODE_REPL_AUTH_TOKEN",
    }


def test_windows_explicit_env_overrides_forwarded_variable_caselessly(
    ctx: ImportContext, monkeypatch: pytest.MonkeyPatch
) -> None:
    platform = replace(get_platform(), os_name="windows")
    monkeypatch.setattr(codex, "get_platform", lambda: platform)
    _user(ctx, '[mcp_servers.s]\ncommand = "server"\nenv_vars = ["TOKEN"]\nenv = {token = "literal"}')
    (candidate,) = CodexSource().discover(ctx).candidates
    assert candidate.config is not None
    assert candidate.config.env == {"token": "literal"}
    assert candidate.warnings == ()


def test_trust_accepts_original_symlink_spelling(ctx: ImportContext, tmp_path: Path) -> None:
    assert ctx.workspace_cwd is not None
    alias = tmp_path / "alias"
    symlink_or_skip(alias, Path(ctx.workspace_cwd), target_is_directory=True)
    _user(ctx, _trust(alias))
    _project(ctx, '[mcp_servers.s]\ncommand = "server"')
    (candidate,) = CodexSource().discover(ImportContext(str(alias), ctx.home)).candidates
    assert candidate.config is not None


def test_empty_project_trust_entry_does_not_mask_root_trust(ctx: ImportContext) -> None:
    assert ctx.workspace_cwd is not None
    root = Path(ctx.workspace_cwd)
    child = root / "child"
    child.mkdir()
    _write(root / ".git/HEAD", "ref: refs/heads/main")
    _user(ctx, _trust(root) + f"\n[projects.{json.dumps(str(child))}]\n")
    _write(child / ".codex/config.toml", '[mcp_servers.s]\ncommand = "server"')
    (candidate,) = CodexSource().discover(ImportContext(str(child), ctx.home)).candidates
    assert candidate.config is not None


def test_a_timeout_longer_than_icode_can_wait_is_capped(ctx: ImportContext) -> None:
    _user(ctx, '[mcp_servers.slow]\ncommand = "server"\ntool_timeout_sec = 1e18\n')

    (candidate,) = CodexSource().discover(ctx).candidates

    assert candidate.config is not None
    assert candidate.config.request_timeout == MAX_REQUEST_TIMEOUT_SECONDS
