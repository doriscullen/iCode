# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Import MCP servers configured in pi, the coding agent.

Baseline: pi 1.3.0 (tag ``v1.3.0`` of earendil-works/pi, commit 71023aec1,
2026-10-15). Each rule is tagged with the source it was read from:
``[code: CA/core/mcp-servers.ts:248]`` with ``CA`` =
``packages/coding-agent/src``.

What is read [code: CA/extensions/mcp/config.ts:91-156]:

- ``global``: ``mcp.json`` in pi's agent folder, ``$PI_CODING_AGENT_DIR``
  when set (``~`` expanded; relative to the folder pi starts in, for which the
  workspace stands in), else ``~/.pi/agent`` on every OS [code: CA/config.ts:581-611].
- ``project``: ``.pi/mcp.json`` in the workspace itself, no folder above it,
  read only when pi trusts the workspace. Its servers replace global ones of
  the same name; an entry with none of ``command``, ``url`` and ``type`` only
  changes the ``enabled``, ``exposure`` and ``toolExposure`` of the global
  server it names.

Trust [code: CA/core/project-trust.ts:47-97, CA/core/trust-manager.ts]: pi
looks up the workspace and each folder above it in ``trust.json`` in the agent
folder, the nearest ``true`` or ``false`` deciding; without one it follows
``defaultProjectTrust`` in the agent folder's ``settings.json``, and asks
when that is ``ask`` (the default). A folder pi distrusts, or one it would
never ask about because ``defaultProjectTrust`` is ``never``, has its project
servers listed but locked: pi doesn't run them. A folder pi would ask about
counts as trusted; the user decides here instead. The launch flags that
decide trust for one run are unknown here.

Both files are strict JSON: a comment, a trailing comma or a byte-order mark
makes pi skip the whole file. Each server is checked as pi checks it
[code: CA/core/mcp-servers.ts:248-305]; one pi rejects is left out, as are a
second name differing from another only in ``-`` versus ``_``, and a project
server with ``auth``. Only ``env`` and ``headers`` values hold variables
[code: CA/core/resolve-config-value.ts]: ``${NAME}`` and ``$NAME`` become
iCode's ``{{NAME}}``, ``$$`` and ``$!`` a literal ``$`` and ``!``; a value
starting with ``!`` runs a command, which iCode doesn't, so its server can't
be imported. pi fails the connection on an unset or empty variable, as iCode
does. ``~`` in ``command`` and ``args`` is expanded on import, as pi expands
it on start [code: CA/extensions/mcp/runtime.ts:89-121].

Settings carried over: ``enabled`` (a server pi has turned off is imported
turned off), ``timeout`` (seconds, rounded up), ``description``, and how its
tools are offered. pi's default ``codemode`` and ``deferred`` keep tools out
of the model's tool list until something looks them up [code:
CA/extensions/mcp/tools.ts:41-46], which is iCode's on-demand loading;
``direct`` is iCode's full loading; ``hidden`` offers no tools.
``toolExposure`` entries naming one tool become iCode's tool lists where they
can; patterns and the rest are named in a warning.

Not read: servers extensions register, and the ``--no-mcp`` flag and the
``-builtin:mcp`` setting that turn MCP off. Runtime differences: pi starts a
stdio server in its ``cwd`` (default: the folder pi starts in), iCode always
in the workspace; pi signs in to an HTTP server with OAuth, or with the token
of a provider pi is logged in to (``auth``); iCode only sends the headers.

When pi updates: read the CHANGELOG.md entries after the baseline that
mention MCP, ``mcp.json`` or trust; re-check the rules here against the cited
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
    decode_source,
    json_number,
    read_source_bytes,
    render_template,
    templates_convertible,
)
from chrys.service.profiles.agents.schema import MCPServerConfig

if TYPE_CHECKING:
    from collections.abc import Iterable

SCOPE_GLOBAL = "global"
SCOPE_PROJECT = "project"

_CONFIG_FILE_NAME = "mcp.json"
_PROJECT_DIR_NAME = ".pi"
_UTF8_BOM = b"\xef\xbb\xbf"

_SERVER_NAME_RE = re.compile(r"[A-Za-z0-9_-]+")
_VARIABLE_NAME_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_OVERRIDE_KEYS = frozenset({"enabled", "exposure", "toolExposure"})
_EXPOSURES = frozenset({"codemode", "deferred", "direct", "hidden"})
_EXPOSURE_ALIASES = {"codemode-deferred": "codemode"}
_LOOPBACK_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})

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
    untrusted: list[_Entry] = field(default_factory=list)
    errors: list[McpSourceError] = field(default_factory=list)
    auto_enable_codemode: bool = True


class PiSource:
    """Reads pi's global servers and, when pi trusts the workspace, its project servers."""

    @property
    def source_id(self) -> str:
        return "pi"

    @property
    def display_name(self) -> str:
        return "pi"

    def detect(self, ctx: ImportContext) -> bool:
        agent_dir = _agent_dir(ctx)
        if agent_dir is not None and agent_dir.is_dir():
            return True
        project = _project_file(ctx)
        return project is not None and project.is_file()

    def discover(self, ctx: ImportContext) -> McpImportResult:
        state = _State()
        agent_dir = _agent_dir(ctx)
        if agent_dir is not None:
            _read_file(state, agent_dir / _CONFIG_FILE_NAME, SCOPE_GLOBAL, trusted=True)
        project = _project_file(ctx)
        if project is not None and project.exists():
            trusted = _project_trusted(ctx, agent_dir)
            _read_file(state, project, SCOPE_PROJECT, trusted=trusted, scope_path=str(project.parent.parent))
        on_demand_search = any(
            _ON_DEMAND_SEARCH in _exposures(entry.config) for entry in state.entries.values() if _enabled(entry.config)
        )
        codemode_off = not state.auto_enable_codemode and not on_demand_search
        candidates = [_candidate(entry, ctx, codemode_off=codemode_off) for entry in state.entries.values()]
        candidates.extend(_untrusted_candidate(entry) for entry in state.untrusted)
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


def _project_trusted(ctx: ImportContext, agent_dir: Path | None) -> bool:
    """Whether pi would read the project file, or ask the user whether to."""
    if agent_dir is None or not ctx.workspace_cwd:
        return True
    decision = _trust_decision(agent_dir / "trust.json", ctx.workspace_cwd)
    if decision is not None:
        return decision
    settings = _read_lenient(agent_dir / "settings.json")
    return settings.get("defaultProjectTrust") != "never"


def _trust_decision(trust_file: Path, workspace: str) -> bool | None:
    """The nearest explicit decision for *workspace* or a folder above it [code: CA/core/trust-manager.ts:45-59]."""
    data = _read_lenient(trust_file)
    try:
        folder = Path(os.path.realpath(workspace))
    except OSError, ValueError:
        return None
    for candidate in (folder, *folder.parents):
        value = data.get(str(candidate))
        if isinstance(value, bool):
            return value
    return None


def _read_lenient(path: Path) -> dict[str, Any]:
    """A JSON object pi keeps for itself (byte-order mark allowed); empty when missing or unusable."""
    try:
        raw = read_source_bytes(path)
        document = None if raw is None else json.loads(decode_source(raw))
    except SourceReadError, ValueError, RecursionError:
        return {}
    return document if isinstance(document, dict) else {}


def _read_file(state: _State, path: Path, scope: str, *, trusted: bool, scope_path: str = "") -> None:
    try:
        document = _read_json(path)
    except SourceReadError as exc:
        state.errors.append(McpSourceError(str(path), exc.kind))
        return
    if document is None:
        return
    servers = document.get("mcpServers", {})
    if not isinstance(servers, dict):
        state.errors.append(McpSourceError(str(path), SourceErrorKind.INVALID_CONFIG))
        return
    if trusted and isinstance(document.get("autoEnableCodemode"), bool):
        state.auto_enable_codemode = document["autoEnableCodemode"]
    for name, value in servers.items():
        if not trusted:
            config = _validate(name, value)
            if config is not None:
                state.untrusted.append(_Entry(name, config, str(path), scope, scope_path))
            continue
        _add_entry(state, name, value, str(path), scope, scope_path)


def _read_json(path: Path) -> dict[str, Any] | None:
    """*path* as pi's ``JSON.parse`` reads it; None when missing. Raises :class:`SourceReadError`."""
    raw = read_source_bytes(path)
    if raw is None:
        return None
    if raw.startswith(_UTF8_BOM):
        raise SourceReadError(SourceErrorKind.INVALID_JSON)
    try:
        document = json.loads(decode_source(raw), parse_constant=_reject_constant)
    except (ValueError, RecursionError) as exc:
        raise SourceReadError(SourceErrorKind.INVALID_JSON) from exc
    if not isinstance(document, dict):
        raise SourceReadError(SourceErrorKind.NOT_AN_OBJECT)
    return document


def _reject_constant(name: str) -> Any:
    raise ValueError(name)


def _add_entry(state: _State, name: str, value: object, source: str, scope: str, scope_path: str) -> None:
    """One server into the list, as pi adds it [code: CA/extensions/mcp/config.ts:107-141]."""
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
    if scope == SCOPE_PROJECT and "url" in config and "auth" in config:
        return
    state.entries[name] = _Entry(name, config, source, scope, scope_path)


def _namespace(name: str) -> str:
    return name.replace("-", "_")


def _validate(name: str, raw: object) -> dict[str, Any] | None:
    """The entry as pi accepts it, exposure aliases resolved; None when pi rejects it."""
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
    if "headers" in value and not _is_str_mapping(value["headers"]):
        return False
    if "oauth" in value and not _oauth_valid(value["oauth"]):
        return False
    if "auth" in value:
        auth = value["auth"]
        if not isinstance(auth, dict) or not isinstance(auth.get("provider"), str) or not auth["provider"]:
            return False
        if url.scheme != "https" and url.hostname not in _LOOPBACK_HOSTS:
            return False
    return True


def _oauth_valid(oauth: object) -> bool:
    """pi's checks on ``oauth`` [code: CA/core/mcp-servers.ts:167-209]."""
    if not isinstance(oauth, dict):
        return False
    for key in ("clientId", "clientSecret", "scope"):
        if key in oauth and not isinstance(oauth[key], str):
            return False
    port = oauth.get("callbackPort")
    if "callbackPort" in oauth and not (isinstance(port, int | float) and _is_integral(port) and 1 <= port <= 65535):
        return False
    callback = None
    if "callbackUrl" in oauth:
        callback = _parse_url(oauth["callbackUrl"]) if isinstance(oauth["callbackUrl"], str) else None
        if callback is None or callback.scheme != "http" or callback.hostname not in _LOOPBACK_HOSTS:
            return False
        if callback.query or callback.fragment:
            return False
        if callback.port is not None and port is not None and callback.port != port:
            return False
    if "clientName" in oauth and not (isinstance(oauth["clientName"], str) and oauth["clientName"].strip()):
        return False
    registration = oauth.get("clientRegistration", "dcr")
    if registration != "dcr":
        if registration != "cimd" or "clientId" in oauth or "clientName" in oauth:
            return False
        if callback is not None and (callback.hostname == "::1" or callback.path != "/callback"):
            return False
    if "authServerMetadataUrl" in oauth:
        metadata = (
            _parse_url(oauth["authServerMetadataUrl"]) if isinstance(oauth["authServerMetadataUrl"], str) else None
        )
        if metadata is None or not (
            metadata.scheme == "https" or (metadata.scheme == "http" and metadata.hostname in _LOOPBACK_HOSTS)
        ):
            return False
    return True


def _stdio_valid(value: dict[str, Any]) -> bool:
    if "args" in value and not (isinstance(value["args"], list) and all(isinstance(arg, str) for arg in value["args"])):
        return False
    if "env" in value and not _is_str_mapping(value["env"]):
        return False
    return "cwd" not in value or isinstance(value["cwd"], str)


def _parse_url(value: str) -> Any:
    """*value* split as a URL with a host, close to ``URL.canParse``; None when it has neither."""
    if any(char.isspace() for char in value.strip()):
        return None
    try:
        parts = urlsplit(value.strip())
        parts.port  # noqa: B018  # raises on a port that isn't a number
    except ValueError:
        return None
    return parts if parts.scheme and parts.hostname else None


def _is_integral(value: object) -> bool:
    """A JSON number JavaScript's ``Number.isInteger`` accepts, ``3000.0`` included."""
    number = json_number(value)
    return number is not None and math.isfinite(number) and number.is_integer()


def _is_positive_number(value: object) -> bool:
    number = json_number(value)
    return number is not None and math.isfinite(number) and number > 0


def _is_str_mapping(value: object) -> bool:
    return isinstance(value, dict) and all(isinstance(item, str) for item in value.values())


def _enabled(config: dict[str, Any]) -> bool:
    return config.get("enabled") is not False


# --- candidates ---------------------------------------------------------------

# The exposure pi reaches through tool search, which it turns on even without codemode.
_ON_DEMAND_SEARCH = "deferred"


def _exposures(config: dict[str, Any]) -> set[str]:
    tool_exposure = config.get("toolExposure", {})
    return {config.get("exposure", "codemode"), *tool_exposure.values()}


def _shown(config: dict[str, Any]) -> dict[str, Any]:
    if _transport(config) == "http":
        return {"url": config["url"], "headers": config.get("headers", {})}
    return {"command": config["command"], "args": config.get("args", []), "env": config.get("env", {})}


def _transport(config: dict[str, Any]) -> str:
    """The transport pi connects with: HTTP whenever the entry has a url [code: CA/extensions/mcp/runtime.ts:98-104]."""
    return "http" if "url" in config else "stdio"


def _untrusted_candidate(entry: _Entry) -> McpImportCandidate:
    return McpImportCandidate(
        entry.name,
        entry.scope,
        entry.source,
        None,
        unsupported=UnsupportedReason.UNTRUSTED_PROJECT,
        entry=_shown(entry.config),
        transport=_transport(entry.config),
        scope_path=entry.scope_path,
    )


def _candidate(entry: _Entry, ctx: ImportContext, *, codemode_off: bool) -> McpImportCandidate:
    config = entry.config
    transport = _transport(config)

    def unsupported(reason: UnsupportedReason) -> McpImportCandidate:
        return McpImportCandidate(
            entry.name,
            entry.scope,
            entry.source,
            None,
            unsupported=reason,
            entry=_shown(config),
            transport=transport,
            scope_path=entry.scope_path,
        )

    values_key = "headers" if transport == "http" else "env"
    converted: dict[str, str] = {}
    for key, value in config.get(values_key, {}).items():
        parts = _value_parts(value)
        if parts is None:
            return unsupported(UnsupportedReason.VALUE_FROM_COMMAND)
        if not templates_convertible(parts):
            return unsupported(UnsupportedReason.TEMPLATE_NEXT_TO_BRACE)
        converted[key] = render_template(parts)

    warnings: list[ImportWarning] = []
    cwd = config.get("cwd")
    if transport == "stdio" and isinstance(cwd, str) and cwd not in {"", "."}:
        warnings.append(ImportWarning(ImportWarningKind.CWD_IGNORED, original=cwd))
    if transport == "http" and "oauth" in config:
        warnings.append(ImportWarning(ImportWarningKind.OAUTH_IGNORED))
    if transport == "http" and "auth" in config:
        warnings.append(ImportWarning(ImportWarningKind.AUTH_PROVIDER_IGNORED, original=config["auth"]["provider"]))
    if entry.override:
        warnings.append(ImportWarning(ImportWarningKind.MERGED, original=entry.override))
    access = _tool_access(config)
    warnings.extend(access.warnings)
    if codemode_off and "codemode" in _exposures(config):
        warnings.append(ImportWarning(ImportWarningKind.CODEMODE_OFF))
    enabled = _enabled(config)
    if not enabled:
        warnings.append(ImportWarning(ImportWarningKind.DISABLED))
    timeout = config.get("timeout")
    # Keep tool_name_prefix empty, matching iCode's manual and Claude imports.
    # pi v1.3.0 adds mcp__<server>__<tool> names automatically; iCode leaves
    # prefix selection to the user when tools collide with builtins or other servers.
    common: dict[str, Any] = {
        "name": entry.name,
        "enabled": enabled,
        "description": config.get("description", ""),
        "request_timeout": None if timeout is None else math.ceil(timeout),
        "allowed_tools": access.allowed_tools,
        "use_progressive_disclosure": access.on_demand,
        "always_load": access.always_load,
    }
    if transport == "http":
        server = MCPServerConfig(transport="http", url=config["url"], headers=converted, **common)
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

    Exact tool names beat patterns in pi [code: CA/core/mcp-servers.ts:234-242];
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
