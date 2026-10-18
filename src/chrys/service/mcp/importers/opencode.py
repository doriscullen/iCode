# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Import MCP servers configured in opencode.

Baseline: opencode 1.19.3 (tag ``v1.19.3``, commit e3c4eb2425, 2026-10-16),
the release ``npm install opencode-ai`` installs; the ``v2.0.x`` tags of the
same repository are an unreleased rewrite and are not followed. Each rule is
tagged with the source it was read from, relative to the repository root:
``[code: C/config.ts:42]`` with ``C`` = ``packages/opencode/src/config``,
``M`` = ``packages/opencode/src/mcp`` and ``K`` = ``packages/core/src``.

opencode merges every config file it reads into one config, later files
winning, with remeda's ``mergeDeep``: objects merge key by key at every depth
(so one server's ``environment`` or ``headers`` gather keys from several
files), anything else, ``command`` included, is replaced [code: C/config.ts:42].
The servers listed are those of the merged config, in the files' order
[code: C/config.ts:328-548]:

- the global folder ``$XDG_CONFIG_HOME/opencode``, else ``~/.config/opencode``,
  on every OS [code: K/global.ts:13]: ``config.json``, ``opencode.json`` and
  ``opencode.jsonc``, read as one: if one of them is unusable, opencode drops
  all three and runs with the others [code: C/config.ts:260-303];
- the file ``$OPENCODE_CONFIG`` names;
- unless ``OPENCODE_DISABLE_PROJECT_CONFIG`` is ``true`` or ``1``: each
  ``opencode.json`` and ``opencode.jsonc`` from the top of the workspace's Git
  repository (the filesystem root outside one) down to the workspace, the
  deepest winning, ``.jsonc`` over ``.json`` in one folder [code: C/paths.ts:10-21];
- ``opencode.json`` and ``opencode.jsonc`` in each ``.opencode`` folder from the
  workspace up to the repository top (an upper one wins, as opencode reads
  them in that order), then ``~/.opencode``, then ``$OPENCODE_CONFIG_DIR``
  [code: C/paths.ts:23-41, C/config.ts:430-448];
- ``$OPENCODE_CONFIG_CONTENT``, a config given inline;
- the managed folder: ``/Library/Application Support/opencode`` (macOS),
  ``%ProgramData%\\opencode`` (Windows), else ``/etc/opencode``
  [code: C/managed.ts:20-33].

Not read: configs opencode fetches (an organization's ``.well-known/opencode``,
the console account's), macOS managed preferences, the legacy TOML ``config``
opencode migrates on start, servers plugins add while running.

Each file is read as opencode reads it (text in any encoding iCode detects,
where opencode reads UTF-8 only): JSON with comments and trailing commas
[code: C/parse.ts:8], ``mcp`` written in opencode 2's form lowered first
[code: C/v2-compat.ts:244-393], then each server matched against the local,
remote and bare ``{"enabled": ...}`` forms, in that order, unknown keys dropped
[code: K/v1/config/config.ts:113, K/v1/config/mcp.ts]. A file with a server
of none of these forms is rejected whole by opencode, which then doesn't start
(or, for a global file, starts without the global folder); its servers are not
listed, the other files' still are. Only the settings an import needs are
checked (``mcp``, ``tools``, ``permission``, agents' ``tools`` and
``permission``, ``experimental.mcp_timeout``): a file opencode rejects for
another setting is still read.

opencode replaces ``{env:NAME}`` and ``{file:path}`` in a file's text before
parsing it [code: C/variable.ts:34-91]. Here a server's values are read after
parsing, and ``{env:NAME}`` becomes iCode's ``{{NAME}}``. opencode reads an
unset variable as empty where iCode fails the connection, so an unset one
gets a warning; opencode can also fill a variable from an organization it is
signed in to, which iCode can't. A ``{file:...}`` in a setting iCode would
import makes the server unimportable: iCode doesn't read the file, and copying
its content would copy a secret out of the file the user keeps it in. A
reference outside a JSON string, such as a timeout written ``{env:T}``, makes
the file invalid JSON here.

Only servers opencode starts are listed: those with a ``type``
[code: M/index.ts:509] and, if remote, a URL it can parse [code: M/index.ts:123].
Servers it has turned off (``enabled: false``) are listed and imported turned
off. A server's settings come from the file that set its ``type`` last; when
other files changed it too, a warning names them. ``timeout`` (milliseconds,
else ``experimental.mcp_timeout``) bounds each call [code: M/index.ts:661-684]
and becomes iCode's request timeout in seconds, rounded up; iCode also waits
at most that long to connect, where opencode waits 30 seconds unless the
server sets ``timeout``. opencode hides the
tools a ``deny`` rule in ``tools`` or ``permission`` matches
[code: C/config.ts:567-578, packages/opencode/src/permission/index.ts:186-218]:
a rule hiding all of a server's tools is imported as an empty tool list, plus
the tools a later rule names exactly (also ``$OPENCODE_PERMISSION``'s rules) —
by the name opencode shows, in which characters other than letters, digits,
``_`` and ``-`` are ``_``, so a tool whose own name has others stays hidden;
a rule iCode can't express that way, or an agent's own rule, is named in a
warning.

Runtime differences an imported server may notice: opencode starts a local
server in ``cwd`` (default: the folder it was started in), iCode always in the
workspace; opencode falls back to SSE when a remote server doesn't speak
Streamable HTTP, and signs in with OAuth when the server asks, iCode does
neither; opencode advertises the folder as a root, iCode advertises none.

When opencode updates: read the release notes after the baseline for ``mcp``,
config loading or variables; re-check the rules here against the cited files
at the new tag, then update the code, the tests and the baseline above.
"""

from __future__ import annotations

import json
import math
import os
import re
import sys
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
    git_root,
    read_source_bytes,
    render_template,
    templates_convertible,
)
from chrys.service.profiles.agents.schema import MCPServerConfig

if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping

SCOPE_GLOBAL = "global"
SCOPE_PROJECT = "project"

_APP = "opencode"
_CONFIG_DIR_NAME = ".opencode"
# In each list, the order opencode merges the files in: the later one wins.
_GLOBAL_FILES = ("config.json", "opencode.json", "opencode.jsonc")
_DIRECTORY_FILES = ("opencode.json", "opencode.jsonc")
# The order opencode looks for them in one folder; it reverses the whole search.
_PROJECT_SEARCH = ("opencode.jsonc", "opencode.json")
_CONTENT_SOURCE = "OPENCODE_CONFIG_CONTENT"

_ENV_REFERENCE_RE = re.compile(r"\{env:([^}]+)\}")
_FILE_REFERENCE_RE = re.compile(r"\{file:[^}]+\}")
# The variable names iCode's ``{{NAME}}`` can hold.
_VARIABLE_NAME_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_URL_SCHEME_RE = re.compile(r"[A-Za-z][A-Za-z0-9+.-]*:")
# Characters opencode keeps in a tool name's server part [code: M/catalog.ts:117-119].
_TOOL_NAME_UNSAFE_RE = re.compile(r"[^a-zA-Z0-9_-]")

_PERMISSION_ACTIONS = frozenset({"ask", "allow", "deny"})
_OAUTH_FIELDS = {
    "clientId": str,
    "clientSecret": str,
    "scope": str,
    "redirectUri": str,
}
_V2_OAUTH_FIELDS = {
    "client_id": "clientId",
    "client_secret": "clientSecret",
    "scope": "scope",
    "callback_port": "callbackPort",
    "redirect_uri": "redirectUri",
}


class _InvalidConfig(Exception):
    """A setting opencode rejects, so it rejects the whole file."""


@dataclass(frozen=True, slots=True)
class _Layer:
    """One config file, or the inline config, in opencode's merge order."""

    source: str
    scope: str
    scope_path: str = ""
    path: Path | None = None
    text: str | None = None


@dataclass(slots=True)
class _Settings:
    """The settings an import needs from one file, as opencode decodes them."""

    mcp: dict[str, dict[str, Any]] = field(default_factory=dict)
    tools: dict[str, bool] = field(default_factory=dict)
    permission: dict[str, Any] = field(default_factory=dict)
    agents: dict[str, dict[str, Any]] = field(default_factory=dict)
    mcp_timeout: int | None = None


@dataclass(slots=True)
class _Server:
    """A server's merged settings and which layer set each of them."""

    entry: dict[str, Any] = field(default_factory=dict)
    layers: list[_Layer] = field(default_factory=list)
    type_layer: _Layer | None = None
    connection_layer: _Layer | None = None
    value_layers: dict[tuple[str, str], _Layer] = field(default_factory=dict)


class OpenCodeSource:
    """Reads the servers of opencode's merged config for the workspace."""

    @property
    def source_id(self) -> str:
        return "opencode"

    @property
    def display_name(self) -> str:
        return "opencode"

    def detect(self, ctx: ImportContext) -> bool:
        if _global_dir(ctx).is_dir():
            return True
        return any(
            layer.text is not None or (layer.path is not None and layer.path.is_file()) for layer in _layers(ctx)
        )

    def discover(self, ctx: ImportContext) -> McpImportResult:
        errors: list[McpSourceError] = []
        loaded: list[tuple[_Layer, _Settings]] = []
        layers = _layers(ctx)
        global_count = len(_GLOBAL_FILES)
        for group in (layers[:global_count], *[[layer] for layer in layers[global_count:]]):
            # The global files are one unit: opencode drops all of them when one fails.
            group_settings: list[tuple[_Layer, _Settings]] = []
            failed = False
            for layer in group:
                try:
                    settings = _read_layer(layer)
                except SourceReadError as exc:
                    errors.append(McpSourceError(layer.source, exc.kind))
                    failed = True
                    continue
                if settings is not None:
                    group_settings.append((layer, settings))
            if not failed:
                loaded.extend(group_settings)
        return _result(ctx, loaded, errors)


def _global_dir(ctx: ImportContext) -> Path:
    base = ctx.environ.get("XDG_CONFIG_HOME") or ""
    # xdg-basedir takes the variable as is when set, a relative path included.
    root = Path(base) if base else ctx.home / ".config"
    return root / "opencode"


def _flag(ctx: ImportContext, name: str) -> bool:
    return ctx.environ.get(name, "").lower() in {"true", "1"}


def _absolute(ctx: ImportContext, value: str) -> Path | None:
    """*value* as opencode opens it: relative to the folder it starts in, for which the workspace stands in."""
    path = Path(value)
    if path.is_absolute():
        return path
    return None if not ctx.workspace_cwd else Path(ctx.workspace_cwd) / path


def _managed_dir(ctx: ImportContext) -> Path:
    override = ctx.environ.get("OPENCODE_TEST_MANAGED_CONFIG_DIR", "")
    if override:
        return Path(override)
    if sys.platform == "darwin":
        return Path("/Library/Application Support/opencode")
    if sys.platform == "win32":
        return Path(ctx.environ.get("ProgramData") or "C:\\ProgramData") / "opencode"
    return Path("/etc/opencode")


def _layers(ctx: ImportContext) -> list[_Layer]:
    """Every config opencode merges for the workspace, lowest precedence first; files may not exist.

    The global folder's files come first, as many as :data:`_GLOBAL_FILES`.
    """
    layers = [_Layer(str(path), SCOPE_GLOBAL, path=path) for path in (_global_dir(ctx) / n for n in _GLOBAL_FILES)]
    custom_file = ctx.environ.get("OPENCODE_CONFIG", "")
    custom_path = _absolute(ctx, custom_file) if custom_file else None
    if custom_path is not None:
        layers.append(_Layer(str(custom_path), SCOPE_GLOBAL, path=custom_path))
    workspace, worktree = _workspace_and_worktree(ctx)
    project_enabled = not _flag(ctx, "OPENCODE_DISABLE_PROJECT_CONFIG")
    if workspace is not None and project_enabled:
        found = [
            _Layer(str(folder / name), SCOPE_PROJECT, str(folder), path=folder / name)
            for folder in _folders_up(workspace, worktree)
            for name in _PROJECT_SEARCH
            if (folder / name).exists()
        ]
        # Found from the workspace up, read from the top down.
        layers.extend(reversed(found))
    layers.extend(_directory_layers(ctx, workspace, worktree, project_enabled=project_enabled))
    content = ctx.environ.get(_CONTENT_SOURCE, "")
    if content:
        layers.append(_Layer(_CONTENT_SOURCE, SCOPE_GLOBAL, text=content))
    managed = _managed_dir(ctx)
    layers.extend(_Layer(str(managed / name), SCOPE_GLOBAL, path=managed / name) for name in _DIRECTORY_FILES)
    return layers


def _workspace_and_worktree(ctx: ImportContext) -> tuple[Path | None, Path | None]:
    """The folder opencode starts in, and the top it searches up to: the Git repository's, else none."""
    if not ctx.workspace_cwd:
        return None, None
    try:
        workspace = Path(os.path.realpath(ctx.workspace_cwd))
    except OSError, ValueError:
        return None, None
    root = git_root(str(workspace))
    return workspace, None if root is None else Path(root)


def _folders_up(start: Path, stop: Path | None) -> list[Path]:
    """*start* and each folder above it, up to *stop* (the filesystem root when None)."""
    folders = [start]
    for parent in start.parents:
        if folders[-1] == stop:
            break
        folders.append(parent)
    return folders


def _directory_layers(
    ctx: ImportContext, workspace: Path | None, worktree: Path | None, *, project_enabled: bool
) -> list[_Layer]:
    home_dir = str(ctx.home / _CONFIG_DIR_NAME)
    config_dir = ctx.environ.get("OPENCODE_CONFIG_DIR", "")
    walked: list[str] = []
    if workspace is not None and project_enabled:
        walked = [
            str(folder / _CONFIG_DIR_NAME)
            for folder in _folders_up(workspace, worktree)
            if (folder / _CONFIG_DIR_NAME).exists()
        ]
    candidates = [str(_global_dir(ctx)), *walked]
    # opencode drops repeated spellings only. The walk here starts from the workspace's real path, so it
    # may reach the home folder's .opencode spelled another way than opencode's walk and home would.
    if Path(home_dir).exists() and _real(home_dir) not in {_real(directory) for directory in walked}:
        candidates.append(home_dir)
    if config_dir:
        candidates.append(config_dir)
    layers: list[_Layer] = []
    for directory in dict.fromkeys(candidates):
        if not (directory.endswith(_CONFIG_DIR_NAME) or directory == config_dir):
            continue
        base = _absolute(ctx, directory)
        if base is None:
            continue
        if directory == config_dir or _real(directory) == _real(home_dir):
            scope, scope_path = SCOPE_GLOBAL, ""
        else:
            scope, scope_path = SCOPE_PROJECT, str(Path(directory).parent)
        layers.extend(_Layer(str(base / name), scope, scope_path, path=base / name) for name in _DIRECTORY_FILES)
    return layers


def _real(path: str) -> str:
    try:
        return os.path.realpath(path)
    except OSError, ValueError:
        return path


def _read_layer(layer: _Layer) -> _Settings | None:
    """The layer's settings; None when its file doesn't exist. Raises :class:`SourceReadError`."""
    if layer.text is not None:
        text = layer.text
    else:
        if layer.path is None:
            return None
        raw = read_source_bytes(layer.path)
        if raw is None:
            return None
        text = decode_source(raw)
    # opencode reads an empty file as an empty config [code: C/config.ts:256].
    if not text:
        return _Settings()
    try:
        document = json.loads(_strip_jsonc(text), parse_constant=_reject_constant)
    except (ValueError, RecursionError) as exc:
        raise SourceReadError(SourceErrorKind.INVALID_JSON) from exc
    if not isinstance(document, dict):
        raise SourceReadError(SourceErrorKind.NOT_AN_OBJECT)
    try:
        return _decode_settings(document)
    except _InvalidConfig as exc:
        raise SourceReadError(SourceErrorKind.INVALID_CONFIG) from exc


def _reject_constant(name: str) -> Any:
    raise ValueError(name)


def _strip_jsonc(text: str) -> str:
    """*text* as plain JSON: comments blanked, a comma right before a closing bracket dropped.

    What jsonc-parser accepts with ``allowTrailingComma`` [code: C/parse.ts:8];
    anything else stays for the JSON parser to reject.
    """
    out: list[str] = []
    trailing_comma: int | None = None
    previous = ""
    index, length = 0, len(text)
    while index < length:
        char = text[index]
        if char == '"':
            end = index + 1
            while end < length and text[end] != '"':
                end += 2 if text[end] == "\\" else 1
            out.append(text[index : end + 1])
            trailing_comma, previous = None, '"'
            index = end + 1
            continue
        if text.startswith("//", index):
            newline = text.find("\n", index)
            index = length if newline == -1 else newline
            continue
        if text.startswith("/*", index):
            end = text.find("*/", index + 2)
            if end == -1:
                raise ValueError("unterminated comment")
            out.append(" ")
            index = end + 2
            continue
        if char in "}]" and trailing_comma is not None:
            out[trailing_comma] = " "
        if char == "," and previous not in {"", "[", "{", ","}:
            trailing_comma = len(out)
        elif not char.isspace():
            trailing_comma = None
        if not char.isspace():
            previous = char
        out.append(char)
        index += 1
    return "".join(out)


def _decode_settings(document: dict[str, Any]) -> _Settings:
    _reject_v2_permissions(document)
    settings = _Settings()
    mcp = document.get("mcp")
    experimental = document.get("experimental")
    timeout_set = isinstance(experimental, dict) and "mcp_timeout" in experimental
    mcp_timeout = experimental.get("mcp_timeout") if isinstance(experimental, dict) else None
    # A key written as null is a wrong type to opencode, not a missing one.
    if "mcp" in document:
        if not isinstance(mcp, dict):
            raise _InvalidConfig
        servers, lowered_timeout = _lower_v2(mcp)
        settings.mcp = {name: _decode_server(entry) for name, entry in servers.items()}
        # A timeout written in experimental is kept over one lowered from mcp [code: C/v2-compat.ts:425-437].
        if not timeout_set:
            mcp_timeout = lowered_timeout
    if timeout_set or mcp_timeout is not None:
        if not isinstance(mcp_timeout, int | float) or not _is_positive_int(mcp_timeout):
            raise _InvalidConfig
        settings.mcp_timeout = int(mcp_timeout)
    if "tools" in document:
        settings.tools = _decode_tools(document["tools"])
    if "permission" in document:
        settings.permission = _decode_permission(document["permission"])
    for key in ("agent", "mode"):
        agents = document.get(key)
        if not isinstance(agents, dict):
            continue
        for name, agent in agents.items():
            if not isinstance(agent, dict):
                continue
            rules: dict[str, Any] = {}
            if isinstance(agent.get("tools"), dict):
                rules["tools"] = _decode_tools(agent["tools"])
            if "permission" in agent:
                rules["permission"] = _decode_permission(agent["permission"])
            if rules:
                settings.agents[name] = rules
    return settings


def _reject_v2_permissions(document: dict[str, Any]) -> None:
    """opencode 2's ``permissions`` fail the file [code: C/v2-compat.ts:92-110]."""
    if "permissions" in document:
        raise _InvalidConfig
    for key in ("agents", "agent", "mode"):
        agents = document.get(key)
        if isinstance(agents, dict) and any(
            isinstance(agent, dict) and "permissions" in agent for agent in agents.values()
        ):
            raise _InvalidConfig


def _decode_tools(value: object) -> dict[str, bool]:
    if not isinstance(value, dict) or not all(isinstance(item, bool) for item in value.values()):
        raise _InvalidConfig
    return dict(value)


def _decode_permission(value: object) -> dict[str, Any]:
    """``permission`` as opencode decodes it: an action for every tool, or one per tool name."""
    if isinstance(value, str):
        if value not in _PERMISSION_ACTIONS:
            raise _InvalidConfig
        return {"*": value}
    if not isinstance(value, dict):
        raise _InvalidConfig
    for rule in value.values():
        if isinstance(rule, str):
            if rule not in _PERMISSION_ACTIONS:
                raise _InvalidConfig
        elif not isinstance(rule, dict) or not all(
            isinstance(action, str) and action in _PERMISSION_ACTIONS for action in rule.values()
        ):
            raise _InvalidConfig
    return dict(value)


def _lower_v2(mcp: dict[str, Any]) -> tuple[dict[str, Any], int | None]:
    """``mcp`` with opencode 2's forms rewritten as opencode 1 reads them [code: C/v2-compat.ts:244-311].

    Returns the servers and the global timeout opencode 2 sets in ``mcp.timeout``.
    """
    nested = mcp.get("servers")
    envelope = isinstance(nested, dict) and not _is_direct_server(nested)
    timeout = mcp.get("timeout")
    global_timeout = (
        isinstance(timeout, dict)
        and not _is_direct_server(timeout)
        and _is_v2_timeout(timeout)
        and (not timeout or any(key in timeout for key in ("startup", "catalog", "execution")))
    )
    servers: dict[str, Any] = {}
    for name, value in mcp.items():
        if (name == "servers" and envelope) or (name == "timeout" and global_timeout):
            continue
        servers[name] = (_lower_v2_server(value) if _is_v2_server(value) else None) or value
    if envelope and isinstance(nested, dict):
        for name, value in nested.items():
            if name in servers:
                continue
            if isinstance(value, dict) and isinstance(value.get("enabled"), bool) and "disabled" not in value:
                servers[name] = value
                continue
            lowered = _lower_v2_server(value)
            if lowered is not None:
                servers[name] = lowered
    lowered_timeout = _lower_timeout(timeout) if global_timeout and isinstance(timeout, dict) else None
    return servers, lowered_timeout


def _is_direct_server(value: dict[str, Any]) -> bool:
    # An object-valued key can be a server literally named "type" or "enabled".
    return any(key in value and (value[key] is None or not isinstance(value[key], dict)) for key in ("type", "enabled"))


def _is_v2_server(value: object) -> bool:
    if not isinstance(value, dict):
        return False
    oauth = value.get("oauth")
    return (
        "disabled" in value
        or "codemode" in value
        or isinstance(value.get("timeout"), dict)
        or (
            isinstance(oauth, dict)
            and any(key in oauth for key in ("client_id", "client_secret", "callback_port", "redirect_uri"))
        )
    )


def _lower_v2_server(value: object) -> dict[str, Any] | None:
    """An opencode 2 server as opencode 1 reads it; None when it doesn't match opencode 2's form either."""
    if not isinstance(value, dict):
        return None
    kind = value.get("type")
    if kind == "local":
        if not _is_str_list(value.get("command")) or not _optional(value, "cwd", str):
            return None
        if "environment" in value and not _is_str_mapping(value["environment"]):
            return None
    elif kind == "remote":
        if not isinstance(value.get("url"), str):
            return None
        if "headers" in value and not _is_str_mapping(value["headers"]):
            return None
        oauth = value.get("oauth")
        if "oauth" in value and oauth is not False and not _is_v2_oauth(oauth):
            return None
    else:
        return None
    if not _optional(value, "disabled", bool) or not _optional(value, "codemode", bool):
        return None
    timeout = value.get("timeout")
    if "timeout" in value and not (isinstance(timeout, dict) and _is_v2_timeout(timeout)):
        return None
    keep = ("type", "command", "cwd", "environment") if kind == "local" else ("type", "url", "headers")
    lowered: dict[str, Any] = {key: value[key] for key in keep if key in value}
    lowered["enabled"] = value["enabled"] if "enabled" in value else value.get("disabled") is not True
    if isinstance(timeout, dict) and (seconds := _lower_timeout(timeout)) is not None:
        lowered["timeout"] = seconds
    if kind == "remote" and "oauth" in value:
        oauth = value["oauth"]
        lowered["oauth"] = (
            False
            if oauth is False
            else {_V2_OAUTH_FIELDS[key]: item for key, item in oauth.items() if key in _V2_OAUTH_FIELDS}
        )
    return lowered


def _is_v2_oauth(value: object) -> bool:
    if not isinstance(value, dict):
        return False
    for key, kind in (("client_id", str), ("client_secret", str), ("scope", str), ("redirect_uri", str)):
        if key in value and not isinstance(value[key], kind):
            return False
    return "callback_port" not in value or _is_port(value["callback_port"])


def _is_v2_timeout(value: dict[str, Any]) -> bool:
    return all(key not in value or _is_positive_int(value[key]) for key in ("startup", "catalog", "execution"))


def _lower_timeout(value: dict[str, Any]) -> int | None:
    """opencode 2's split timeout as opencode 1's single one, when it is one [code: C/v2-compat.ts:362-367]."""
    if "startup" in value or "catalog" not in value or "execution" not in value:
        return None
    return int(value["catalog"]) if value["catalog"] == value["execution"] else None


def _decode_server(entry: object) -> dict[str, Any]:
    """The server as opencode decodes it: local, remote, else a bare on/off switch; known keys only."""
    if not isinstance(entry, dict):
        raise _InvalidConfig
    for decode in (_decode_local, _decode_remote):
        decoded = decode(entry)
        if decoded is not None:
            return decoded
    if isinstance(entry.get("enabled"), bool):
        return {"enabled": entry["enabled"]}
    raise _InvalidConfig


def _decode_local(entry: dict[str, Any]) -> dict[str, Any] | None:
    if entry.get("type") != "local" or not _is_str_list(entry.get("command")):
        return None
    if not _optional(entry, "cwd", str) or not _common_valid(entry):
        return None
    if "environment" in entry and not _is_str_mapping(entry["environment"]):
        return None
    return _pick(entry, ("type", "command", "cwd", "environment", "enabled", "timeout"))


def _decode_remote(entry: dict[str, Any]) -> dict[str, Any] | None:
    if entry.get("type") != "remote" or not isinstance(entry.get("url"), str) or not _common_valid(entry):
        return None
    if "headers" in entry and not _is_str_mapping(entry["headers"]):
        return None
    oauth = entry.get("oauth")
    if "oauth" in entry and oauth is not False and (not isinstance(oauth, dict) or not _oauth_valid(oauth)):
        return None
    return _pick(entry, ("type", "url", "headers", "oauth", "enabled", "timeout"))


def _common_valid(entry: dict[str, Any]) -> bool:
    return _optional(entry, "enabled", bool) and ("timeout" not in entry or _is_positive_int(entry["timeout"]))


def _oauth_valid(oauth: dict[str, Any]) -> bool:
    if any(key in oauth and not isinstance(oauth[key], kind) for key, kind in _OAUTH_FIELDS.items()):
        return False
    return "callbackPort" not in oauth or _is_port(oauth["callbackPort"])


def _pick(entry: dict[str, Any], keys: Iterable[str]) -> dict[str, Any]:
    return {key: entry[key] for key in keys if key in entry}


def _optional(entry: dict[str, Any], key: str, kind: type) -> bool:
    return key not in entry or (
        isinstance(entry[key], kind) and not (kind is not bool and isinstance(entry[key], bool))
    )


def _is_positive_int(value: object) -> bool:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return False
    return math.isfinite(value) and value == int(value) and value > 0


def _is_port(value: object) -> bool:
    return _is_positive_int(value) and isinstance(value, int | float) and value <= 65535


def _is_str_list(value: object) -> bool:
    return isinstance(value, list) and all(isinstance(item, str) for item in value)


def _is_str_mapping(value: object) -> bool:
    return isinstance(value, dict) and all(isinstance(item, str) for item in value.values())


# --- merging ------------------------------------------------------------------


def _merge_deep(target: object, source: object) -> object:
    """remeda's ``mergeDeep``: objects merge key by key, anything else is replaced."""
    if isinstance(target, dict) and isinstance(source, dict):
        merged = dict(target)
        for key, value in source.items():
            merged[key] = _merge_deep(merged[key], value) if key in merged else value
        return merged
    return source


def _merge_servers(loaded: list[tuple[_Layer, _Settings]]) -> dict[str, _Server]:
    servers: dict[str, _Server] = {}
    for layer, settings in loaded:
        for name, entry in settings.mcp.items():
            server = servers.setdefault(name, _Server())
            merged = _merge_deep(server.entry, entry)
            server.entry = merged if isinstance(merged, dict) else {}
            server.layers.append(layer)
            if "type" in entry:
                server.type_layer = layer
            if "command" in entry or "url" in entry:
                server.connection_layer = layer
            for group in ("environment", "headers"):
                values = entry.get(group)
                if isinstance(values, dict):
                    for key in values:
                        server.value_layers[(group, key)] = layer
    return servers


# --- candidates ---------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _Merged:
    tools: dict[str, bool]
    permission: dict[str, Any]
    agents: dict[str, Any]
    mcp_timeout: int | None


def _result(
    ctx: ImportContext, loaded: list[tuple[_Layer, _Settings]], errors: list[McpSourceError]
) -> McpImportResult:
    tools: dict[str, bool] = {}
    permission: dict[str, Any] = {}
    agents: dict[str, Any] = {}
    mcp_timeout: int | None = None
    for _layer, settings in loaded:
        tools = {**tools, **settings.tools}
        merged_permission = _merge_deep(permission, settings.permission)
        permission = merged_permission if isinstance(merged_permission, dict) else {}
        merged_agents = _merge_deep(agents, settings.agents)
        agents = merged_agents if isinstance(merged_agents, dict) else {}
        if settings.mcp_timeout is not None:
            mcp_timeout = settings.mcp_timeout
    permission = _with_permission_env(ctx, permission)
    merged = _Merged(tools, permission, agents, mcp_timeout)
    candidates = []
    for name, server in _merge_servers(loaded).items():
        candidate = _candidate(ctx, name, server, merged)
        if candidate is not None:
            candidates.append(candidate)
    return McpImportResult(candidates=tuple(candidates), source_errors=tuple(errors))


def _with_permission_env(ctx: ImportContext, permission: dict[str, Any]) -> dict[str, Any]:
    """*permission* with ``$OPENCODE_PERMISSION`` merged over it; opencode skips it when it isn't JSON [code: C/config.ts:559-565]."""
    value = ctx.environ.get("OPENCODE_PERMISSION", "")
    if not value:
        return permission
    try:
        extra = json.loads(value)
    except ValueError, RecursionError:
        return permission
    merged = _merge_deep(permission, extra)
    return merged if isinstance(merged, dict) else permission


def _candidate(ctx: ImportContext, name: str, server: _Server, merged: _Merged) -> McpImportCandidate | None:
    entry = server.entry
    layer = server.type_layer
    kind = entry.get("type")
    if layer is None or kind not in {"local", "remote"}:
        # opencode ignores a server no file gave a type [code: M/index.ts:509-511].
        return None
    if kind == "local":
        command = entry.get("command")
        if not _is_str_list(command) or not command or not command[0]:
            return None
        transport = "stdio"
        shown: dict[str, Any] = {"command": command[0], "args": command[1:], "env": entry.get("environment", {})}
    else:
        url = entry.get("url")
        if not isinstance(url, str):
            return None
        # opencode substitutes references before parsing the URL; report them as unsupported below.
        if not (_ENV_REFERENCE_RE.search(url) or _FILE_REFERENCE_RE.search(url)) and not _url_parses(url):
            return None
        transport = "http"
        shown = {"url": url, "headers": entry.get("headers", {})}

    def unsupported(reason: UnsupportedReason) -> McpImportCandidate:
        return McpImportCandidate(
            name,
            layer.scope,
            layer.source,
            None,
            unsupported=reason,
            entry=shown,
            transport=transport,
            scope_path=layer.scope_path,
        )

    if kind == "local":
        connection = [*entry["command"]]
        values_key = "environment"
    else:
        connection = [entry["url"]]
        values_key = "headers"
    values: dict[str, str] = entry.get(values_key, {})
    if any(_FILE_REFERENCE_RE.search(value) for value in (*connection, *values.values())):
        return unsupported(UnsupportedReason.FILE_REFERENCE)
    if any(_ENV_REFERENCE_RE.search(value) for value in connection):
        return unsupported(
            UnsupportedReason.TEMPLATE_IN_COMMAND if kind == "local" else UnsupportedReason.TEMPLATE_IN_URL
        )
    converted: dict[str, str] = {}
    variables: list[tuple[str, str]] = []
    for key, value in values.items():
        parts = _env_parts(value)
        if parts is None:
            return unsupported(UnsupportedReason.VARIABLE_NAME)
        if not templates_convertible(parts):
            return unsupported(UnsupportedReason.TEMPLATE_NEXT_TO_BRACE)
        converted[key] = render_template(parts)
        variables.extend((key, part.name) for part in parts if isinstance(part, TemplateVariable))

    warnings: list[ImportWarning] = [
        ImportWarning(ImportWarningKind.VARIABLE_UNSET, variable, field=key)
        for key, variable in dict.fromkeys(variables)
        if not ctx.environ.get(variable)
    ]
    cwd = entry.get("cwd") if kind == "local" else None
    if isinstance(cwd, str) and cwd not in {"", "."}:
        warnings.append(ImportWarning(ImportWarningKind.CWD_IGNORED, original=cwd))
    if kind == "remote" and isinstance(entry.get("oauth"), dict):
        warnings.append(ImportWarning(ImportWarningKind.OAUTH_IGNORED))
    warnings.extend(_merged_warning(server, values_key))
    allowed_tools, tool_warnings = _tool_access(name, merged)
    warnings.extend(tool_warnings)
    enabled = entry.get("enabled") is not False
    if not enabled:
        warnings.append(ImportWarning(ImportWarningKind.DISABLED))
    timeout = entry.get("timeout") if _is_positive_int(entry.get("timeout")) else merged.mcp_timeout
    request_timeout = None if timeout is None else math.ceil(timeout / 1000)
    # Keep tool_name_prefix empty, matching iCode's manual and Claude imports.
    # opencode v1.19.3 adds <server>_<tool> names automatically; iCode leaves
    # prefix selection to the user when tools collide with builtins or other servers.
    if kind == "local":
        config = MCPServerConfig(
            name=name,
            transport="stdio",
            command=entry["command"][0],
            args=list(entry["command"][1:]),
            env=converted,
            enabled=enabled,
            allowed_tools=allowed_tools,
            request_timeout=request_timeout,
        )
    else:
        config = MCPServerConfig(
            name=name,
            transport="http",
            url=entry["url"],
            headers=converted,
            enabled=enabled,
            allowed_tools=allowed_tools,
            request_timeout=request_timeout,
        )
    return McpImportCandidate(
        name,
        layer.scope,
        layer.source,
        config,
        warnings=tuple(warnings),
        transport=transport,
        scope_path=layer.scope_path,
    )


def _url_parses(url: str) -> bool:
    """Close to ``URL.canParse``: a scheme, no blanks, and a host for http(s) [code: M/index.ts:123-125]."""
    if _URL_SCHEME_RE.match(url) is None or any(char.isspace() for char in url.strip()):
        return False
    try:
        parts = urlsplit(url.strip())
    except ValueError:
        return False
    return parts.scheme.lower() not in {"http", "https"} or bool(parts.hostname)


def _env_parts(value: str) -> list[TemplatePart] | None:
    """*value*'s literal text and ``{env:NAME}`` references; None when a NAME can't be written for iCode."""
    parts: list[TemplatePart] = []
    position = 0
    for match in _ENV_REFERENCE_RE.finditer(value):
        name = match.group(1)
        if _VARIABLE_NAME_RE.fullmatch(name) is None:
            return None
        parts.extend((value[position : match.start()], TemplateVariable(name)))
        position = match.end()
    parts.append(value[position:])
    return parts


def _merged_warning(server: _Server, values_key: str) -> Iterable[ImportWarning]:
    """Which other files changed the server, and the env or header names they gave it."""
    others = [layer.source for layer in dict.fromkeys(server.layers) if layer is not server.type_layer]
    if not others:
        return
    inherited = [
        key
        for (group, key), layer in server.value_layers.items()
        if group == values_key and key in server.entry.get(values_key, {}) and layer is not server.connection_layer
    ]
    yield ImportWarning(ImportWarningKind.MERGED, field=", ".join(inherited), original=", ".join(others))


# --- tool rules ---------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _Rule:
    key: str
    pattern: str
    action: str


def _rules(tools: Mapping[str, bool], permission: Mapping[str, Any]) -> list[_Rule]:
    """opencode's rule list: ``tools`` as allow/deny rules, overlaid by ``permission`` [code: C/config.ts:567-578]."""
    from_tools: dict[str, Any] = {}
    for tool, enabled in tools.items():
        key = "edit" if tool in {"write", "edit", "patch"} else tool
        from_tools[key] = "allow" if enabled else "deny"
    combined = _merge_deep(from_tools, dict(permission)) if from_tools else dict(permission)
    rules: list[_Rule] = []
    for key, value in combined.items() if isinstance(combined, dict) else ():
        if isinstance(value, str):
            rules.append(_Rule(key, "*", value))
        elif isinstance(value, dict):
            rules.extend(_Rule(key, pattern, action) for pattern, action in value.items())
    return rules


def _wildcard(key: str) -> re.Pattern[str]:
    """opencode's ``Wildcard.match`` pattern for *key* [code: K/util/wildcard.ts]."""
    escaped = re.sub(r"[.+^${}()|\[\]\\]", lambda match: "\\" + match.group(0), key.replace("\\", "/"))
    escaped = escaped.replace("*", ".*").replace("?", ".")
    if escaped.endswith(" .*"):
        escaped = escaped[:-3] + "( .*)?"
    # Windows matches case-insensitively; opencode runs on the machine the import runs on.
    flags = re.DOTALL | (re.IGNORECASE if sys.platform == "win32" else 0)
    return re.compile("^" + escaped + "$", flags)


def _matches(name: str, key: str) -> bool:
    return _wildcard(key).match(name.replace("\\", "/")) is not None


def _covers_all(key: str, prefix: str) -> bool:
    """Whether the rule *key* matches every tool name starting with *prefix*.

    A pattern ending in ``*`` that matches the prefix alone matches it followed by anything.
    """
    return key.endswith("*") and _matches(prefix, key)


def _may_match(key: str, prefix: str) -> bool:
    """Whether the rule *key* may match some tool name starting with *prefix*.

    A key without wildcards names one tool; otherwise its text before the first
    wildcard must agree with the prefix as far as both go.
    """
    key, prefix = _fold(key), _fold(prefix)
    wildcard = min((index for index in (key.find("*"), key.find("?")) if index != -1), default=-1)
    if wildcard == -1:
        return key.startswith(prefix)
    head = key[:wildcard]
    return head.startswith(prefix) or prefix.startswith(head)


def _fold(text: str) -> str:
    return text.casefold() if sys.platform == "win32" else text


def _is_exact(key: str) -> bool:
    return "*" not in key and "?" not in key


def _tool_access(name: str, merged: _Merged) -> tuple[list[str] | None, list[ImportWarning]]:
    """The tools iCode should offer, after the rules opencode applies to this server's tool names.

    opencode applies the last rule whose key matches a tool's name, and hides
    the tool when that rule denies it for every pattern
    [code: packages/opencode/src/permission/index.ts:204-219]. Only the last
    rule covering every tool of the server and the rules after it matter.
    """
    prefix = _TOOL_NAME_UNSAFE_RE.sub("_", name) + "_"
    rules = _rules(merged.tools, merged.permission)
    baseline_index = max((index for index, rule in enumerate(rules) if _covers_all(rule.key, prefix)), default=-1)
    hidden_by = ""
    if baseline_index != -1:
        rule = rules[baseline_index]
        if rule.action == "deny" and rule.pattern == "*":
            hidden_by = rule.key
    ignored: list[str] = []
    # Under a hidden baseline, the tools a later rule names exactly, and whether they end up shown.
    named: dict[str, bool] = {}
    for rule in rules[baseline_index + 1 :]:
        if not _may_match(rule.key, prefix):
            continue
        hides = rule.action == "deny" and rule.pattern == "*"
        if hidden_by and _is_exact(rule.key):
            named[rule.key] = not hides
            continue
        if hidden_by:
            for tool in named:
                if _matches(tool, rule.key):
                    named[tool] = not hides
        if hides != bool(hidden_by):
            ignored.append(rule.key)
    for agent, agent_rules in merged.agents.items():
        if not isinstance(agent_rules, dict):
            continue
        agent_list = _rules(agent_rules.get("tools", {}), agent_rules.get("permission", {}))
        ignored.extend(f"{agent}: {rule.key}" for rule in agent_list if _may_match(rule.key, prefix))
    warnings: list[ImportWarning] = []
    allowed: list[str] | None = None
    if hidden_by:
        allowed = [tool[len(prefix) :] for tool, shown in named.items() if shown]
        warnings.append(ImportWarning(ImportWarningKind.TOOLS_LIMITED, field=hidden_by, original=", ".join(allowed)))
    if ignored:
        warnings.append(ImportWarning(ImportWarningKind.TOOL_RULES_IGNORED, original=", ".join(dict.fromkeys(ignored))))
    return allowed, warnings
