# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Import MCP servers configured in pi, the coding agent.

Baseline: pi 1.5.0 (tag ``v1.5.0`` of earendil-works/pi, commit f9a263655,
2026-11-02), rechecked 2026-11-04. Connection, discovery, variable and exposure
rules are unchanged from 1.4.0; OAuth's new ``clientRegistration: "auto"``
remains deliberately unmigrated. Each rule is tagged with the source it was read from:
``[code: CA/core/mcp-servers.ts:260]`` with ``CA`` =
``packages/coding-agent/src``.

What is read [code: CA/extensions/mcp/config.ts:92-157]:

- ``global``: ``mcp.json`` in pi's agent folder, ``$PI_CODING_AGENT_DIR``
  when set (``~`` expanded; relative to the folder pi starts in, for which the
  workspace stands in), else ``~/.pi/agent`` on every OS [code: CA/config.ts:606-636].
- ``project``: ``.pi/mcp.json`` in the workspace itself, no folder above it.
  Its servers replace global ones of
  the same name; an entry with none of ``command``, ``url`` and ``type`` only
  changes the ``enabled``, ``exposure`` and ``toolExposure`` of the global
  server it names.

pi's project trust settings are not read: the user decides which configurations
to import. Import does not start servers or change pi's trust settings.

Both files are strict JSON after one leading byte-order mark, which pi drops
[code: CA/extensions/mcp/config.ts:97]: a comment or a trailing comma makes
pi skip the whole file. Migrated connection and tool exposure settings follow
the source forms [code: CA/core/mcp-servers.ts:260-317]; omitted OAuth and auth
settings are not validated. A second name differing from another only in
``-`` versus ``_`` is left out. The baseline records verified conversion
rules, not a source version gate. Only ``env`` and ``headers`` values hold variables
[code: CA/core/resolve-config-value.ts]: ``${NAME}`` and ``$NAME`` become
iCode's ``{{NAME}}``, ``$$`` and ``$!`` a literal ``$`` and ``!``; a value
starting with ``!`` runs a command. It is not converted to iCode's
``{{!command}}``: pi uses its process cwd and shell, a 10-second limit,
uncached output and accepts more than one line of UTF-8 text [code:
CA/core/resolve-config-value.ts]. iCode uses the home folder, its configured
shell, a 30-second limit and single-line printable ASCII, with HTTP credential
caching and refresh. Such a server needs manual configuration. pi fails the
connection on an unset or empty variable, as iCode
does. ``~`` in ``command`` and ``args`` is expanded on import, as pi expands
it on start [code: CA/extensions/mcp/runtime.ts:89-121].

Settings carried over: ``enabled`` (a server pi has turned off is imported
turned off), ``timeout`` (seconds, rounded up), ``description``, and how its
tools are offered. An omitted timeout keeps pi's default of 60 seconds
[code: CA/extensions/mcp/runtime.ts:48,211]. pi's default ``codemode`` and ``deferred`` keep tools out
of the model's tool list until something looks them up [code:
CA/extensions/mcp/tools.ts:41-46], which is iCode's on-demand loading;
``direct`` is iCode's full loading; ``hidden`` offers no tools.
``toolExposure`` entries naming one tool become iCode's tool lists where they
can; patterns and the rest are named in a warning.

pi accepts a ``timeout`` too long for a JavaScript number, which it reads as
Infinity [code: CA/core/mcp-servers.ts:279]. That one, and one whose
milliseconds overflow to Infinity, set no time limit [code:
CA/extensions/mcp/runtime.ts:210-212; packages/mcp/src/client.ts:553-559],
and are imported as the longest iCode waits. A finite timeout above
2,147,483,647 ms, Node's timer limit, makes pi time out every request at once;
iCode waits the time given instead, capped the same way.

Not read: servers extensions register, and the ``--no-mcp`` flag and the
``-builtin:mcp`` setting that turn MCP off. Runtime differences: pi starts a
stdio server in its ``cwd`` (default: the folder pi starts in), iCode always
in the workspace; pi signs in to an HTTP server with OAuth, or with the token
of a provider pi is logged in to (``auth``); iCode only sends the headers.

When pi updates: read the CHANGELOG.md entries after the baseline that
mention MCP, config loading or variables; re-check discovery and migrated fields against the cited
files at the new tag, then update the code, the tests and the baseline above.
"""

from __future__ import annotations

import json
import math
import os
import re
from dataclasses import dataclass, field
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
    SourceErrorKind,
    SourceReadError,
    TemplatePart,
    TemplateVariable,
    UnsupportedReason,
    json_number,
    read_source_bytes,
    render_template,
    request_timeout_seconds,
    templates_convertible,
    trim_header_value,
    unsupported_env_value,
)
from chrys.service.profiles.agents.schema import MCPServerConfig

if TYPE_CHECKING:
    from collections.abc import Iterable

SCOPE_GLOBAL = "global"
SCOPE_PROJECT = "project"

_CONFIG_FILE_NAME = "mcp.json"
_PROJECT_DIR_NAME = ".pi"
_DEFAULT_TIMEOUT_SECONDS = 60

_SERVER_NAME_RE = re.compile(r"[A-Za-z0-9_-]+")
_VARIABLE_NAME_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_OVERRIDE_KEYS = frozenset({"enabled", "exposure", "toolExposure"})
_EXPOSURES = frozenset({"codemode", "deferred", "direct", "hidden"})
_EXPOSURE_ALIASES = {"codemode-deferred": "codemode"}

# How iCode offers a server's tools for each of pi's exposures.
_FULL = "full"
_ON_DEMAND = "on_demand"
_HIDDEN = "hidden"
_EXPOSURE_CLASS = {"direct": _FULL, "codemode": _ON_DEMAND, "deferred": _ON_DEMAND, "hidden": _HIDDEN}


@dataclass(slots=True)
class _Entry:
    name: str
    config: dict[str, Any]
    source: str
    scope: str
    scope_path: str = ""
    override: str = ""


@dataclass(slots=True)
class _State:
    entries: dict[str, _Entry] = field(default_factory=dict)
    errors: list[McpSourceError] = field(default_factory=list)


class PiSource:
    """Reads pi's global and project servers for the user to choose from."""

    @property
    def source_id(self) -> str:
        return "pi"

    @property
    def display_name(self) -> str:
        return "pi"

    def detect(self, ctx: ImportContext) -> bool:
        agent_dir = _agent_dir(ctx)
        # An empty agent folder (which pi creates on start) holds no servers, so it is not
        # a sign that anything can be imported; the global mcp.json and the project file are.
        if agent_dir is not None and (agent_dir / _CONFIG_FILE_NAME).is_file():
            return True
        project = _project_file(ctx)
        return project is not None and project.is_file()

    def discover(self, ctx: ImportContext) -> McpImportResult:
        state = _State()
        agent_dir = _agent_dir(ctx)
        if agent_dir is not None:
            _read_file(state, agent_dir / _CONFIG_FILE_NAME, SCOPE_GLOBAL)
        project = _project_file(ctx)
        if project is not None and project.exists():
            _read_file(state, project, SCOPE_PROJECT, scope_path=str(project.parent.parent))
        candidates = [_candidate(entry, ctx) for entry in state.entries.values()]
        return McpImportResult(candidates=tuple(candidates), source_errors=tuple(state.errors))


def _agent_dir(ctx: ImportContext) -> Path | None:
    """pi's agent folder; None when it is relative and there is no workspace to stand in for pi's start folder."""
    override = ctx.environ.get("PI_CODING_AGENT_DIR", "")
    if not override:
        return ctx.home / ".pi" / "agent"
    path = _expand_home(override, ctx.home)
    if os.path.isabs(path):
        return Path(path)
    return None if not ctx.workspace_cwd else Path(ctx.workspace_cwd) / path


def _project_file(ctx: ImportContext) -> Path | None:
    return None if not ctx.workspace_cwd else Path(ctx.workspace_cwd) / _PROJECT_DIR_NAME / _CONFIG_FILE_NAME


def _expand_home(value: str, home: Path) -> str:
    """``~`` and ``~/…`` (also ``~\\…`` on Windows) as the home folder, as pi expands them."""
    if value == "~":
        return str(home)
    if value.startswith("~/") or (os.sep == "\\" and value.startswith("~\\")):
        return str(home / value[2:])
    return value


def _read_file(state: _State, path: Path, scope: str, *, scope_path: str = "") -> None:
    try:
        document = _read_json(path)
    except SourceReadError as exc:
        state.errors.append(McpSourceError(str(path), exc.kind, exc.field, exc.line, exc.column))
        return
    if document is None:
        return
    servers = document.get("mcpServers", {})
    if not isinstance(servers, dict):
        state.errors.append(McpSourceError(str(path), SourceErrorKind.INVALID_CONFIG, field="mcpServers"))
        return
    for name, value in servers.items():
        _add_entry(state, name, value, str(path), scope, scope_path)


def _read_json(path: Path) -> dict[str, Any] | None:
    """*path* as pi's ``JSON.parse`` reads it; None when missing. Raises :class:`SourceReadError`."""
    raw = read_source_bytes(path)
    if raw is None:
        return None
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise SourceReadError(SourceErrorKind.INVALID_ENCODING) from exc
    try:
        document = json.loads(text, parse_constant=_reject_constant)
    except json.JSONDecodeError as exc:
        raise SourceReadError(SourceErrorKind.INVALID_JSON, line=exc.lineno, column=exc.colno) from exc
    except (ValueError, RecursionError) as exc:
        raise SourceReadError(SourceErrorKind.INVALID_JSON) from exc
    if not isinstance(document, dict):
        raise SourceReadError(SourceErrorKind.NOT_AN_OBJECT)
    return document


def _reject_constant(name: str) -> Any:
    raise ValueError(name)


def _add_entry(state: _State, name: str, value: object, source: str, scope: str, scope_path: str) -> None:
    """One server into the list, as pi adds it [code: CA/extensions/mcp/config.ts:108-142]."""
    if scope == SCOPE_PROJECT and isinstance(value, dict) and not ({"command", "url", "type"} & value.keys()):
        base = state.entries.get(name)
        if base is None or value.keys() - _OVERRIDE_KEYS:
            return
        merged = _validate(name, {**base.config, **value})
        if merged is not None:
            state.entries[name] = _Entry(name, merged, base.source, base.scope, base.scope_path, override=source)
        return
    config = _validate(name, value)
    if config is None:
        return
    # Names differing only in "-" and "_" would share one tool namespace.
    if any(other != name and _namespace(other) == _namespace(name) for other in state.entries):
        return
    state.entries[name] = _Entry(name, config, source, scope, scope_path)


def _namespace(name: str) -> str:
    return name.replace("-", "_")


def _validate(name: str, raw: object) -> dict[str, Any] | None:
    """Validate migration fields and resolve exposure aliases, without validating omitted auth."""
    if _SERVER_NAME_RE.fullmatch(name) is None or not isinstance(raw, dict):
        return None
    value = dict(raw)
    if isinstance(value.get("exposure"), str):
        value["exposure"] = _EXPOSURE_ALIASES.get(value["exposure"], value["exposure"])
    tool_exposure = value.get("toolExposure")
    if isinstance(tool_exposure, dict):
        value["toolExposure"] = {
            tool: _EXPOSURE_ALIASES.get(item, item) if isinstance(item, str) else item
            for tool, item in tool_exposure.items()
        }
    if not _common_valid(value):
        return None
    kind = value.get("type")
    if kind == "sse":
        return None
    # A tuple, not a set: the type may be any JSON value, a list included.
    if isinstance(value.get("url"), str) and kind in (None, "http", "streamable-http"):
        return value if _http_valid(value) else None
    if isinstance(value.get("command"), str) and kind in (None, "stdio"):
        # pi connects to a url whenever the entry has one, whatever its type says
        # [code: CA/extensions/mcp/runtime.ts:98-104]; it fails when that url is unusable.
        if "url" in value and not (isinstance(value["url"], str) and _http_valid(value)):
            return None
        return value if _stdio_valid(value) else None
    return None


def _common_valid(value: dict[str, Any]) -> bool:
    if "exposure" in value and not _is_exposure(value["exposure"]):
        return False
    if "toolExposure" in value:
        tool_exposure = value["toolExposure"]
        if not isinstance(tool_exposure, dict) or not all(_is_exposure(item) for item in tool_exposure.values()):
            return False
    if "enabled" in value and not isinstance(value["enabled"], bool):
        return False
    if "description" in value and not isinstance(value["description"], str):
        return False
    return "timeout" not in value or _is_positive_number(value["timeout"])


def _is_exposure(value: object) -> bool:
    return isinstance(value, str) and value in _EXPOSURES


def _http_valid(value: dict[str, Any]) -> bool:
    url = _parse_url(value["url"])
    if url is None or url.scheme not in {"http", "https"}:
        return False
    return "headers" not in value or _is_str_mapping(value["headers"])


def _stdio_valid(value: dict[str, Any]) -> bool:
    if "args" in value and not (isinstance(value["args"], list) and all(isinstance(arg, str) for arg in value["args"])):
        return False
    return "env" not in value or _is_str_mapping(value["env"])


def _parse_url(value: str) -> Any:
    """*value* split as a URL with a host, close to ``URL.canParse``; None when it has neither."""
    value = _normalize_url(value)
    try:
        parts = urlsplit(value)
        parts.port  # noqa: B018  # raises on a port that isn't a number
    except ValueError:
        return None
    hostname = parts.hostname
    if not parts.scheme or not hostname or any(char.isspace() or ord(char) < 32 for char in hostname):
        return None
    return parts


def _normalize_url(value: str) -> str:
    """The common WHATWG whitespace cleanup applied by pi's URL constructor."""
    return value.strip("".join(chr(code) for code in range(33))).replace("\t", "").replace("\r", "").replace("\n", "")


def _is_positive_number(value: object) -> bool:
    """A JSON number pi takes as a positive one: Infinity, from a number too long for a double, included."""
    number = json_number(value)
    return number is not None and number > 0


def _is_str_mapping(value: object) -> bool:
    return isinstance(value, dict) and all(isinstance(item, str) for item in value.values())


def _enabled(config: dict[str, Any]) -> bool:
    return config.get("enabled") is not False


# --- candidates ---------------------------------------------------------------


def _shown(config: dict[str, Any]) -> dict[str, Any]:
    if _transport(config) == "http":
        return {"url": config["url"], "headers": config.get("headers", {})}
    return {"command": config["command"], "args": config.get("args", []), "env": config.get("env", {})}


def _transport(config: dict[str, Any]) -> str:
    """The transport pi connects with: HTTP whenever the entry has a url [code: CA/extensions/mcp/runtime.ts:98-104]."""
    return "http" if "url" in config else "stdio"


def _candidate(entry: _Entry, ctx: ImportContext) -> McpImportCandidate:
    config = entry.config
    transport = _transport(config)

    def unsupported(reason: UnsupportedReason, field: str) -> McpImportCandidate:
        return McpImportCandidate(
            entry.name,
            entry.scope,
            entry.source,
            None,
            unsupported=reason,
            entry=_shown(config),
            transport=transport,
            scope_path=entry.scope_path,
            unsupported_field=field,
        )

    values_key = "headers" if transport == "http" else "env"
    converted: dict[str, str] = {}
    header_names: dict[str, str] = {}
    escaped_braces = False
    for key, value in config.get(values_key, {}).items():
        source_field = f"mcpServers.{entry.name}.{values_key}.{key}"
        parts = _value_parts(value)
        if parts is None:
            return unsupported(UnsupportedReason.VALUE_FROM_COMMAND, source_field)
        if not templates_convertible(parts):
            return unsupported(UnsupportedReason.TEMPLATE_NEXT_TO_BRACE, source_field)
        if values_key == "headers" and any(not part.isascii() for part in parts if isinstance(part, str)):
            return unsupported(UnsupportedReason.HEADER_VALUE_ENCODING, source_field)
        rendered = render_template(parts)
        escaped_braces |= rendered != "".join(
            part if isinstance(part, str) else "{{" + part.name + "}}" for part in parts
        )
        # pi's fetch trims a header value; iCode's HTTP client refuses one it doesn't.
        if values_key == "headers":
            rendered = trim_header_value(rendered)
            first_name = header_names.setdefault(key.lower(), key)
            # Web Headers combines case aliases before sending them.
            converted[first_name] = f"{converted[first_name]}, {rendered}" if first_name in converted else rendered
        else:
            converted[key] = rendered
    if transport == "stdio" and (invalid_env := unsupported_env_value(converted)) is not None:
        reason, field = invalid_env
        return unsupported(reason, f"mcpServers.{entry.name}.env.{field}")
    if values_key == "headers":
        converted = {key: trim_header_value(value) for key, value in converted.items()}

    warnings: list[ImportWarning] = []
    if escaped_braces:
        warnings.append(ImportWarning(ImportWarningKind.LITERAL_BRACES_ESCAPED))
    cwd = config.get("cwd")
    if transport == "stdio" and isinstance(cwd, str) and cwd not in {"", "."}:
        expanded = _expand_home(cwd, ctx.home)
        warnings.append(
            ImportWarning(
                ImportWarningKind.CWD_IGNORED,
                field=(ctx.workspace_cwd or "") if not Path(expanded).is_absolute() else "",
                original=expanded,
            )
        )
    elif transport == "stdio" and "cwd" in config and not isinstance(cwd, str):
        warnings.append(ImportWarning(ImportWarningKind.UNKNOWN_FIELDS_IGNORED, original="cwd"))
    if transport == "http" and "oauth" in config:
        warnings.append(ImportWarning(ImportWarningKind.OAUTH_IGNORED))
    if transport == "http" and "auth" in config:
        auth = config["auth"]
        provider = auth.get("provider") if isinstance(auth, dict) else None
        warnings.append(
            ImportWarning(ImportWarningKind.AUTH_PROVIDER_IGNORED, original=provider)
            if isinstance(provider, str) and provider
            else ImportWarning(ImportWarningKind.UNKNOWN_FIELDS_IGNORED, original="auth")
        )
    if entry.override:
        warnings.append(ImportWarning(ImportWarningKind.MERGED, original=entry.override))
    access = _tool_access(config)
    warnings.extend(access.warnings)
    enabled = _enabled(config)
    timeout = config.get("timeout", _DEFAULT_TIMEOUT_SECONDS)
    request_timeout = request_timeout_seconds(timeout)
    source_timeout = math.inf if json_number(timeout) == math.inf else timeout
    # The source's own config, no tool name prefix: the dialog gives every import one
    # (base.with_tool_name_prefix), as pi names tools mcp__<server>__<tool>.
    common: dict[str, Any] = {
        "name": entry.name,
        "enabled": enabled,
        "description": config.get("description", ""),
        "request_timeout": request_timeout,
        "allowed_tools": access.allowed_tools,
        "use_progressive_disclosure": access.on_demand,
        "always_load": access.always_load,
    }
    if transport == "http":
        server = MCPServerConfig(transport="http", url=_normalize_url(config["url"]), headers=converted, **common)
    else:
        home = ctx.home
        server = MCPServerConfig(
            transport="stdio",
            command=_expand_home(config["command"], home),
            args=[_expand_home(arg, home) for arg in config.get("args", [])],
            env=converted,
            **common,
        )
    return McpImportCandidate(
        entry.name,
        entry.scope,
        entry.source,
        server,
        warnings=tuple(warnings),
        transport=transport,
        scope_path=entry.scope_path,
        timeout_source_seconds=source_timeout if request_timeout is not None and timeout != request_timeout else None,
    )


def _value_parts(value: str) -> list[TemplatePart] | None:
    """*value* read as pi reads env and header values; None for a value that runs a command."""
    if value.startswith("!"):
        return None
    parts: list[TemplatePart] = []
    index = 0
    while index < len(value):
        dollar = value.find("$", index)
        if dollar < 0:
            parts.append(value[index:])
            break
        parts.append(value[index:dollar])
        following = value[dollar + 1 : dollar + 2]
        if following in {"$", "!"}:
            parts.append(following)
            index = dollar + 2
            continue
        if following == "{":
            end = value.find("}", dollar + 2)
            if end < 0:
                parts.append("$")
                index = dollar + 1
                continue
            name = value[dollar + 2 : end]
            parts.append(TemplateVariable(name) if _VARIABLE_NAME_RE.fullmatch(name) else value[dollar : end + 1])
            index = end + 1
            continue
        match = _VARIABLE_NAME_RE.match(value, dollar + 1)
        if match is not None:
            parts.append(TemplateVariable(match.group(0)))
            index = match.end()
            continue
        parts.append("$")
        index = dollar + 1
    return parts


@dataclass(frozen=True, slots=True)
class _ToolAccess:
    on_demand: bool = False
    allowed_tools: list[str] | None = None
    always_load: list[str] = field(default_factory=list)
    warnings: tuple[ImportWarning, ...] = ()


def _tool_access(config: dict[str, Any]) -> _ToolAccess:
    """pi's ``exposure`` and ``toolExposure`` as iCode's tool lists and loading strategy.

    Exact tool names beat patterns in pi [code: CA/core/mcp-servers.ts:246-254];
    a pattern, or an exact name iCode can't single out, is named in a warning.
    """
    exposure = config.get("exposure", "codemode")
    base = _EXPOSURE_CLASS[exposure]
    named: dict[str, str] = {}
    ignored: list[str] = []
    for tool, tool_exposure in config.get("toolExposure", {}).items():
        kind = _EXPOSURE_CLASS[tool_exposure]
        if kind == base:
            continue
        if "*" in tool:
            ignored.append(tool)
        elif base == _HIDDEN or (base == _ON_DEMAND and kind == _FULL):
            named[tool] = kind
        else:
            ignored.append(tool)
    warnings = list(_ignored_warning(ignored))
    if base == _HIDDEN:
        allowed = [tool for tool, kind in named.items() if kind != _HIDDEN]
        on_demand = any(kind == _ON_DEMAND for kind in named.values())
        always = [tool for tool, kind in named.items() if kind == _FULL] if on_demand else []
        warnings.insert(
            0,
            ImportWarning(ImportWarningKind.TOOLS_LIMITED, field=f"exposure: {exposure}", original=", ".join(allowed)),
        )
        return _ToolAccess(on_demand, allowed, always, tuple(warnings))
    if base == _ON_DEMAND:
        return _ToolAccess(True, None, list(named), tuple(warnings))
    return _ToolAccess(warnings=tuple(warnings))


def _ignored_warning(ignored: list[str]) -> Iterable[ImportWarning]:
    if ignored:
        yield ImportWarning(ImportWarningKind.TOOL_RULES_IGNORED, original=", ".join(ignored))
