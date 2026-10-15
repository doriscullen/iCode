# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Import MCP servers configured in Claude Code.

Baseline: Claude Code 2.1.296, checked 2026-10-13 against
code.claude.com/docs/en/{mcp,managed-mcp,settings-reference}. Each rule is
tagged with its evidence: ``[doc: page, "section"]``, ``[observed 2.1.296]``
(behavior the docs leave out, seen by running that release) or
``[binary 2.1.296]`` (read from that release's config schema; internal).

What is read [doc: mcp, "MCP installation scopes"]:

- ``user``: the top-level ``mcpServers`` of ``.claude.json``, which Claude Code
  uses in every project, as iCode uses every server.
- ``local``: ``projects[<folder>].mcpServers`` of ``.claude.json``, which
  Claude Code uses in that folder alone [doc: mcp, "Local scope"]. It reads
  one key only: the Git repository root when the workspace is inside one,
  else the workspace itself; a key for a folder inside the repository, or
  above a workspace outside one, is never read [observed 2.1.296].
- ``project``: ``mcpServers`` of the ``.mcp.json`` in the workspace and in
  every folder above it, past the repository root, which Claude Code all reads
  [doc: mcp, "Project scope"; observed 2.1.296]. Tools that install a server
  "just for this project" write it there (codegraph does).

``.claude.json`` sits in the home directory, or in ``$CLAUDE_CONFIG_DIR`` when
that is non-empty; a relative or blank value is a path under the folder Claude
Code starts in [observed 2.1.296], for which the workspace stands in here. A
``.claude.json`` inside a project is not read: Claude Code never reads one.

Local and project candidates carry their folder, so the user sees that a
server Claude Code uses in one folder becomes global once imported. Only
servers Claude Code itself loads are listed: an entry it skips (a ``url`` with
no ``type``, ``type: "sdk"``, an unknown type, a missing or mistyped
``command``, ``args``, ``env``, ``url`` or ``headers``) is left out
[doc: mcp, "Option 1: Add a remote HTTP server"; binary 2.1.296]. Servers it
loads that iCode can't run are listed with the reason.

Runtime differences an imported server may notice:

- Working folder: iCode starts stdio servers in the workspace folder, so a
  server that finds its project from its working folder, as codegraph does,
  keeps working.
- ``CLAUDE_PROJECT_DIR``: Claude Code sets it in a stdio server's environment
  [doc: mcp, "Option 3: Add a local stdio server"]; iCode doesn't. A value
  referencing it gets a warning; a server reading it itself can't be detected.
- ``roots/list``: Claude Code answers it with the session's folders
  [doc: mcp, "Option 3: Add a local stdio server"]; iCode doesn't advertise
  roots, so a server asking falls back to its working folder or fails.
- A tool's ``_meta["anthropic/alwaysLoad"]`` [doc: mcp, "Scale with MCP tool
  search"] means nothing to iCode, which loads tools by its own strategy.

Deliberately not reproduced: which of several same-named servers Claude Code
loads, its approval and disabled-server lists, ``managed-mcp.json`` and
``managedMcpServers``, plugin servers and claude.ai connectors. The user picks
what to import.

When Claude Code updates: read the entries of CHANGELOG.md in
anthropics/claude-code after the baseline that mention MCP, ``.mcp.json`` or
``.claude.json``; for each, re-check the rules here tagged with that topic
against the cited doc section (an ``[observed]`` rule needs a re-run with
``claude mcp add-json`` and ``claude mcp get`` under a temporary ``HOME`` and
``CLAUDE_CONFIG_DIR``); then update the code, the tests and the baseline above.
"""

from __future__ import annotations

import json
import math
import os
from dataclasses import replace
from pathlib import Path
from typing import TYPE_CHECKING, Any

from chrys.service.mcp.importers.base import (
    ImportContext,
    ImportWarning,
    ImportWarningKind,
    McpImportCandidate,
    McpImportResult,
    McpSourceError,
    SourceReadError,
    UnsupportedReason,
    convert_dollar_templates,
    dollar_template_variables,
    dollar_templates_convertible,
    git_root,
    has_dollar_template,
    read_json_object,
)
from chrys.service.profiles.agents.schema import MCPServerConfig

if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping

SCOPE_LOCAL = "local"
SCOPE_PROJECT = "project"
SCOPE_USER = "user"

_CONFIG_FILE_NAME = ".claude.json"
_PROJECT_FILE_NAME = ".mcp.json"

# The MCP specification's name for the HTTP transport, accepted in JSON as an alias
# [doc: mcp, "Option 1: Add a remote HTTP server"].
_HTTP_ALIASES = frozenset({"http", "streamable-http"})

# Remote transports Claude Code runs and iCode doesn't: HTTP+SSE, deprecated, and WebSocket
# [doc: mcp, "Option 2: Add a remote SSE server", "Option 4: Add a remote WebSocket server"].
_UNSUPPORTED_REMOTE_TRANSPORTS = frozenset({"sse", "ws"})

# The per-server ``timeout`` is in milliseconds and values below one second are ignored
# [doc: mcp, tips under "Push messages with channels"].
_MIN_TIMEOUT_MS = 1000

# A remote server's ``request_timeout_ms`` becomes its ``timeout`` when it sets none,
# capped at five minutes; any other value is dropped [binary 2.1.296, internal].
_MAX_REQUEST_TIMEOUT_MS = 300_000

# Fields Claude Code reads for each transport [doc: mcp, "Option 1: Add a remote HTTP
# server", "Option 3: Add a local stdio server", "Exempt a server from deferral"], plus
# ``request_timeout_ms`` [binary 2.1.296]. ``alwaysLoad`` only changes when Claude Code
# loads a server's tools, which iCode does upfront by default, so it is read and
# dropped without a note; anything else unknown is reported.
_STDIO_FIELDS = frozenset({"type", "command", "args", "env", "timeout", "alwaysLoad"})
_HTTP_FIELDS = frozenset(
    {"type", "url", "headers", "headersHelper", "oauth", "timeout", "request_timeout_ms", "alwaysLoad"}
)

# Variables Claude Code reads as empty in a remote server's url and headers, so a
# project file or plugin can't send the user's own credentials to a server it
# names [doc: mcp, "Credential variables that read as empty"]. The documented list
# names examples; these are the ones it spells out. iCode has no such rule and would
# send the real value.
CREDENTIAL_VARIABLES = frozenset(
    {"ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "AWS_BEARER_TOKEN_BEDROCK", "HTTPS_PROXY", "NPM_TOKEN"}
)

# Set by Claude Code in the environment of the stdio servers it starts, never by iCode
# [doc: mcp, "Option 3: Add a local stdio server"].
CLAUDE_PROJECT_DIR = "CLAUDE_PROJECT_DIR"


class ClaudeCodeSource:
    """Reads Claude Code's user servers and the workspace's local and project ones."""

    @property
    def source_id(self) -> str:
        return "claude_code"

    @property
    def display_name(self) -> str:
        return "Claude Code"

    def detect(self, ctx: ImportContext) -> bool:
        config_path = _config_file(ctx)
        if config_path is not None and config_path.is_file():
            return True
        return any(path.is_file() for path in _project_files(ctx.workspace_cwd))

    def discover(self, ctx: ImportContext) -> McpImportResult:
        collector = _Collector()
        config_path = _config_file(ctx)
        config = collector.read(config_path) if config_path is not None else None
        if config is not None and config_path is not None and ctx.workspace_cwd:
            local = _local_project(config, _project_folder(ctx.workspace_cwd))
            if local is not None:
                folder, servers = local
                collector.add_servers(servers, SCOPE_LOCAL, config_path, scope_path=folder)
        for project_path in _project_files(ctx.workspace_cwd):
            project = collector.read(project_path)
            if project is not None:
                collector.add_servers(
                    project.get("mcpServers"), SCOPE_PROJECT, project_path, scope_path=str(project_path.parent)
                )
        if config is not None and config_path is not None:
            collector.add_servers(config.get("mcpServers"), SCOPE_USER, config_path)
        return collector.result()


def _config_file(ctx: ImportContext) -> Path | None:
    """Where Claude Code keeps ``.claude.json``; None when that depends on a folder iCode can't know."""
    override = ctx.environ.get("CLAUDE_CONFIG_DIR", "")
    if not override:
        return ctx.home / _CONFIG_FILE_NAME
    base = Path(override).expanduser()
    if not base.is_absolute():
        # Claude Code resolves it against the folder it starts in, which the workspace stands in for.
        if not ctx.workspace_cwd:
            return None
        base = Path(ctx.workspace_cwd) / base
    return base / _CONFIG_FILE_NAME


def _project_files(workspace_cwd: str | None) -> list[Path]:
    """The ``.mcp.json`` Claude Code reads for the workspace: its own, then each folder's above it."""
    if not workspace_cwd:
        return []
    try:
        folder = Path(os.path.realpath(workspace_cwd))
    except OSError, ValueError:
        return []
    return [candidate / _PROJECT_FILE_NAME for candidate in (folder, *folder.parents)]


def _local_project(config: Mapping[str, Any], folder: str) -> tuple[str, dict[str, Any]] | None:
    """The ``projects`` key Claude Code uses for *folder* and that project's servers."""
    projects = config.get("projects")
    if not isinstance(projects, dict):
        return None
    entries = [(key, project) for key, project in projects.items() if key and isinstance(project, dict)]
    match = _project_entry(entries, folder)
    if match is None:
        return None
    key, project = match
    servers = project.get("mcpServers")
    return (key, servers) if isinstance(servers, dict) and servers else None


def _project_folder(workspace_cwd: str) -> str:
    """The folder Claude Code keys the workspace's local servers by: its Git repository root, else itself."""
    root = git_root(workspace_cwd)
    return workspace_cwd if root is None else root


def _project_entry(entries: list[tuple[str, dict[str, Any]]], folder: str) -> tuple[str, dict[str, Any]] | None:
    # Claude Code writes the key as it spells the folder, so an exact match usually ends the
    # search without touching the filesystem (the map may hold hundreds of keys, some on slow
    # or missing mounts). Then a key spelled like the folder wins; only then ask the filesystem,
    # which alone knows whether a key differing in case (or reached through another link)
    # names the same folder.
    for key, project in entries:
        if key == folder:
            return key, project
    wanted = _path_key(folder)
    if wanted is not None:
        for key, project in entries:
            if _path_key(key) == wanted:
                return key, project
    for key, project in entries:
        if _same_directory(key, folder):
            return key, project
    return None


def _path_key(path: str) -> str | None:
    try:
        return os.path.normcase(os.path.realpath(path))
    except OSError, ValueError:
        return None


def _same_directory(path: str, other: str) -> bool:
    try:
        return os.path.samefile(path, other)
    except OSError, ValueError:
        # A project folder deleted since Claude Code recorded it, or a key that isn't a path.
        return False


class _Collector:
    def __init__(self) -> None:
        self._candidates: list[McpImportCandidate] = []
        self._errors: list[McpSourceError] = []

    def read(self, path: Path) -> dict[str, Any] | None:
        try:
            return read_json_object(path)
        except SourceReadError as exc:
            self._errors.append(McpSourceError(str(path), exc.kind))
            return None

    def add_servers(self, servers: object, scope: str, path: Path, *, scope_path: str = "") -> None:
        if not isinstance(servers, dict):
            return
        for name, entry in servers.items():
            candidate = _candidate(name, entry, scope, str(path))
            if candidate is None:
                continue
            self._candidates.append(replace(candidate, scope_path=scope_path) if scope_path else candidate)

    def result(self) -> McpImportResult:
        return McpImportResult(
            candidates=tuple(self._candidates),
            source_errors=tuple(self._errors),
        )


def _candidate(name: str, entry: object, scope: str, source_path: str) -> McpImportCandidate | None:
    """The server as iCode would import it; None for an entry Claude Code itself skips."""
    if not isinstance(entry, dict) or not name.strip():
        return None
    transport = _transport(entry)
    if transport == "stdio":
        return _stdio_candidate(name, entry, scope, source_path)
    if transport == "http":
        return _http_candidate(name, entry, scope, source_path)
    if transport in _UNSUPPORTED_REMOTE_TRANSPORTS and _remote_shape_is_valid(entry):
        return _unsupported(name, entry, scope, source_path, UnsupportedReason.UNSUPPORTED_TRANSPORT)
    # A url with no type (read as stdio, so a configuration error), "sdk" (only an SDK host
    # registers one) and unknown types: Claude Code skips them all
    # [doc: mcp, "Option 1: Add a remote HTTP server"].
    return None


def _transport(entry: dict[str, Any]) -> str:
    """The transport Claude Code would use: declared, stdio when none is declared, empty when unusable."""
    declared = entry.get("type")
    if declared is None:
        return "" if "url" in entry else "stdio"
    if not isinstance(declared, str):
        return ""
    return "http" if declared in _HTTP_ALIASES else declared


def _remote_shape_is_valid(entry: dict[str, Any]) -> bool:
    """Whether Claude Code would connect the remote entry: a non-empty url and string headers [binary 2.1.296]."""
    url = entry.get("url")
    return isinstance(url, str) and bool(url.strip()) and _is_str_mapping(entry.get("headers", {}))


def _unsupported(
    name: str, entry: dict[str, Any], scope: str, source_path: str, reason: UnsupportedReason
) -> McpImportCandidate:
    return McpImportCandidate(
        name, scope, source_path, None, unsupported=reason, entry=entry, transport=_transport(entry)
    )


def _stdio_candidate(name: str, entry: dict[str, Any], scope: str, source_path: str) -> McpImportCandidate | None:
    command = entry.get("command")
    args = entry.get("args", [])
    env = entry.get("env", {})
    if not isinstance(command, str) or not command.strip() or not _is_str_list(args) or not _is_str_mapping(env):
        return None
    if has_dollar_template(command) or any(has_dollar_template(arg) for arg in args):
        return _unsupported(name, entry, scope, source_path, UnsupportedReason.TEMPLATE_IN_COMMAND)
    if not _convertible(env):
        return _unsupported(name, entry, scope, source_path, UnsupportedReason.TEMPLATE_NEXT_TO_BRACE)
    converted_env, warnings = _convert_mapping(env)
    warnings = (*warnings, *_project_dir_warnings(env), *_unknown_fields(entry, _STDIO_FIELDS))
    config = MCPServerConfig(
        name=name,
        transport="stdio",
        command=command,
        args=list(args),
        env=converted_env,
        request_timeout=_request_timeout(entry, remote=False),
    )
    return McpImportCandidate(name, scope, source_path, config, warnings=warnings, transport="stdio")


def _http_candidate(name: str, entry: dict[str, Any], scope: str, source_path: str) -> McpImportCandidate | None:
    if not _remote_shape_is_valid(entry):
        return None
    url: str = entry["url"]
    headers: dict[str, str] = entry.get("headers", {})
    if has_dollar_template(url):
        return _unsupported(name, entry, scope, source_path, UnsupportedReason.TEMPLATE_IN_URL)
    if not _convertible(headers):
        return _unsupported(name, entry, scope, source_path, UnsupportedReason.TEMPLATE_NEXT_TO_BRACE)
    converted_headers, warnings = _convert_mapping(headers)
    warnings = (*warnings, *_credential_warnings(headers), *_project_dir_warnings(headers))
    if "headersHelper" in entry:
        helper = entry["headersHelper"]
        command = helper if isinstance(helper, str) else json.dumps(helper)
        warnings = (*warnings, ImportWarning(ImportWarningKind.HEADERS_HELPER_IGNORED, original=command))
    if "oauth" in entry:
        warnings = (*warnings, ImportWarning(ImportWarningKind.OAUTH_IGNORED))
    warnings = (*warnings, *_unknown_fields(entry, _HTTP_FIELDS))
    config = MCPServerConfig(
        name=name,
        transport="http",
        url=url,
        headers=converted_headers,
        request_timeout=_request_timeout(entry, remote=True),
    )
    return McpImportCandidate(name, scope, source_path, config, warnings=warnings, transport="http")


def _convertible(values: dict[str, str]) -> bool:
    return all(dollar_templates_convertible(value) for value in values.values())


def _convert_mapping(values: dict[str, str]) -> tuple[dict[str, str], tuple[ImportWarning, ...]]:
    # Warnings name the setting, never its value: they are saved in the description, and the value may be a secret.
    converted: dict[str, str] = {}
    warnings: list[ImportWarning] = []
    for key, value in values.items():
        converted[key], value_warnings = convert_dollar_templates(value)
        warnings.extend(replace(warning, field=key) for warning in value_warnings)
    return converted, tuple(warnings)


def _credential_warnings(headers: dict[str, str]) -> Iterable[ImportWarning]:
    """One warning per header that references a variable Claude Code never sends to a remote server."""
    for key, value in headers.items():
        for variable in dollar_template_variables(value):
            if variable in CREDENTIAL_VARIABLES:
                yield ImportWarning(ImportWarningKind.CREDENTIAL_VARIABLE, variable, field=key)


def _project_dir_warnings(values: dict[str, str]) -> Iterable[ImportWarning]:
    """One warning per setting that references ``CLAUDE_PROJECT_DIR``, which iCode never sets."""
    for key, value in values.items():
        if CLAUDE_PROJECT_DIR in dollar_template_variables(value):
            yield ImportWarning(ImportWarningKind.CLAUDE_PROJECT_DIR, CLAUDE_PROJECT_DIR, field=key)


def _unknown_fields(entry: dict[str, Any], known: frozenset[str]) -> Iterable[ImportWarning]:
    unknown = [key for key in entry if key not in known]
    if unknown:
        yield ImportWarning(ImportWarningKind.UNKNOWN_FIELDS_IGNORED, original=", ".join(unknown))


def _request_timeout(entry: dict[str, Any], *, remote: bool) -> int | None:
    """Claude Code's per-call ``timeout`` in milliseconds as iCode's request timeout in seconds.

    A remote server that sets no ``timeout`` takes its ``request_timeout_ms`` instead.
    """
    if remote and "timeout" not in entry:
        timeout = _folded_request_timeout(entry.get("request_timeout_ms"))
    else:
        timeout = entry.get("timeout")
    if isinstance(timeout, bool) or not isinstance(timeout, int | float):
        return None
    if not math.isfinite(timeout) or timeout < _MIN_TIMEOUT_MS:
        return None
    return math.ceil(timeout / 1000)


def _folded_request_timeout(value: object) -> int | None:
    """``request_timeout_ms`` as the ``timeout`` Claude Code reads it as: a positive whole number, capped."""
    if isinstance(value, bool) or not isinstance(value, int | float) or not math.isfinite(value):
        return None
    if value <= 0 or value != int(value):
        return None
    return min(int(value), _MAX_REQUEST_TIMEOUT_MS)


def _is_str_list(value: object) -> bool:
    return isinstance(value, list) and all(isinstance(item, str) for item in value)


def _is_str_mapping(value: object) -> bool:
    return isinstance(value, dict) and all(isinstance(item, str) for item in value.values())
