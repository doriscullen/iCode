# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Import local Codex MCP configuration without starting Codex or its servers.

Baseline: https://github.com/openai/codex, tag ``rust-v0.166.0`` (0.166.0),
commit ``6ff1e1ca3b30fc8b914720681fcf5fe62e95f856``. Source paths below are
relative to ``codex-rs`` at that commit; the release source is available at
https://github.com/openai/codex/tree/rust-v0.166.0/codex-rs.
Rechecked 2026-11-04: still the latest stable release. Main at
``021fccb046508938c4754982efccfb51d6e8356e`` and 0.168.0-alpha.1 leave the
migrated connection schema, merge, environment and header contracts unchanged.

Read ``$CODEX_HOME/config.toml`` (default ``~/.codex/config.toml``), then
``.codex/config.toml`` from the nearest project root down to the workspace.
The root uses ``project_root_markers`` (default ``[".git"]``); absent a
marker, only the workspace is searched [config/src/loader/mod.rs:discover_project_layers].
Source project trust does not gate this user-selected import. Tables merge
recursively; arrays and scalars replace [config/src/merge.rs].

Transport inference follows config/src/mcp_types.rs. Only settings used by
migration are validated; omitted runtime and authentication settings do not
gate import. The baseline records verified conversion rules, not a version
requirement for the source application. Values
in ``env`` and ``http_headers`` are literal; header references to environment
variables become iCode templates, never captured secrets. A stdio server's
``env_vars``, which Codex forwards only while set, are generally inherited.
Python path overrides that iCode filters from inherited variables are written as
explicit references, with a warning about requiring nonempty values.
Environment headers beat static headers; bearer
auth beats Authorization [rmcp-client/src/utils.rs, codex-mcp/src/rmcp_client.rs].
Optional header references get a warning because iCode requires them to be set.
The default tool timeout is 300 seconds [codex-mcp/src/rmcp_client.rs:122]. Deny-only tool filters are
imported with no tools, since iCode cannot express a deny list without a catalog.

Not read: system/managed/cloud configuration, selected profile files,
session overrides, plugin servers, stored OAuth credentials, or project trust.
Tool approval policies are reported without validation or migration.
Remote execution environments cannot be imported.
Ignored runtime settings are named in warnings. iCode's subprocess environment
inheritance differs from Codex's filtered environment. No commands, header
helpers, OAuth flows or network discovery run during import.
Explicit variables on Codex's non-inheritable list are omitted with a warning
[protocol/src/shell_environment.rs::NON_INHERITABLE_ENV_VARS].

When updating the baseline, re-check these files and their tests, especially
the connection fields, variable handling, discovery and merge rules.
"""

from __future__ import annotations

import math
import os
import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from chrys.foundation.platform.runtime_paths import strip_python_runtime_overrides
from chrys.service.mcp.importers._codex_headers import HeaderIgnoreReason, normalize_codex_headers
from chrys.service.mcp.importers.base import (
    ImportContext,
    ImportWarning,
    ImportWarningKind,
    McpImportCandidate,
    McpImportResult,
    McpSourceError,
    SourceErrorKind,
    SourceReadError,
    TemplateVariable,
    UnsupportedReason,
    read_source_bytes,
    render_template,
    request_timeout_seconds,
    unsupported_env_value,
)
from chrys.service.profiles.agents.schema import MCPServerConfig

_NAME = re.compile(r"[A-Za-z0-9_:@/.-]+")
_VARIABLE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_STDIO_FIELDS = {"command", "args", "env", "env_vars", "cwd"}
_HTTP_FIELDS = {
    "url",
    "bearer_token_env_var",
    "http_headers",
    "env_http_headers",
    "http_headers_helper",
    "oauth",
    "oauth_resource",
    "auth",
}
_CARRIED_FIELDS = (
    _STDIO_FIELDS
    | _HTTP_FIELDS
    | {"enabled", "tool_timeout_sec", "startup_timeout_sec", "enabled_tools", "disabled_tools"}
)
# protocol/src/shell_environment.rs::NON_INHERITABLE_ENV_VARS, case-insensitive on every OS.
_NON_INHERITABLE = frozenset(
    {
        "CODEX_EXEC_SERVER_NOISE_AUTH_TOKEN",
        "NODE_REPL_AUTH_TOKEN",
        "CODEX_GUARDIAN_DECISIONS_API_KEY",
        "OPENAI_FEDERATION_RULE_ID",
        "OPENAI_IDENTITY_TOKEN_FILE",
        "OPENAI_WORKLOAD_IDENTITY_CONTEXT",
    }
)


@dataclass(slots=True)
class _Entry:
    value: dict[str, Any]
    path: Path
    scope: str
    folder: str = ""
    sources: list[str] = field(default_factory=list)


class CodexSource:
    """Reads Codex's user file and project layers for the user to choose from."""

    @property
    def source_id(self) -> str:
        return "codex"

    @property
    def display_name(self) -> str:
        return "Codex"

    def detect(self, ctx: ImportContext) -> bool:
        home = _codex_home(ctx)
        if home is not None and (home / "config.toml").exists():
            return True
        return any((folder / ".codex/config.toml").exists() for folder in _project_folders(ctx, {}))

    def discover(self, ctx: ImportContext) -> McpImportResult:
        errors: list[McpSourceError] = []
        entries: dict[str, _Entry] = {}
        home = _codex_home(ctx)
        if home is None:
            return McpImportResult()
        user_path = home / "config.toml"
        user = _read(user_path, errors)
        _add_layer(entries, user, user_path, "user", "", errors)
        for folder in _project_folders(ctx, user):
            path = folder / ".codex/config.toml"
            if os.path.realpath(path) == os.path.realpath(user_path):
                continue
            document = _read(path, errors)
            _add_layer(entries, document, path, "project", str(folder), errors)
        candidates = [_candidate(name, entry) for name, entry in entries.items() if _valid(name, entry.value)]
        return McpImportResult(tuple(candidates), tuple(errors))


def _codex_home(ctx: ImportContext) -> Path | None:
    override = ctx.environ.get("CODEX_HOME")
    if not override:
        return ctx.home / ".codex"
    path = Path(override)
    if not path.is_absolute():
        if ctx.workspace_cwd is None:
            return None
        path = Path(ctx.workspace_cwd) / path
    return path


def _read(path: Path, errors: list[McpSourceError]) -> dict[str, Any]:
    try:
        raw = read_source_bytes(path)
        return {} if raw is None else tomllib.loads(raw.decode("utf-8"))
    except SourceReadError as exc:
        errors.append(McpSourceError(str(path), exc.kind))
    except UnicodeDecodeError:
        errors.append(McpSourceError(str(path), SourceErrorKind.INVALID_ENCODING))
    except tomllib.TOMLDecodeError as exc:
        errors.append(McpSourceError(str(path), SourceErrorKind.INVALID_TOML, line=exc.lineno, column=exc.colno))
    except ValueError, RecursionError:
        errors.append(McpSourceError(str(path), SourceErrorKind.INVALID_TOML))
    return {}


def _project_folders(ctx: ImportContext, user: dict[str, Any]) -> list[Path]:
    if not ctx.workspace_cwd:
        return []
    workspace = Path(os.path.abspath(ctx.workspace_cwd))
    markers = user.get("project_root_markers", [".git"])
    if not _strings(markers):
        return []
    ancestors = (workspace, *workspace.parents)
    root = next((folder for folder in ancestors if any(_has_marker(folder, marker) for marker in markers)), workspace)
    result: list[Path] = []
    for folder in ancestors:
        result.append(folder)
        if folder == root:
            break
    return list(reversed(result))


def _has_marker(folder: Path, marker: str) -> bool:
    path = folder / marker
    try:
        return path.exists() and (marker != ".git" or not path.is_dir() or (path / "HEAD").exists())
    except OSError, ValueError:
        return False


def _merge(base: dict[str, Any], overlay: dict[str, Any]) -> dict[str, Any]:
    merged = dict(base)
    for key, value in overlay.items():
        previous = merged.get(key)
        merged[key] = _merge(previous, value) if isinstance(previous, dict) and isinstance(value, dict) else value
    return merged


def _add_layer(
    entries: dict[str, _Entry],
    document: dict[str, Any],
    path: Path,
    scope: str,
    folder: str,
    errors: list[McpSourceError],
) -> None:
    servers = document.get("mcp_servers", {})
    if not isinstance(servers, dict):
        errors.append(McpSourceError(str(path), SourceErrorKind.INVALID_CONFIG, field="mcp_servers"))
        return
    for name, value in servers.items():
        previous = entries.get(name)
        if not isinstance(value, dict):
            entries.pop(name, None)
            continue
        merged = _merge(previous.value, value) if previous else value
        sources = [*previous.sources, str(previous.path)] if previous else []
        entries[name] = _Entry(merged, path, scope, folder, sources)


def _strings(value: object) -> bool:
    return isinstance(value, list) and all(isinstance(item, str) for item in value)


def _string_map(value: object) -> bool:
    return isinstance(value, dict) and all(isinstance(item, str) for item in value.values())


def _duration(value: object) -> bool:
    if type(value) is int:
        return 0 <= value < 2**64
    return isinstance(value, float) and math.isfinite(value) and 0 <= value < 2**64


def _valid(name: str, value: dict[str, Any]) -> bool:
    if not _NAME.fullmatch(name) or "bearer_token" in value:
        return False
    stdio = "command" in value
    if (stdio and value.keys() & _HTTP_FIELDS) or (not stdio and ("url" not in value or value.keys() & _STDIO_FIELDS)):
        return False
    for key in ("command", "url", "bearer_token_env_var", "environment_id"):
        if key in value and not isinstance(value[key], str):
            return False
    for key in ("args", "enabled_tools", "disabled_tools"):
        if key in value and not _strings(value[key]):
            return False
    for key in ("env", "http_headers", "env_http_headers"):
        if key in value and not _string_map(value[key]):
            return False
    if "enabled" in value and not isinstance(value["enabled"], bool):
        return False
    if "tool_timeout_sec" in value and not _duration(value["tool_timeout_sec"]):
        return False
    variables = value.get("env_vars", [])
    return isinstance(variables, list) and all(_env_var_valid(item) for item in variables)


def _env_var_valid(item: object) -> bool:
    return isinstance(item, str) or (
        isinstance(item, dict)
        and not item.keys() - {"name", "source"}
        and isinstance(item.get("name"), str)
        and item.get("source", "local") in ("local", "remote")
    )


def _candidate(name: str, entry: _Entry) -> McpImportCandidate:
    value = entry.value
    stdio = "command" in value
    transport = "stdio" if stdio else "http"
    shown = {key: value[key] for key in ("command", "args", "env", "url") if key in value}
    omitted: set[str] = set()
    if not stdio:
        shown["headers"] = value.get("http_headers", {})
        shown["env_http_headers"] = value.get("env_http_headers", {})

    def unsupported(reason: UnsupportedReason, *, field: str = "", variable: str = "") -> McpImportCandidate:
        return McpImportCandidate(
            name,
            entry.scope,
            str(entry.path),
            None,
            unsupported=reason,
            entry=shown,
            transport=transport,
            scope_path=entry.folder,
            unsupported_field=field,
            unsupported_variable=variable,
        )

    variables = value.get("env_vars", [])
    if value.get("environment_id", "local") != "local" or any(
        isinstance(item, dict) and item.get("source") == "remote" for item in variables
    ):
        return unsupported(UnsupportedReason.REMOTE_ENVIRONMENT)
    warnings: list[ImportWarning] = []
    env = {key: render_template([literal]) for key, literal in value.get("env", {}).items()}
    for key in list(env):
        if key.upper() in _NON_INHERITABLE:
            del env[key]
            warnings.append(ImportWarning(ImportWarningKind.UNKNOWN_FIELDS_IGNORED, original=f"env.{key}"))
    braces_escaped = any(env[key] != literal for key, literal in value.get("env", {}).items() if key in env)
    forwarded = {item if isinstance(item, str) else item["name"]: "" for item in variables}
    inherited = strip_python_runtime_overrides(dict(forwarded))
    for variable in sorted(forwarded.keys() - inherited.keys()):
        if variable in env:
            continue
        env[variable] = render_template([TemplateVariable(variable)])
        warnings.append(ImportWarning(ImportWarningKind.OPTIONAL_VARIABLE, variable=variable, field=variable))
    if transport == "stdio" and (invalid_env := unsupported_env_value(env)) is not None:
        reason, field = invalid_env
        return unsupported(reason, field=f"mcp_servers.{name}.env.{field}")
    source_headers = normalize_codex_headers(
        value.get("http_headers", {}),
        value.get("env_http_headers", {}),
        bearer_authorization="bearer_token_env_var" in value,
    )
    if source_headers.conflicts:
        return unsupported(UnsupportedReason.HEADER_NAME_COLLISION, field=", ".join(source_headers.conflicts))
    warnings.extend(
        ImportWarning(
            ImportWarningKind.INVALID_HEADER_IGNORED,
            field=ignored_header.field,
            original="name" if ignored_header.reason is HeaderIgnoreReason.INVALID_NAME else "value",
        )
        for ignored_header in source_headers.ignored
    )
    headers = {key: render_template([literal]) for key, literal in source_headers.static_headers.items()}
    braces_escaped |= any(
        headers[key] != literal
        for key, literal in source_headers.static_headers.items()
        if key not in source_headers.env_headers
    )
    # Other env_vars are inherited. Python path overrides need explicit references because iCode
    # filters inherited ones; the warning explains that a reference requires a nonempty value.
    references = source_headers.env_headers
    reference_fields = {key.lower(): f"env_http_headers.{key}" for key in value.get("env_http_headers", {})}
    for key, variable in references.items():
        if not _VARIABLE.fullmatch(variable):
            return unsupported(UnsupportedReason.VARIABLE_NAME, field=reference_fields[key], variable=variable)
        headers[key] = render_template([TemplateVariable(variable)])
        warnings.append(ImportWarning(ImportWarningKind.OPTIONAL_VARIABLE, variable=variable, field=key))
    if "bearer_token_env_var" in value:
        variable = value["bearer_token_env_var"]
        if not _VARIABLE.fullmatch(variable):
            return unsupported(UnsupportedReason.VARIABLE_NAME, field="bearer_token_env_var", variable=variable)
        headers["authorization"] = render_template(["Bearer ", TemplateVariable(variable)])
    if not stdio:
        # Automatic OAuth is a Codex capability, not evidence that this server uses it.
        # Stored sign-in state is outside the local configuration import.
        auth = value.get("auth")
        if isinstance(auth, str) and auth in {"chatgpt", "ema_auth"}:
            warnings.append(ImportWarning(ImportWarningKind.AUTH_PROVIDER_IGNORED, original=auth))
        elif value.get("auth") == "oauth" or "oauth" in value or "oauth_resource" in value:
            warnings.append(ImportWarning(ImportWarningKind.OAUTH_IGNORED))
        if "auth" in value and auth not in ("oauth", "chatgpt", "ema_auth"):
            omitted.add("auth")
    if "startup_timeout_sec" in value:
        warnings.append(ImportWarning(ImportWarningKind.STARTUP_TIMEOUT_IGNORED))
    if braces_escaped:
        warnings.append(ImportWarning(ImportWarningKind.LITERAL_BRACES_ESCAPED))
    if "http_headers_helper" in value:
        # Helper arguments may contain credentials; expose the setting name, not the command.
        warnings.append(ImportWarning(ImportWarningKind.HEADERS_HELPER_IGNORED))
    if stdio and "cwd" in value:
        cwd = value["cwd"]
        if isinstance(cwd, str):
            if cwd:
                warnings.append(ImportWarning(ImportWarningKind.CWD_IGNORED, original=cwd))
        else:
            omitted.add("cwd")
    if entry.sources:
        warnings.append(ImportWarning(ImportWarningKind.MERGED, original=", ".join(entry.sources)))
    permission_fields, output_fields, ignored_tool_fields = _omitted_tool_fields(value)
    ignored = sorted(
        (value.keys() - _CARRIED_FIELDS - {"tools", "default_tools_approval_mode"}) | ignored_tool_fields | omitted
    )
    if ignored:
        warnings.append(ImportWarning(ImportWarningKind.UNKNOWN_FIELDS_IGNORED, original=", ".join(ignored)))
    if permission_fields:
        warnings.append(
            ImportWarning(ImportWarningKind.TOOL_PERMISSIONS_IGNORED, original=", ".join(sorted(permission_fields)))
        )
    if output_fields:
        warnings.append(
            ImportWarning(ImportWarningKind.TOOL_OUTPUT_LIMIT_IGNORED, original=", ".join(sorted(output_fields)))
        )
    allowed = value.get("enabled_tools")
    denied = value.get("disabled_tools", [])
    if allowed is not None:
        allowed = [tool for tool in allowed if tool not in denied]
    elif denied:
        allowed = []
        warnings.append(ImportWarning(ImportWarningKind.DENY_LIST, original=", ".join(denied)))
    timeout = value.get("tool_timeout_sec", 300)
    if timeout == 0:
        warnings.append(ImportWarning(ImportWarningKind.ZERO_TIMEOUT_IGNORED))
    request_timeout = request_timeout_seconds(timeout) if timeout else None
    server = MCPServerConfig(
        name=name,
        transport=transport,
        command=value.get("command", ""),
        args=value.get("args", []),
        env=env,
        url=value.get("url", ""),
        headers=headers,
        enabled=value.get("enabled", True),
        allowed_tools=allowed,
        request_timeout=request_timeout,
    )
    return McpImportCandidate(
        name,
        entry.scope,
        str(entry.path),
        server,
        warnings=tuple(warnings),
        transport=transport,
        scope_path=entry.folder,
        timeout_source_seconds=timeout if timeout and timeout != request_timeout else None,
    )


def _omitted_tool_fields(value: dict[str, Any]) -> tuple[set[str], set[str], set[str]]:
    permissions = {"default_tools_approval_mode"} if "default_tools_approval_mode" in value else set()
    output_limits: set[str] = set()
    ignored: set[str] = set()
    if "tools" in value:
        tools = value["tools"]
        if not isinstance(tools, dict):
            ignored.add("tools")
        else:
            for name, settings in tools.items():
                if not isinstance(settings, dict):
                    ignored.add(f"tools.{name}")
                    continue
                for key in settings:
                    field = f"tools.{name}.{key}"
                    if key == "approval_mode":
                        permissions.add(field)
                    elif key == "output_token_limit":
                        output_limits.add(field)
                    else:
                        ignored.add(field)
    return permissions, output_limits, ignored
