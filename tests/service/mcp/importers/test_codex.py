# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Codex importer behavior against openai/codex tag rust-v0.166.0."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from chrys.foundation.util.env_templates import resolve_env_templates
from chrys.service.mcp.importers import (
    IMPORT_SOURCES,
    ImportContext,
    ImportWarningKind,
    McpSourceError,
    SourceErrorKind,
    UnsupportedReason,
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
env = { DOLLAR = "${TOKEN}", BRACES = "{{TOKEN}}" }
enabled = false
""",
    )
    (candidate,) = CodexSource().discover(ctx).candidates
    assert candidate.config == MCPServerConfig(
        name="files",
        transport="stdio",
        command="~/bin/server",
        args=["${HOME}", "{{literal}}"],
        env={"DOLLAR": "${TOKEN}", "BRACES": "{{{{TOKEN}}}}"},
        enabled=False,
        request_timeout=300,
    )
    assert candidate.scope == "user"
    assert candidate.scope_path == ""
    assert [warning.kind for warning in candidate.warnings] == [ImportWarningKind.LITERAL_BRACES_ESCAPED]


def test_forwarded_variables_are_left_to_the_inherited_environment(ctx: ImportContext) -> None:
    # Codex forwards each one only while it is set; iCode's stdio servers inherit the environment,
    # where a {{VAR}} reference would fail the connection whenever the variable is unset.
    _user(
        ctx,
        """
[mcp_servers.files]
command = "server"
env_vars = ["TOKEN", {name = "OTHER", source = "local"}, "OVERRIDE", "BAD-NAME", "NODE_REPL_AUTH_TOKEN"]
env = { OVERRIDE = "literal {{text}}" }
""",
    )
    (candidate,) = CodexSource().discover(ctx).candidates
    assert candidate.config is not None
    assert candidate.config.env == {"OVERRIDE": "literal {{{{text}}}}"}
    assert [warning.kind for warning in candidate.warnings] == [ImportWarningKind.LITERAL_BRACES_ESCAPED]


@pytest.mark.parametrize("variable", ["PYTHONPATH", "PYTHONHOME"])
def test_explicit_python_environment_forwarding_survives_runtime_filtering(
    ctx: ImportContext, monkeypatch: pytest.MonkeyPatch, variable: str
) -> None:
    from chrys.foundation.util.env_templates import EnvVarResolutionError
    from chrys.service.mcp.cache import resolve_mcp_effective_config

    _user(ctx, f'[mcp_servers.py]\ncommand = "python"\nenv_vars = ["{variable}"]\n')
    monkeypatch.delenv(variable, raising=False)
    (candidate,) = CodexSource().discover(ctx).candidates
    assert candidate.config is not None
    assert candidate.config.env == {variable: "{{" + variable + "}}"}
    assert [(w.kind, w.variable) for w in candidate.warnings] == [(ImportWarningKind.OPTIONAL_VARIABLE, variable)]
    with pytest.raises(EnvVarResolutionError):
        resolve_mcp_effective_config(candidate.config)
    monkeypatch.setenv(variable, "/user/python/path")
    effective = resolve_mcp_effective_config(candidate.config)
    assert effective.stdio_env is not None and effective.stdio_env[variable] == "/user/python/path"


def test_explicit_environment_values_override_forwarded_python_variables(ctx: ImportContext) -> None:
    _user(ctx, '[mcp_servers.py]\ncommand = "python"\nenv_vars = ["PYTHONPATH"]\nenv = {PYTHONPATH = "explicit"}\n')
    (candidate,) = CodexSource().discover(ctx).candidates
    assert candidate.config is not None and candidate.config.env == {"PYTHONPATH": "explicit"}
    assert candidate.warnings == ()


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


def test_header_values_are_imported_trimmed_as_servers_receive_them(ctx: ImportContext) -> None:
    # Codex sends a static header's leading and trailing spaces and tabs [rmcp-client/src/utils.rs], which a
    # server doesn't read as part of the value (RFC 9110, "Field Values"); iCode's HTTP client refuses to send them.
    _user(
        ctx,
        """
[mcp_servers.remote]
url = "https://example.test/mcp"
http_headers = { Authorization = "Bearer ", "X-Pad" = " \\tpadded {{literal}}\\t " }

[mcp_servers.local]
command = "server"
env = { PAD = " kept " }
""",
    )
    candidates = {candidate.name: candidate for candidate in CodexSource().discover(ctx).candidates}
    remote = candidates["remote"].config
    assert remote is not None
    assert remote.headers == {"authorization": "Bearer", "x-pad": "padded {{{{literal}}}}"}
    assert candidates["local"].config is None
    assert candidates["local"].unsupported is UnsupportedReason.ENV_VALUE_WHITESPACE


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


@pytest.mark.parametrize(
    ("rules", "warning_kind"),
    [
        ("tools = {write = {output_token_limit = 0}}", ImportWarningKind.TOOL_OUTPUT_LIMIT_IGNORED),
        ('tools = "not a policy table"', ImportWarningKind.UNKNOWN_FIELDS_IGNORED),
        ('default_tools_approval_mode = "future-mode"', ImportWarningKind.TOOL_PERMISSIONS_IGNORED),
    ],
)
def test_source_tool_policies_do_not_gate_valid_server_settings(
    ctx: ImportContext, rules: str, warning_kind: ImportWarningKind
) -> None:
    _user(ctx, '[mcp_servers.server]\ncommand = "server"\n' + rules)
    (candidate,) = CodexSource().discover(ctx).candidates
    assert candidate.config is not None and candidate.config.command == "server"
    assert candidate.config.allowed_tools is None
    assert any(w.kind is warning_kind for w in candidate.warnings)


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
    assert (
        warnings[ImportWarningKind.TOOL_PERMISSIONS_IGNORED] == "default_tools_approval_mode, tools.write.approval_mode"
    )
    assert warnings[ImportWarningKind.TOOL_OUTPUT_LIMIT_IGNORED] == "tools.write.output_token_limit"
    assert ImportWarningKind.HEADERS_HELPER_IGNORED in warnings
    assert "required" in warnings[ImportWarningKind.UNKNOWN_FIELDS_IGNORED]
    assert "startup_timeout_sec" not in warnings[ImportWarningKind.UNKNOWN_FIELDS_IGNORED]
    assert ImportWarningKind.STARTUP_TIMEOUT_IGNORED in warnings


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
    ],
)
def test_invalid_entries_are_not_candidates(ctx: ImportContext, entry: str) -> None:
    _user(ctx, f'[mcp_servers.invalid]\n{entry}\n[mcp_servers.good]\ncommand = "good"')
    result = CodexSource().discover(ctx)
    assert [candidate.name for candidate in result.candidates] == ["good"]


@pytest.mark.parametrize(
    "field",
    [
        "oauth",
        "oauth_resource",
        "auth",
        "http_headers_helper",
        "startup_readiness",
        "startup_timeout_sec",
        "startup_timeout_ms",
        "required",
        "supports_parallel_tool_calls",
        "scopes",
        "omit_tools_from",
        "tool_input_schema_max_bytes",
        "name",
        "future_setting",
    ],
)
@pytest.mark.parametrize("value", ['"future-mode"', '{secret = "omitted-secret"}', '["omitted-secret"]', "false", "42"])
def test_omitted_settings_do_not_gate_migration(ctx: ImportContext, field: str, value: str) -> None:
    _user(ctx, f'[mcp_servers.remote]\nurl = "https://example.test/mcp"\n{field} = {value}')

    result = CodexSource().discover(ctx)
    (candidate,) = result.candidates
    assert result.source_errors == ()
    assert candidate.config == MCPServerConfig(
        name="remote", transport="http", url="https://example.test/mcp", request_timeout=300
    )
    assert candidate.warnings
    assert "omitted-secret" not in repr(result)


@pytest.mark.parametrize("value", ['{secret = "omitted-secret"}', '["omitted-secret"]', "42", "false"])
def test_an_unrecognized_working_folder_is_reported_without_its_value(ctx: ImportContext, value: str) -> None:
    _user(ctx, f'[mcp_servers.local]\ncommand = "server"\ncwd = {value}')

    result = CodexSource().discover(ctx)
    (candidate,) = result.candidates
    assert candidate.config is not None and candidate.config.command == "server"
    assert [(warning.kind, warning.original) for warning in candidate.warnings] == [
        (ImportWarningKind.UNKNOWN_FIELDS_IGNORED, "cwd")
    ]
    assert "omitted-secret" not in repr(result)


@pytest.mark.parametrize("method", ["client_secret_basic", "client_secret_post"])
def test_an_ema_client_with_a_secret_may_name_its_token_endpoint_auth_method(ctx: ImportContext, method: str) -> None:
    _user(
        ctx,
        '[mcp_servers.s]\nurl = "https://example.test"\nauth = "ema_auth"\n'
        f'oauth = {{client_id = "client", client_secret = "secret", token_endpoint_auth_method = "{method}"}}',
    )
    assert [candidate.name for candidate in CodexSource().discover(ctx).candidates] == ["s"]


@pytest.mark.parametrize(
    "entry",
    [
        'url = "https://example.test"\nbearer_token_env_var = "BAD-NAME"',
        'url = "https://example.test"\nenv_http_headers = {"X-Key" = "BAD-NAME"}',
    ],
)
def test_unrepresentable_variable_names_are_locked(ctx: ImportContext, entry: str) -> None:
    _user(ctx, f"[mcp_servers.s]\n{entry}")
    (candidate,) = CodexSource().discover(ctx).candidates
    assert candidate.unsupported == UnsupportedReason.VARIABLE_NAME


@pytest.mark.parametrize(
    ("text", "kind", "field", "line", "column"),
    [
        ("[mcp_servers", SourceErrorKind.INVALID_TOML, "", 1, 13),
        ("\ufeff[mcp_servers]", SourceErrorKind.INVALID_TOML, "", 1, 1),
        ('mcp_servers = "oops"', SourceErrorKind.INVALID_CONFIG, "mcp_servers", None, None),
    ],
)
def test_source_errors_are_reported(
    ctx: ImportContext, text: str, kind: SourceErrorKind, field: str, line: int | None, column: int | None
) -> None:
    path = _user(ctx, text)
    result = CodexSource().discover(ctx)
    assert result.candidates == ()
    assert result.source_errors == (McpSourceError(str(path), kind, field=field, line=line, column=column),)


def test_invalid_encoding_is_reported_separately(ctx: ImportContext) -> None:
    path = _user(ctx, "")
    path.write_bytes(b"\xff")
    assert CodexSource().discover(ctx).source_errors == (McpSourceError(str(path), SourceErrorKind.INVALID_ENCODING),)


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
def test_project_import_does_not_depend_on_codex_trust(ctx: ImportContext, decision: str | None) -> None:
    assert ctx.workspace_cwd is not None
    _user(ctx, "" if decision is None else _trust(ctx.workspace_cwd, decision))
    _project(ctx, '[mcp_servers.project]\ncommand = "project"' + _trust(ctx.workspace_cwd))
    (candidate,) = CodexSource().discover(ctx).candidates
    assert candidate.scope_path == ctx.workspace_cwd
    assert candidate.config is not None
    assert candidate.unsupported is None


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


def test_project_settings_are_available_without_a_source_trust_decision(ctx: ImportContext) -> None:
    _user(ctx, '[mcp_servers.shared]\ncommand = "user"')
    _project(ctx, '[mcp_servers.shared]\ncommand = "project"')
    (project,) = CodexSource().discover(ctx).candidates
    assert project.config is not None and project.config.command == "project"
    assert any(w.kind is ImportWarningKind.MERGED for w in project.warnings)


def test_recursive_merge_does_not_silently_change_transports(ctx: ImportContext) -> None:
    assert ctx.workspace_cwd is not None
    _user(ctx, '[mcp_servers.shared]\ncommand = "server"' + _trust(ctx.workspace_cwd))
    _project(ctx, '[mcp_servers.shared]\nurl = "https://example.test"')
    assert CodexSource().discover(ctx).candidates == ()


def test_custom_root_markers_limit_discovery_without_enforcing_trust(ctx: ImportContext) -> None:
    assert ctx.workspace_cwd is not None
    workspace = Path(ctx.workspace_cwd)
    child = workspace / "child"
    child.mkdir()
    _write(workspace / "root.marker", "")
    _user(ctx, 'project_root_markers = ["root.marker"]' + _trust(workspace) + _trust(child, "untrusted"))
    _project(ctx, '[mcp_servers.root]\ncommand = "root"')
    _write(child / ".codex/config.toml", '[mcp_servers.child]\ncommand = "child"')
    result = CodexSource().discover(ImportContext(str(child), ctx.home))
    root, project = result.candidates
    assert root.config is not None and root.name == "root"
    assert project.config is not None and project.unsupported is None
    assert project.name == "child"


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
env = { openai_identity_token_file = "secret-location", NORMAL = "value" }
""",
    )
    (candidate,) = CodexSource().discover(ctx).candidates
    assert candidate.config is not None
    assert candidate.config.env == {"NORMAL": "value"}
    assert "secret-location" not in repr(candidate)
    assert {warning.original for warning in candidate.warnings} == {"env.openai_identity_token_file"}


def test_project_discovery_accepts_symlink_spelling(ctx: ImportContext, tmp_path: Path) -> None:
    assert ctx.workspace_cwd is not None
    alias = tmp_path / "alias"
    symlink_or_skip(alias, Path(ctx.workspace_cwd), target_is_directory=True)
    _user(ctx, _trust(alias))
    _project(ctx, '[mcp_servers.s]\ncommand = "server"')
    (candidate,) = CodexSource().discover(ImportContext(str(alias), ctx.home)).candidates
    assert candidate.config is not None


def test_empty_project_trust_entry_does_not_block_import(ctx: ImportContext) -> None:
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
    assert candidate.timeout_source_seconds == 1e18


@pytest.mark.parametrize(
    ("source", "converted", "preview_source"),
    [("1.501", 2, 1.501), ("1.0", 1, None), ("300", 300, None)],
)
def test_timeout_preview_records_only_changed_source_seconds(
    ctx: ImportContext, source: str, converted: int, preview_source: float | None
) -> None:
    _user(ctx, f'[mcp_servers.s]\ncommand = "server"\ntool_timeout_sec = {source}\n')

    (candidate,) = CodexSource().discover(ctx).candidates

    assert candidate.config is not None and candidate.config.request_timeout == converted
    assert candidate.timeout_source_seconds == preview_source


def test_zero_timeout_keeps_runtime_default_and_has_a_dedicated_warning(ctx: ImportContext) -> None:
    _user(ctx, '[mcp_servers.s]\ncommand = "server"\ntool_timeout_sec = 0\n')

    (candidate,) = CodexSource().discover(ctx).candidates

    assert candidate.config is not None and candidate.config.request_timeout is None
    assert candidate.timeout_source_seconds is None
    assert [warning.kind for warning in candidate.warnings] == [ImportWarningKind.ZERO_TIMEOUT_IGNORED]


@pytest.mark.parametrize(
    ("settings", "field"),
    [
        ('env_http_headers = {"X-Key" = "BAD-NAME"}', "env_http_headers.X-Key"),
        ('bearer_token_env_var = "BAD-NAME"', "bearer_token_env_var"),
    ],
)
def test_unsupported_variable_metadata_identifies_the_source_reference(
    ctx: ImportContext, settings: str, field: str
) -> None:
    _user(ctx, '[mcp_servers.s]\nurl = "https://example.test"\n' + settings)

    (candidate,) = CodexSource().discover(ctx).candidates

    assert candidate.config is None
    assert candidate.unsupported is UnsupportedReason.VARIABLE_NAME
    assert candidate.unsupported_field == field
    assert candidate.unsupported_variable == "BAD-NAME"


@pytest.mark.parametrize(
    ("auth", "warning_kind"),
    [
        ("oauth", ImportWarningKind.OAUTH_IGNORED),
        ("chatgpt", ImportWarningKind.AUTH_PROVIDER_IGNORED),
        ("ema_auth", ImportWarningKind.AUTH_PROVIDER_IGNORED),
    ],
)
def test_http_authentication_has_one_applicable_migration_warning(
    ctx: ImportContext, auth: str, warning_kind: ImportWarningKind
) -> None:
    _user(ctx, f'[mcp_servers.s]\nurl = "https://example.test"\nauth = "{auth}"\n')

    (candidate,) = CodexSource().discover(ctx).candidates

    assert [warning.kind for warning in candidate.warnings] == [warning_kind]


@pytest.mark.parametrize("settings", ["", 'http_headers = {Authorization = "Bearer token"}'])
def test_http_without_explicit_oauth_has_no_sign_in_warning(ctx: ImportContext, settings: str) -> None:
    _user(ctx, '[mcp_servers.s]\nurl = "https://example.test"\n' + settings)

    (candidate,) = CodexSource().discover(ctx).candidates

    assert candidate.config is not None
    assert candidate.warnings == ()


@pytest.mark.parametrize("settings", ['oauth = {client_id = "client"}', 'oauth_resource = "https://example.test"'])
def test_explicit_oauth_settings_warn_without_auth_selector(ctx: ImportContext, settings: str) -> None:
    _user(ctx, '[mcp_servers.s]\nurl = "https://example.test"\n' + settings)

    (candidate,) = CodexSource().discover(ctx).candidates

    assert [warning.kind for warning in candidate.warnings] == [ImportWarningKind.OAUTH_IGNORED]


def test_startup_timeout_warns_without_replacing_request_timeout(ctx: ImportContext) -> None:
    _user(ctx, '[mcp_servers.s]\ncommand = "server"\nstartup_timeout_sec = 120\ntool_timeout_sec = 60\n')

    (candidate,) = CodexSource().discover(ctx).candidates

    assert candidate.config is not None and candidate.config.request_timeout == 60
    assert [warning.kind for warning in candidate.warnings] == [ImportWarningKind.STARTUP_TIMEOUT_IGNORED]


def test_tool_output_limits_are_not_reported_as_approval_permissions(ctx: ImportContext) -> None:
    _user(
        ctx,
        '[mcp_servers.s]\ncommand = "server"\n'
        'tools = {inspect = {output_token_limit = 600}, second = {future_setting = "private-sentinel"}}\n',
    )

    (candidate,) = CodexSource().discover(ctx).candidates
    warnings = {warning.kind: warning.original for warning in candidate.warnings}

    assert warnings == {
        ImportWarningKind.TOOL_OUTPUT_LIMIT_IGNORED: "tools.inspect.output_token_limit",
        ImportWarningKind.UNKNOWN_FIELDS_IGNORED: "tools.second.future_setting",
    }
    assert candidate.config is not None and candidate.config.allowed_tools is None
    assert "private-sentinel" not in repr(candidate)


@pytest.mark.parametrize(
    "settings",
    [
        'command = "server{{literal}}"\nargs = ["{{literal}}"]',
        'command = "server"\nenv = {NODE_REPL_AUTH_TOKEN = "{{literal}}"}',
        (
            'url = "https://example.test"\nhttp_headers = {"X-Key" = "{{literal}}"}\n'
            'env_http_headers = {"X-Key" = "TOKEN"}'
        ),
        'url = "https://example.test"\nhttp_headers = {Authorization = "{{literal}}"}\nbearer_token_env_var = "TOKEN"',
    ],
)
def test_literal_brace_warning_only_describes_retained_values(ctx: ImportContext, settings: str) -> None:
    _user(ctx, "[mcp_servers.s]\n" + settings)

    (candidate,) = CodexSource().discover(ctx).candidates

    assert all(warning.kind is not ImportWarningKind.LITERAL_BRACES_ESCAPED for warning in candidate.warnings)


def test_invalid_header_values_are_omitted_before_trimming_without_disclosing_them(ctx: ImportContext) -> None:
    _user(
        ctx,
        """
[mcp_servers.s]
url = "https://example.test"
http_headers = { "Bad Header" = "secret-name-value", "X-Test" = "\\nsecret-newline\\n", "X-Control" = "\\u0001secret-control", "X-Good" = "kept", "X-Unicode" = "你好", "X-Empty" = "" }
env_http_headers = { "Bad Env Header" = "TOKEN" }
""",
    )

    (candidate,) = CodexSource().discover(ctx).candidates

    assert candidate.config is not None
    assert candidate.config.headers == {"x-good": "kept", "x-unicode": "你好", "x-empty": ""}
    assert {
        (warning.field, warning.original)
        for warning in candidate.warnings
        if warning.kind is ImportWarningKind.INVALID_HEADER_IGNORED
    } == {
        ("http_headers.Bad Header", "name"),
        ("http_headers.X-Test", "value"),
        ("http_headers.X-Control", "value"),
        ("env_http_headers.Bad Env Header", "name"),
    }
    assert "secret-" not in repr(candidate)


@pytest.mark.parametrize(
    "settings",
    [
        'http_headers = {"x-test" = "secret-lower", "X-Test" = "secret-upper"}',
        'env_http_headers = {"x-test" = "LOWER_TOKEN", "X-Test" = "UPPER_TOKEN"}',
    ],
)
def test_effective_header_alias_conflicts_are_not_silently_selected(ctx: ImportContext, settings: str) -> None:
    _user(ctx, '[mcp_servers.s]\nurl = "https://example.test"\n' + settings)

    (candidate,) = CodexSource().discover(ctx).candidates

    assert candidate.config is None
    assert candidate.unsupported is UnsupportedReason.HEADER_NAME_COLLISION
    assert candidate.unsupported_field == "x-test"
    assert candidate.unsupported_variable == ""
    assert candidate.warnings == ()


@pytest.mark.parametrize("with_static_headers", [False, True])
def test_unsupported_header_preview_keeps_original_maps_separate(
    ctx: ImportContext, monkeypatch: pytest.MonkeyPatch, with_static_headers: bool
) -> None:
    monkeypatch.setenv("MCP_IMPORT_TEST", "resolved-lower-secret")
    monkeypatch.setenv("CODEX_PROBE_TOKEN", "resolved-upper-secret")
    static = {"x-test": "literal-lower", "X-Test": "literal-upper"} if with_static_headers else {}
    settings = 'http_headers = {"x-test" = "literal-lower", "X-Test" = "literal-upper"}\n' if static else ""
    _user(
        ctx,
        '[mcp_servers.s]\nurl = "http://127.0.0.1:18767/mcp"\n'
        + settings
        + 'env_http_headers = {"x-test" = "MCP_IMPORT_TEST", "X-Test" = "CODEX_PROBE_TOKEN"}',
    )

    (candidate,) = CodexSource().discover(ctx).candidates

    assert candidate.config is None
    assert candidate.unsupported is UnsupportedReason.HEADER_NAME_COLLISION
    assert candidate.entry == {
        "url": "http://127.0.0.1:18767/mcp",
        "headers": static,
        "env_http_headers": {"x-test": "MCP_IMPORT_TEST", "X-Test": "CODEX_PROBE_TOKEN"},
    }
    assert "resolved-" not in repr(candidate)


def test_bearer_masks_header_alias_conflicts_and_invalid_authorization_variable(ctx: ImportContext) -> None:
    _user(
        ctx,
        """
[mcp_servers.s]
url = "https://example.test"
http_headers = {Authorization = "one", AUTHORIZATION = "two"}
env_http_headers = {Authorization = "BAD-NAME", AUTHORIZATION = "OTHER"}
bearer_token_env_var = "BEARER"
""",
    )

    (candidate,) = CodexSource().discover(ctx).candidates

    assert candidate.config is not None
    assert candidate.config.headers == {"authorization": "Bearer {{BEARER}}"}
    assert candidate.unsupported is None


def test_source_error_metadata_does_not_include_the_source_line(ctx: ImportContext) -> None:
    _user(ctx, '[mcp_servers.s]\nurl = "private-credential-sentinel" trailing\n')

    result = CodexSource().discover(ctx)

    assert result.candidates == ()
    (error,) = result.source_errors
    assert error.kind is SourceErrorKind.INVALID_TOML
    assert error.line == 2
    assert error.column is not None
    assert "private-credential-sentinel" not in repr(result)
