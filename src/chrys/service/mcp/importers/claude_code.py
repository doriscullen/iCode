# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Import MCP servers configured in Claude Code.

Baseline: Claude Code 2.1.302, checked 2026-10-19 against
code.claude.com/docs/en/{mcp,managed-mcp,settings-reference}. Each rule is
tagged with its evidence: ``[doc: page, "section"]``, ``[observed 2.1.302]``
(behavior the docs leave out, seen by running that release) or
``[binary 2.1.302]`` (read from that release's config schema; internal).

What is read [doc: mcp, "MCP installation scopes"]:

- ``user``: the top-level ``mcpServers`` of ``.claude.json``, which Claude Code
  uses in every project, as iCode uses every server.
- ``local``: ``projects[<folder>].mcpServers`` of ``.claude.json``, which
  Claude Code uses in that folder alone [doc: mcp, "Local scope"]. It reads
  one key only: the Git repository root when the workspace is inside one,
  else the workspace itself; a key for a folder inside the repository, or
  above a workspace outside one, is never read [observed 2.1.302].
- ``project``: ``mcpServers`` of the ``.mcp.json`` in the workspace and in
  every folder above it, past the repository root but not in the filesystem
  root, which Claude Code all reads [doc: mcp, "Project scope"; observed
  2.1.302; binary 2.1.302]. A blank file holds no servers [observed 2.1.302].
  Tools that install a server "just for this project" write it there
  (codegraph does).

User and local servers come from Claude Code's global config: ``.claude.json``
in the home directory, or in ``$CLAUDE_CONFIG_DIR`` when that is non-empty
[observed 2.1.302]. A legacy ``.config.json`` in the config folder
(``$CLAUDE_CONFIG_DIR`` whenever it is set, even empty, else ``~/.claude``)
replaces it when it is a file that holds something, or an empty writable one
while ``.claude.json`` is missing [observed 2.1.302; binary 2.1.302]. A relative
or blank folder is a path under the folder Claude Code starts in, for which the
workspace stands in here. A ``.claude.json`` inside a project is not read:
Claude Code never reads one.

Local and project candidates carry their folder, so the user sees that a
server Claude Code uses in one folder becomes global once imported. Only
servers Claude Code itself loads are listed: an entry it skips (a ``url`` with
no ``type``, ``type: "sdk"``, an unknown type, a missing or mistyped setting,
or a name it keeps for its own servers) is left out [doc: mcp, "Option 1: Add a
remote HTTP server", "Configuration warnings"; binary 2.1.302; observed
2.1.302]. Servers it loads that iCode can't run are listed with the reason.

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
``managedMcpServers``, plugin servers and claude.ai connectors, and the
``.claude-custom-oauth.json`` it reads instead of ``.claude.json`` when
``CLAUDE_CODE_CUSTOM_OAUTH_URL`` points it at another sign-in server. The user
picks what to import.

When Claude Code updates: read the entries of CHANGELOG.md in
anthropics/claude-code after the baseline that mention MCP, ``.mcp.json`` or
``.claude.json``; for each, re-check the rules here tagged with that topic
against the cited doc section (an ``[observed]`` rule needs a re-run of
``claude mcp list`` under a temporary ``HOME`` and ``CLAUDE_CONFIG_DIR``, which
lists every server Claude Code loads and names each one it skips, with the
reason); then update the code, the tests and the baseline above.
"""

from __future__ import annotations

import math
import os
import re
import stat
from dataclasses import replace
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.parse import urlsplit

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
    decode_source,
    dollar_template_variables,
    dollar_templates_convertible,
    git_root,
    has_dollar_template,
    parse_json_object,
    read_source_bytes,
)
from chrys.service.profiles.agents.schema import MCPServerConfig

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable, Mapping

SCOPE_LOCAL = "local"
SCOPE_PROJECT = "project"
SCOPE_USER = "user"

_CONFIG_FILE_NAME = ".claude.json"
_LEGACY_CONFIG_FILE_NAME = ".config.json"
_PROJECT_FILE_NAME = ".mcp.json"

# What JavaScript's ``String.prototype.trim`` removes: a ``.mcp.json`` holding only these
# holds no servers [observed 2.1.302].
_JS_WHITESPACE = " \t\n\v\f\r\u00a0\u1680\u2000\u2001\u2002\u2003\u2004\u2005\u2006\u2007\u2008\u2009\u200a\u2028\u2029\u202f\u205f\u3000\ufeff"

# Names Claude Code keeps for its own servers and skips in any config [doc: mcp,
# "Configuration warnings"; observed 2.1.302]. They are matched against the name as
# Claude Code writes it in tool names, ``mcp__<name>__<tool>``: every character other
# than a letter, digit, ``-`` or ``_`` becomes ``_``, and the name ends at the first
# ``__`` [binary 2.1.302]. Claude Code keeps a few more names in its hosted sessions only.
_RESERVED_SERVER_NAMES = frozenset(
    {
        "Claude_Browser",
        "Claude_Preview",
        "Claude_in_Chrome",
        "claude-code-remote",
        "claude-device",
        "claude-in-chrome",
        "claude_ai",
        "computer-use",
        "hearthbot",
        "remote-devices",
        "webagent",
        "workspace",
    }
)
# Reserved as written: a name Claude Code's Slack integration uses, and the members of
# JavaScript's ``Object.prototype`` whose tool-name form starts with ``__`` [observed 2.1.302].
_RESERVED_LITERAL_NAMES = frozenset(
    {
        "Slack sign-in (Claude Code tag)",
        "__defineGetter__",
        "__defineSetter__",
        "__lookupGetter__",
        "__lookupSetter__",
        "__proto__",
    }
)
_TOOL_NAME_UNSAFE_RE = re.compile(r"[^A-Za-z0-9_-]")
# Names Claude Code's connectors use are rewritten further [binary 2.1.302].
_CONNECTOR_NAME_PREFIX = "claude.ai "

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
# capped at five minutes; any other value is dropped [binary 2.1.302, internal].
_MAX_REQUEST_TIMEOUT_MS = 300_000

# Fields Claude Code reads for each transport [doc: mcp, "Option 1: Add a remote HTTP
# server", "Option 3: Add a local stdio server", "Exempt a server from deferral"], plus
# ``request_timeout_ms`` and the internal ``bareElicitationCapability``,
# ``discoveryCache`` and ``role`` [binary 2.1.302]. ``alwaysLoad`` only changes when
# Claude Code loads a server's tools, which iCode does upfront by default, and the
# internal ones only how Claude Code itself talks to the server, so they are read and
# dropped without a note; anything else unknown is reported.
_STDIO_FIELDS = frozenset(
    {"type", "command", "args", "env", "timeout", "alwaysLoad", "bareElicitationCapability", "role"}
)
_HTTP_FIELDS = frozenset(
    {
        "type",
        "url",
        "headers",
        "headersHelper",
        "oauth",
        "timeout",
        "request_timeout_ms",
        "alwaysLoad",
        "bareElicitationCapability",
        "discoveryCache",
        "role",
    }
)

# A setting of the wrong type makes Claude Code skip the whole entry; unknown fields
# don't [binary 2.1.302; observed 2.1.302]. ``timeout`` is a positive whole number of
# milliseconds a JavaScript number holds exactly.
_MAX_SAFE_INTEGER = 2**53 - 1
_TOOL_POLICIES = frozenset({"always_allow", "always_ask", "always_deny"})
_TOOL_PERMISSIONS = frozenset({"allow", "ask", "blocked"})
# Characters a URL's host can't hold once parsed; ``:`` is left out because only an
# IPv6 host keeps one once the port is split off, and ``%`` because the parser decodes it.
_FORBIDDEN_HOST_RE = re.compile(r"[\x00-\x20#/<>?@\[\]^|\x7f]")
_URL_DROPPED_RE = re.compile(r"[\t\n\r]")
_C0_AND_SPACE = "".join(chr(code) for code in range(0x21))

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
            project = collector.read(project_path, blank_holds_nothing=True)
            if project is not None:
                collector.add_servers(
                    project.get("mcpServers"), SCOPE_PROJECT, project_path, scope_path=str(project_path.parent)
                )
        if config is not None and config_path is not None:
            collector.add_servers(config.get("mcpServers"), SCOPE_USER, config_path)
        return collector.result()


def _config_file(ctx: ImportContext) -> Path | None:
    """Claude Code's global config; None when that depends on a folder iCode can't know."""
    override = ctx.environ.get("CLAUDE_CONFIG_DIR")
    if override:
        config_folder = _start_folder_path(ctx, override)
        if config_folder is None:
            return None
        claude_json = config_folder / _CONFIG_FILE_NAME
    else:
        claude_json = ctx.home / _CONFIG_FILE_NAME
        # Set, even empty, it names the folder of the legacy file [observed 2.1.302]; an empty one
        # names a folder iCode can't know without a workspace, so only .claude.json is read then.
        config_folder = ctx.home / ".claude" if override is None else _start_folder_path(ctx, override)
        if config_folder is None:
            return claude_json
    legacy = config_folder / _LEGACY_CONFIG_FILE_NAME
    return legacy if _legacy_config_applies(legacy, claude_json) else claude_json


def _start_folder_path(ctx: ImportContext, value: str) -> Path | None:
    """*value* as a folder; a relative one is under the folder Claude Code starts in, which the workspace stands in for.

    ``~`` is not expanded: Claude Code reads ``~/cfg`` as a folder named ``~`` [observed 2.1.302].
    """
    base = Path(value)
    if base.is_absolute():
        return base
    return None if not ctx.workspace_cwd else Path(ctx.workspace_cwd) / base


def _legacy_config_applies(legacy: Path, claude_json: Path) -> bool:
    """Whether Claude Code reads *legacy* instead of *claude_json* [binary 2.1.302; observed 2.1.302]."""
    try:
        info = os.stat(legacy)
    except OSError, ValueError:
        return False
    if not stat.S_ISREG(info.st_mode):
        return False
    # An empty one a sandboxed command left behind doesn't hide .claude.json.
    return info.st_size > 0 or (bool(info.st_mode & 0o222) and not os.path.exists(claude_json))


def _project_files(workspace_cwd: str | None) -> list[Path]:
    """The ``.mcp.json`` Claude Code reads for the workspace: its own, then each folder's above it."""
    if not workspace_cwd:
        return []
    try:
        folder = Path(os.path.realpath(workspace_cwd))
    except OSError, ValueError:
        return []
    # Claude Code stops below the filesystem root [binary 2.1.302].
    return [candidate / _PROJECT_FILE_NAME for candidate in (folder, *folder.parents) if candidate != candidate.parent]


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

    def read(self, path: Path, *, blank_holds_nothing: bool = False) -> dict[str, Any] | None:
        try:
            raw = read_source_bytes(path)
            if raw is None:
                return None
            text = decode_source(raw)
            if blank_holds_nothing and not text.strip(_JS_WHITESPACE):
                return {}
            return parse_json_object(text)
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
    if not isinstance(entry, dict) or not name.strip() or _is_reserved_name(name):
        return None
    transport = _transport(entry)
    if transport == "stdio":
        return _stdio_candidate(name, entry, scope, source_path)
    if transport == "http":
        return _http_candidate(name, entry, scope, source_path)
    if transport in _UNSUPPORTED_REMOTE_TRANSPORTS and _remote_shape_is_valid(entry, transport):
        return _unsupported(name, entry, scope, source_path, UnsupportedReason.UNSUPPORTED_TRANSPORT)
    # A url with no type (read as stdio, so a configuration error), "sdk" (only an SDK host
    # registers one) and unknown types: Claude Code skips them all
    # [doc: mcp, "Option 1: Add a remote HTTP server"].
    return None


def _is_reserved_name(name: str) -> bool:
    """Whether Claude Code keeps *name* for a server of its own and skips the entry."""
    if name in _RESERVED_LITERAL_NAMES:
        return True
    # JavaScript rewrites UTF-16 code units, so a character beyond U+FFFF becomes two.
    rewritten = _TOOL_NAME_UNSAFE_RE.sub(lambda match: "__" if ord(match.group()) > 0xFFFF else "_", name)
    if name.startswith(_CONNECTOR_NAME_PREFIX):
        rewritten = re.sub("_+", "_", rewritten).strip("_")
    return f"mcp__{rewritten}__tool".split("__")[1] in _RESERVED_SERVER_NAMES


def _transport(entry: dict[str, Any]) -> str:
    """The transport Claude Code would use: declared, stdio when none is declared, empty when unusable."""
    if "type" not in entry:
        return "" if "url" in entry else "stdio"
    declared = entry["type"]
    if not isinstance(declared, str):
        return ""
    return "http" if declared in _HTTP_ALIASES else declared


def _remote_shape_is_valid(entry: dict[str, Any], transport: str) -> bool:
    """Whether Claude Code would connect the remote entry: a non-empty url, string headers, well-typed settings."""
    url = entry.get("url")
    if not isinstance(url, str) or not url.strip() or not _is_str_mapping(entry.get("headers", {})):
        return False
    checks = _WEBSOCKET_CHECKS if transport == "ws" else _HTTP_CHECKS
    return _settings_are_valid(entry, checks)


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
    if not _settings_are_valid(entry, _STDIO_CHECKS):
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
    if not _remote_shape_is_valid(entry, "http"):
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
        helper: str = entry["headersHelper"]
        warnings = (*warnings, ImportWarning(ImportWarningKind.HEADERS_HELPER_IGNORED, original=helper))
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
    if not isinstance(value, int | float) or not _is_positive_int(value):
        return None
    return min(int(value), _MAX_REQUEST_TIMEOUT_MS)


def _is_str_list(value: object) -> bool:
    return isinstance(value, list) and all(isinstance(item, str) for item in value)


def _is_str_mapping(value: object) -> bool:
    return isinstance(value, dict) and all(isinstance(item, str) for item in value.values())


def _settings_are_valid(entry: dict[str, Any], checks: Mapping[str, Callable[[object], bool]]) -> bool:
    """Whether every setting *checks* names that *entry* holds has the type Claude Code requires."""
    return all(check(entry[key]) for key, check in checks.items() if key in entry)


def _is_bool(value: object) -> bool:
    return isinstance(value, bool)


def _is_str(value: object) -> bool:
    return isinstance(value, str)


def _is_non_empty_str(value: object) -> bool:
    return isinstance(value, str) and bool(value)


def _is_positive_int(value: object) -> bool:
    """A JSON number Claude Code takes as a positive whole number: ``1500.0`` is one.

    An int is compared as it is, never converted to a float, which a long one overflows.
    """
    if isinstance(value, bool):
        return False
    if isinstance(value, int):
        return 0 < value <= _MAX_SAFE_INTEGER
    return isinstance(value, float) and value.is_integer() and 0 < value <= _MAX_SAFE_INTEGER


def _is_https_url(value: object) -> bool:
    """An ``https://`` URL with a host, approximating the WHATWG parser Claude Code checks it with.

    Claude Code trims the value first and reads ``\\`` as ``/``. When unsure this accepts: iCode
    never imports ``oauth``, and rejecting it would hide a server Claude Code loads.
    """
    if not isinstance(value, str):
        return False
    text = value.strip(_JS_WHITESPACE)
    if not text.startswith("https://"):
        return False
    try:
        parts = urlsplit(_URL_DROPPED_RE.sub("", text.strip(_C0_AND_SPACE)).replace("\\", "/"))
        _ = parts.port
    except ValueError:
        return False
    host = parts.hostname
    return bool(host) and _FORBIDDEN_HOST_RE.search(host) is None


def _is_oauth(value: object) -> bool:
    return isinstance(value, dict) and _settings_are_valid(value, _OAUTH_CHECKS)


def _is_tool_list(value: object) -> bool:
    return isinstance(value, list) and all(
        isinstance(tool, dict)
        and isinstance(tool.get("name"), str)
        and ("permission_policy" not in tool or _is_one_of(tool["permission_policy"], _TOOL_POLICIES))
        for tool in value
    )


def _is_tool_permissions(value: object) -> bool:
    return isinstance(value, dict) and all(_is_one_of(permission, _TOOL_PERMISSIONS) for permission in value.values())


def _is_one_of(value: object, choices: frozenset[str]) -> bool:
    return isinstance(value, str) and value in choices


# The settings each schema types, beyond those every transport's conversion checks
# itself (``command``, ``args``, ``env``, ``url`` and ``headers``) [binary 2.1.302].
_STDIO_CHECKS: dict[str, Callable[[object], bool]] = {
    "timeout": _is_positive_int,
    "alwaysLoad": _is_bool,
    "bareElicitationCapability": _is_bool,
}
_WEBSOCKET_CHECKS: dict[str, Callable[[object], bool]] = {**_STDIO_CHECKS, "headersHelper": _is_str}
_HTTP_CHECKS: dict[str, Callable[[object], bool]] = {
    **_WEBSOCKET_CHECKS,
    "oauth": _is_oauth,
    "tools": _is_tool_list,
    "discoveryCache": _is_bool,
    "toolPermissions": _is_tool_permissions,
}
_OAUTH_CHECKS: dict[str, Callable[[object], bool]] = {
    "clientId": _is_str,
    "callbackPort": _is_positive_int,
    "authServerMetadataUrl": _is_https_url,
    "scopes": _is_non_empty_str,
    "xaa": _is_bool,
}
