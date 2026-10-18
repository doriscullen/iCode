# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Import local Codex MCP configuration without starting Codex or its servers.

Baseline: https://github.com/openai/codex, tag ``rust-v0.163.0`` (0.163.0),
commit ``83086188ae0614bd882818ff1aa23f3194962012``. Source paths below are
relative to ``codex-rs`` at that commit; the release source is available at
https://github.com/openai/codex/tree/rust-v0.163.0/codex-rs.

Read ``$CODEX_HOME/config.toml`` (default ``~/.codex/config.toml``), then
``.codex/config.toml`` from the nearest project root down to the workspace.
The root uses ``project_root_markers`` (default ``[".git"]``); absent a
marker, only the workspace is searched. Project trust comes from the user
file, checking the layer's folder before the project root and Git root.
Untrusted and undecided project layers are shown locked, not merged
[config/src/loader/mod.rs:ProjectTrustContext,discover_project_layers]. Tables merge
recursively; arrays and scalars replace [config/src/merge.rs].

Transport inference and validation follow config/src/mcp_types.rs. Values
in ``env`` and ``http_headers`` are literal; named environment references
become iCode templates, never captured secrets. Explicit env beats env_vars;
environment headers beat static headers; bearer auth beats Authorization
[rmcp-client/src/utils.rs, codex-mcp/src/rmcp_client.rs]. Optional references
get a warning because iCode requires them to be set. The default tool timeout
is 300 seconds [codex-mcp/src/rmcp_client.rs:105]. Deny-only tool filters are
imported with no tools, since iCode cannot express a deny list without a catalog.

Not read: system/managed/cloud configuration, selected profile files,
session overrides, plugin servers, stored OAuth credentials, or main-checkout
trust for linked worktrees. Remote execution environments cannot be imported.
Ignored runtime settings are named in warnings. iCode's subprocess environment
inheritance differs from Codex's filtered environment. No commands, header
helpers, OAuth flows or network discovery run during import.
Explicit variables on Codex's non-inheritable list are omitted with a warning
[protocol/src/shell_environment.rs::NON_INHERITABLE_ENV_VARS].

When updating the baseline, re-check these files and their tests, especially
the raw config shape, transport validation, trust and merge rules.
"""

from __future__ import annotations

import math
import os
import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from chrys.foundation.platform import get_platform
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
    git_root,
    read_source_bytes,
    render_template,
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
_CARRIED_FIELDS = _STDIO_FIELDS | _HTTP_FIELDS | {"enabled", "tool_timeout_sec", "enabled_tools", "disabled_tools"}
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
    """Reads Codex's user file and trusted project layers for the workspace."""

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
        return any((folder / ".codex/config.toml").exists() for folder, _trusted in _project_folders(ctx, {}))

    def discover(self, ctx: ImportContext) -> McpImportResult:
        errors: list[McpSourceError] = []
        entries: dict[str, _Entry] = {}
        locked: list[tuple[str, _Entry]] = []
        home = _codex_home(ctx)
        if home is None:
            return McpImportResult()
        user_path = home / "config.toml"
        user = _read(user_path, errors)
        _add_layer(entries, user, user_path, "user", "", errors)
        for folder, trusted in _project_folders(ctx, user):
            path = folder / ".codex/config.toml"
            if os.path.realpath(path) == os.path.realpath(user_path):
                continue
            document = _read(path, errors)
            if trusted:
                _add_layer(entries, document, path, "project", str(folder), errors)
            else:
                isolated: dict[str, _Entry] = {}
                _add_layer(isolated, document, path, "project", str(folder), errors)
                locked.extend(isolated.items())
        candidates = [_candidate(name, entry) for name, entry in entries.items() if _valid(name, entry.value)]
        candidates.extend(
            _candidate(name, entry, untrusted=True) for name, entry in locked if _valid(name, entry.value)
        )
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
    except UnicodeDecodeError, ValueError, RecursionError:
        errors.append(McpSourceError(str(path), SourceErrorKind.INVALID_TOML))
    return {}


def _project_folders(ctx: ImportContext, user: dict[str, Any]) -> list[tuple[Path, bool]]:
    if not ctx.workspace_cwd:
        return []
    workspace = Path(os.path.abspath(ctx.workspace_cwd))
    markers = user.get("project_root_markers", [".git"])
    if not _strings(markers):
        return []
    ancestors = (workspace, *workspace.parents)
    root = next((folder for folder in ancestors if any(_has_marker(folder, marker) for marker in markers)), workspace)
    repository = git_root(str(workspace))
    projects = user.get("projects", {})
    trust = (
        {
            os.path.normcase(key): value.get("trust_level")
            for key, value in projects.items()
            if isinstance(value, dict) and value.get("trust_level") in ("trusted", "untrusted")
        }
        if isinstance(projects, dict)
        else {}
    )
    result: list[tuple[Path, bool]] = []
    for folder in ancestors:
        paths = [str(folder), str(root)] + ([repository] if repository else [])
        keys = [key for path in paths for key in (os.path.realpath(path), path)]
        decision = next((trust[os.path.normcase(key)] for key in keys if os.path.normcase(key) in trust), None)
        result.append((folder, decision == "trusted"))
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
        errors.append(McpSourceError(str(path), SourceErrorKind.INVALID_CONFIG))
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
    for key in (
        "command",
        "url",
        "cwd",
        "bearer_token_env_var",
        "http_headers_helper",
        "environment_id",
        "oauth_resource",
        "name",
    ):
        if key in value and not isinstance(value[key], str):
            return False
    for key in ("args", "enabled_tools", "disabled_tools", "scopes", "omit_tools_from"):
        if key in value and not _strings(value[key]):
            return False
    if any(surface not in ("code_mode", "deferred", "direct") for surface in value.get("omit_tools_from", [])):
        return False
    for key in ("env", "http_headers", "env_http_headers"):
        if key in value and not _string_map(value[key]):
            return False
    for key in ("enabled", "required", "supports_parallel_tool_calls"):
        if key in value and not isinstance(value[key], bool):
            return False
    for key in ("tool_timeout_sec", "startup_timeout_sec"):
        if key in value and not _duration(value[key]):
            return False
    if "startup_timeout_ms" in value and (
        type(value["startup_timeout_ms"]) is not int or not 0 <= value["startup_timeout_ms"] < 2**64
    ):
        return False
    if "tool_input_schema_max_bytes" in value and (
        type(value["tool_input_schema_max_bytes"]) is not int or value["tool_input_schema_max_bytes"] <= 0
    ):
        return False
    if value.get("auth", "oauth") not in ("oauth", "chatgpt", "ema_auth"):
        return False
    if value.get("startup_readiness", "connection") not in ("connection", "catalog"):
        return False
    if value.get("default_tools_approval_mode", "auto") not in ("auto", "prompt", "writes", "approve"):
        return False
    helper = value.get("http_headers_helper")
    if helper is not None and (not helper.strip() or value.get("environment_id", "local") != "local"):
        return False
    variables = value.get("env_vars", [])
    if not isinstance(variables, list) or not all(_env_var_valid(item) for item in variables):
        return False
    return _oauth_valid(value) and _tools_valid(value.get("tools", {}))


def _env_var_valid(item: object) -> bool:
    return isinstance(item, str) or (
        isinstance(item, dict)
        and not item.keys() - {"name", "source"}
        and isinstance(item.get("name"), str)
        and item.get("source", "local") in ("local", "remote")
    )


def _oauth_valid(value: dict[str, Any]) -> bool:
    oauth = value.get("oauth", {})
    if not isinstance(oauth, dict):
        return False
    for key in ("client_id", "client_secret", "callback_url", "authorization_server_issuer"):
        if key in oauth and not isinstance(oauth[key], str):
            return False
    if "callback_port" in oauth and (
        type(oauth["callback_port"]) is not int or not 0 <= oauth["callback_port"] <= 65535
    ):
        return False
    if "client_secret" in oauth and (not oauth["client_secret"].strip() or not oauth.get("client_id", "").strip()):
        return False
    return "authorization_server_issuer" not in oauth or value.get("auth") == "ema_auth"


def _tools_valid(tools: object) -> bool:
    if not isinstance(tools, dict):
        return False
    for item in tools.values():
        if not isinstance(item, dict) or item.get("approval_mode", "auto") not in (
            "auto",
            "prompt",
            "writes",
            "approve",
        ):
            return False
        limit = item.get("output_token_limit", 1)
        if type(limit) is not int or limit <= 0:
            return False
    return True


def _candidate(name: str, entry: _Entry, *, untrusted: bool = False) -> McpImportCandidate:
    value = entry.value
    stdio = "command" in value
    transport = "stdio" if stdio else "http"
    shown = {key: value[key] for key in ("command", "args", "env", "url") if key in value}
    if not stdio:
        shown["headers"] = value.get("http_headers", {})

    def unsupported(reason: UnsupportedReason) -> McpImportCandidate:
        return McpImportCandidate(
            name,
            entry.scope,
            str(entry.path),
            None,
            unsupported=reason,
            entry=shown,
            transport=transport,
            scope_path=entry.folder,
        )

    if untrusted:
        return unsupported(UnsupportedReason.UNTRUSTED_PROJECT)
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
    headers = {key.lower(): render_template([literal]) for key, literal in value.get("http_headers", {}).items()}
    references = {
        item if isinstance(item, str) else item["name"]: item if isinstance(item, str) else item["name"]
        for item in variables
    }
    if not stdio:
        references = {key.lower(): variable for key, variable in value.get("env_http_headers", {}).items()}
    target = env if stdio else headers
    for key, variable in references.items():
        if stdio:
            if key.upper() in _NON_INHERITABLE:
                warnings.append(ImportWarning(ImportWarningKind.UNKNOWN_FIELDS_IGNORED, original=f"env_vars.{key}"))
                continue
            if key in env or (get_platform().is_windows and any(key.lower() == name.lower() for name in env)):
                continue
        if not stdio and key == "authorization" and "bearer_token_env_var" in value:
            continue
        if not _VARIABLE.fullmatch(variable):
            return unsupported(UnsupportedReason.VARIABLE_NAME)
        target[key] = render_template([TemplateVariable(variable)])
        warnings.append(ImportWarning(ImportWarningKind.OPTIONAL_VARIABLE, variable=variable, field=key))
    if "bearer_token_env_var" in value:
        variable = value["bearer_token_env_var"]
        if not _VARIABLE.fullmatch(variable):
            return unsupported(UnsupportedReason.VARIABLE_NAME)
        headers["authorization"] = render_template(["Bearer ", TemplateVariable(variable)])
    if not stdio:
        warnings.append(ImportWarning(ImportWarningKind.OAUTH_IGNORED))
    if value.get("auth", "oauth") != "oauth":
        warnings.append(ImportWarning(ImportWarningKind.AUTH_PROVIDER_IGNORED, original=value["auth"]))
    if "http_headers_helper" in value:
        # Helper arguments may contain credentials; expose the setting name, not the command.
        warnings.append(ImportWarning(ImportWarningKind.UNKNOWN_FIELDS_IGNORED, original="http_headers_helper"))
    if stdio and value.get("cwd"):
        warnings.append(ImportWarning(ImportWarningKind.CWD_IGNORED, original=value["cwd"]))
    if not value.get("enabled", True):
        warnings.append(ImportWarning(ImportWarningKind.DISABLED))
    if entry.sources:
        warnings.append(ImportWarning(ImportWarningKind.MERGED, original=", ".join(entry.sources)))
    ignored = sorted(value.keys() - _CARRIED_FIELDS - {"tools"})
    if ignored:
        warnings.append(ImportWarning(ImportWarningKind.UNKNOWN_FIELDS_IGNORED, original=", ".join(ignored)))
    if value.get("tools"):
        warnings.append(ImportWarning(ImportWarningKind.TOOL_RULES_IGNORED, original=", ".join(value["tools"])))
    allowed = value.get("enabled_tools")
    denied = value.get("disabled_tools", [])
    if allowed is not None:
        allowed = [tool for tool in allowed if tool not in denied]
    elif denied:
        allowed = []
        warnings.append(ImportWarning(ImportWarningKind.DENY_LIST, original=", ".join(denied)))
    timeout = value.get("tool_timeout_sec", 300)
    if timeout == 0:
        warnings.append(ImportWarning(ImportWarningKind.UNKNOWN_FIELDS_IGNORED, original="tool_timeout_sec = 0"))
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
        request_timeout=math.ceil(timeout) if timeout else None,
    )
    return McpImportCandidate(
        name,
        entry.scope,
        str(entry.path),
        server,
        warnings=tuple(warnings),
        transport=transport,
        scope_path=entry.folder,
    )
