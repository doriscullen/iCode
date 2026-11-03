# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Import MCP servers configured in opencode.

Baseline: opencode 1.19.5 (tag ``v1.19.5`` of anomalyco/opencode, commit
810c0eba1a, 2026-10-28), the release ``npm install opencode-ai`` installs and
the one the repository marks as latest. opencode 2, a rewrite released from
the same repository as the npm package ``@opencode/cli`` (``v2.x`` tags), is
not followed. Each rule is
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
listed, the other files' still are. Only MCP settings and
``experimental.mcp_timeout`` are validated. Permissions and agent policies are
not validated or migrated; global permission setting names are reported.

opencode replaces ``{env:NAME}`` and ``{file:path}`` in a file's text before
parsing it [code: C/variable.ts:34-91]. Here a server's values are read after
parsing, and ``{env:NAME}`` becomes iCode's ``{{NAME}}``. opencode reads an
unset variable as empty where iCode fails the connection; variable availability
is checked when iCode connects, not during import. opencode can also fill a
variable from an organization it is signed in to, which iCode can't. A
``{file:...}`` in a setting iCode would import makes the server unimportable:
iCode doesn't read the file, and copying
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
server sets ``timeout``. Without either request timeout setting, opencode's
MCP SDK defaults tool calls to 60 seconds; import keeps that request limit,
so iCode also uses 60 seconds for its connection. Global ``tools``,
``permission``, ``permissions`` and
``$OPENCODE_PERMISSION`` settings are reported as omitted. Their rules and
precedence are not reproduced; users configure tool access and approval in iCode.

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
    json_number,
    read_source_bytes,
    render_template,
    request_timeout_seconds,
    templates_convertible,
    trim_header_value,
)
from chrys.service.profiles.agents.schema import MCPServerConfig

if TYPE_CHECKING:
    from collections.abc import Iterable

SCOPE_GLOBAL = "global"
SCOPE_PROJECT = "project"

_APP = "opencode"
# SDK 1.29.0's Protocol.request fallback when convertTool passes an undefined timeout.
_DEFAULT_REQUEST_TIMEOUT_SECONDS = 60
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

    def __init__(self, field: str = "") -> None:
        super().__init__(field)
        self.field = field


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
    permission_fields: tuple[str, ...] = ()
    mcp_timeout: int | None = None


@dataclass(slots=True)
class _Server:
    """A server's merged settings and which layer set each of them."""

    entry: dict[str, Any] = field(default_factory=dict)
    layers: list[_Layer] = field(default_factory=list)
    type_layer: _Layer | None = None
    connection_layer: _Layer | None = None
    value_layers: dict[tuple[str, str], _Layer] = field(default_factory=dict)
    dynamic_field: str = ""
    dynamic_layer: _Layer | None = None
    dynamic_layers: dict[str, _Layer] = field(default_factory=dict)


class OpenCodeSource:
    """Reads the servers of opencode's merged config for the workspace."""

    @property
    def source_id(self) -> str:
        return "opencode"

    @property
    def display_name(self) -> str:
        return "opencode"

    def detect(self, ctx: ImportContext) -> bool:
        # An empty global folder (which opencode creates on start) holds no servers, so it is not
        # a sign that anything can be imported; the global files and the other layers are.
        return any(
            layer.text is not None or (layer.path is not None and layer.path.is_file()) for layer in _layers(ctx)
        )

    def discover(self, ctx: ImportContext) -> McpImportResult:
        errors: list[McpSourceError] = []
        loaded: list[tuple[_Layer, _Settings]] = []
        layers = _layers(ctx)
        global_count = len(_GLOBAL_FILES) if _global_dir(ctx) is not None else 0
        for group in (layers[:global_count], *[[layer] for layer in layers[global_count:]]):
            # The global files are one unit: opencode drops all of them when one fails.
            group_settings: list[tuple[_Layer, _Settings]] = []
            failed = False
            for layer in group:
                try:
                    settings = _read_layer(layer)
                except SourceReadError as exc:
                    errors.append(McpSourceError(layer.source, exc.kind, exc.field, exc.line, exc.column))
                    failed = True
                    continue
                if settings is not None:
                    group_settings.append((layer, settings))
            if not failed:
                loaded.extend(group_settings)
        return _result(ctx, loaded, errors)


def _global_dir(ctx: ImportContext) -> Path | None:
    base = ctx.environ.get("XDG_CONFIG_HOME") or ""
    # xdg-basedir takes the variable as is when set, a relative path included.
    root = _absolute(ctx, base) if base else ctx.home / ".config"
    return None if root is None else root / "opencode"


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

    When the global folder can be resolved, its files come first, as many as :data:`_GLOBAL_FILES`.
    """
    global_dir = _global_dir(ctx)
    layers = (
        [_Layer(str(path), SCOPE_GLOBAL, path=path) for path in (global_dir / n for n in _GLOBAL_FILES)]
        if global_dir is not None
        else []
    )
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
    global_dir = _global_dir(ctx)
    candidates = [*([str(global_dir)] if global_dir is not None else []), *walked]
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
    except json.JSONDecodeError as exc:
        raise SourceReadError(SourceErrorKind.INVALID_JSON, line=exc.lineno, column=exc.colno) from exc
    except (ValueError, RecursionError) as exc:
        raise SourceReadError(SourceErrorKind.INVALID_JSON) from exc
    if not isinstance(document, dict):
        raise SourceReadError(SourceErrorKind.NOT_AN_OBJECT)
    try:
        return _decode_settings(document)
    except _InvalidConfig as exc:
        raise SourceReadError(SourceErrorKind.INVALID_CONFIG, field=exc.field) from exc


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
            end = length if newline == -1 else newline
            out.append(" " * (end - index))
            index = end
            continue
        if text.startswith("/*", index):
            end = text.find("*/", index + 2)
            if end == -1:
                raise json.JSONDecodeError("Unterminated comment", text, index)
            out.append("".join(char if char in "\r\n" else " " for char in text[index : end + 2]))
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
    settings = _Settings()
    mcp = document.get("mcp")
    experimental = document.get("experimental")
    timeout_set = isinstance(experimental, dict) and "mcp_timeout" in experimental
    mcp_timeout = experimental.get("mcp_timeout") if isinstance(experimental, dict) else None
    # A key written as null is a wrong type to opencode, not a missing one.
    if "mcp" in document:
        if not isinstance(mcp, dict):
            raise _InvalidConfig("mcp")
        servers, lowered_timeout = _lower_v2(mcp)
        for name, entry in servers.items():
            dynamic_field = f"mcp.{name}" if _has_name_reference(name) else _dynamic_setting_key(entry, f"mcp.{name}")
            if dynamic_field and isinstance(entry, dict):
                # Text substitution can turn an unknown key into a required connection setting.
                # Its source schema cannot be checked until that name has been expanded.
                settings.mcp[name] = entry
                continue
            try:
                settings.mcp[name] = _decode_server(entry)
            except _InvalidConfig as exc:
                raise _InvalidConfig(f"mcp.{name}") from exc
        # A timeout written in experimental is kept over one lowered from mcp [code: C/v2-compat.ts:425-437].
        if not timeout_set:
            mcp_timeout = lowered_timeout
    if timeout_set or mcp_timeout is not None:
        if not isinstance(mcp_timeout, int | float) or not _is_positive_int(mcp_timeout):
            raise _InvalidConfig("experimental.mcp_timeout" if timeout_set else "mcp.timeout")
        settings.mcp_timeout = int(mcp_timeout)
    settings.permission_fields = tuple(key for key in ("tools", "permission", "permissions") if key in document)
    return settings


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
    # Keep dynamic names for the unsupported preview even when V2 lowering drops unknown keys.
    preserved = _merge_deep(lowered, _dynamic_key_shape(value))
    return preserved if isinstance(preserved, dict) else lowered


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
    number = json_number(value)
    return number is not None and math.isfinite(number) and number.is_integer() and number > 0


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
            dynamic_paths = (
                (f"mcp.{name}",) if _has_name_reference(name) else _dynamic_setting_keys(entry, f"mcp.{name}")
            )
            for dynamic_path in dynamic_paths:
                server.dynamic_layers[dynamic_path] = layer
            if "type" in entry:
                server.type_layer = layer
            if "command" in entry or "url" in entry:
                server.connection_layer = layer
            for group in ("environment", "headers"):
                values = entry.get(group)
                if isinstance(values, dict):
                    for key in values:
                        server.value_layers[(group, key)] = layer
    for name, server in servers.items():
        server.dynamic_field = (
            f"mcp.{name}" if _has_name_reference(name) else _dynamic_setting_key(server.entry, f"mcp.{name}")
        )
        server.dynamic_layer = server.dynamic_layers.get(server.dynamic_field)
    return servers


# --- candidates ---------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _Merged:
    permission_fields: tuple[str, ...]
    mcp_timeout: int | None


def _result(
    ctx: ImportContext, loaded: list[tuple[_Layer, _Settings]], errors: list[McpSourceError]
) -> McpImportResult:
    permission_fields: set[str] = set()
    mcp_timeout: int | None = None
    for _layer, settings in loaded:
        permission_fields.update(settings.permission_fields)
        if settings.mcp_timeout is not None:
            mcp_timeout = settings.mcp_timeout
    if ctx.environ.get("OPENCODE_PERMISSION"):
        permission_fields.add("OPENCODE_PERMISSION")
    merged = _Merged(tuple(sorted(permission_fields)), mcp_timeout)
    candidates = []
    for name, server in _merge_servers(loaded).items():
        candidate = _candidate(name, server, merged, ctx.workspace_cwd)
        if candidate is not None:
            candidates.append(candidate)
    return McpImportResult(candidates=tuple(candidates), source_errors=tuple(errors))


def _candidate(name: str, server: _Server, merged: _Merged, workspace_cwd: str | None) -> McpImportCandidate | None:
    entry = server.entry
    layer = server.type_layer
    kind = entry.get("type")
    if server.dynamic_layer is not None:
        layer = server.dynamic_layer
        shown: dict[str, Any] = {}
        command = entry.get("command")
        if _is_str_list(command) and command:
            shown.update(command=command[0], args=command[1:])
        for source_key, preview_key in (("environment", "env"), ("url", "url"), ("headers", "headers")):
            if source_key in entry:
                shown[preview_key] = entry[source_key]
        return McpImportCandidate(
            name,
            layer.scope,
            layer.source,
            None,
            unsupported=UnsupportedReason.TEMPLATE_IN_FIELD_NAME,
            entry=shown,
            transport="stdio" if kind == "local" else "http" if kind == "remote" else "",
            scope_path=layer.scope_path,
            unsupported_field=server.dynamic_field,
        )
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

    def unsupported(reason: UnsupportedReason, field: str, variable: str = "") -> McpImportCandidate:
        return McpImportCandidate(
            name,
            layer.scope,
            layer.source,
            None,
            unsupported=reason,
            entry=shown,
            transport=transport,
            scope_path=layer.scope_path,
            unsupported_field=field,
            unsupported_variable=variable,
        )

    if _has_name_reference(name):
        return unsupported(UnsupportedReason.TEMPLATE_IN_FIELD_NAME, f"mcp.{name}")
    if kind == "local":
        connection = [(f"command[{index}]", value) for index, value in enumerate(entry["command"])]
        values_key = "environment"
    else:
        connection = [("url", entry["url"])]
        values_key = "headers"
    values: dict[str, str] = entry.get(values_key, {})
    for key in values:
        if _has_name_reference(key):
            return unsupported(UnsupportedReason.TEMPLATE_IN_FIELD_NAME, f"mcp.{name}.{values_key}.{key}")
    fields = [*connection, *((f"{values_key}.{key}", value) for key, value in values.items())]
    for source_field, value in fields:
        if _FILE_REFERENCE_RE.search(value):
            return unsupported(UnsupportedReason.FILE_REFERENCE, f"mcp.{name}.{source_field}")
    for source_field, value in connection:
        if _ENV_REFERENCE_RE.search(value):
            return unsupported(
                UnsupportedReason.TEMPLATE_IN_COMMAND if kind == "local" else UnsupportedReason.TEMPLATE_IN_URL,
                f"mcp.{name}.{source_field}",
            )
    converted: dict[str, str] = {}
    header_names: dict[str, str] = {}
    escaped_braces = False
    for key, value in values.items():
        source_field = f"mcp.{name}.{values_key}.{key}"
        parts = _env_parts(value)
        if parts is None:
            invalid = next(
                match.group(1)
                for match in _ENV_REFERENCE_RE.finditer(value)
                if _VARIABLE_NAME_RE.fullmatch(match.group(1)) is None
            )
            return unsupported(UnsupportedReason.VARIABLE_NAME, source_field, invalid)
        if not templates_convertible(parts):
            return unsupported(UnsupportedReason.TEMPLATE_NEXT_TO_BRACE, source_field)
        escaped_braces |= any(isinstance(part, str) and ("{{" in part or "}}" in part) for part in parts)
        rendered = render_template(parts)
        # opencode's fetch trims a header value; iCode's HTTP client refuses one it doesn't.
        if values_key == "headers":
            rendered = trim_header_value(rendered)
            first_name = header_names.setdefault(key.lower(), key)
            # Web Headers combines case aliases in insertion order before sending them.
            converted[first_name] = f"{converted[first_name]}, {rendered}" if first_name in converted else rendered
        else:
            converted[key] = rendered
    if values_key == "headers":
        # Fetch trims once more after joining aliases; intermediate empty values keep their spaces.
        converted = {key: trim_header_value(value) for key, value in converted.items()}

    warnings: list[ImportWarning] = []
    if escaped_braces:
        warnings.append(ImportWarning(ImportWarningKind.LITERAL_BRACES_ESCAPED))
    cwd = entry.get("cwd") if kind == "local" else None
    if isinstance(cwd, str) and cwd not in {"", "."}:
        warnings.append(
            ImportWarning(
                ImportWarningKind.CWD_IGNORED,
                field=(workspace_cwd or "") if not Path(cwd).is_absolute() else "",
                original=cwd,
            )
        )
    if kind == "remote" and entry.get("oauth") is not False:
        warnings.append(ImportWarning(ImportWarningKind.OAUTH_IGNORED))
    warnings.extend(_merged_warning(server, values_key))
    if merged.permission_fields:
        warnings.append(
            ImportWarning(ImportWarningKind.TOOL_PERMISSIONS_IGNORED, original=", ".join(merged.permission_fields))
        )
    enabled = entry.get("enabled") is not False
    timeout = entry.get("timeout") if _is_positive_int(entry.get("timeout")) else merged.mcp_timeout
    request_timeout = _DEFAULT_REQUEST_TIMEOUT_SECONDS if timeout is None else request_timeout_seconds(timeout / 1000)
    # The source's own config, no tool name prefix: the dialog gives every import one
    # (base.with_tool_name_prefix), the name opencode puts before its tools.
    if kind == "local":
        config = MCPServerConfig(
            name=name,
            transport="stdio",
            command=entry["command"][0],
            args=list(entry["command"][1:]),
            env=converted,
            enabled=enabled,
            request_timeout=request_timeout,
        )
    else:
        config = MCPServerConfig(
            name=name,
            transport="http",
            url=_normalize_url(entry["url"]),
            headers=converted,
            enabled=enabled,
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
        timeout_source_ms=timeout if timeout is not None and request_timeout * 1000 != timeout else None,
    )


def _normalize_url(url: str) -> str:
    """The common WHATWG whitespace cleanup also applied by opencode's URL constructor."""
    return url.strip("".join(chr(code) for code in range(33))).replace("\t", "").replace("\r", "").replace("\n", "")


def _has_name_reference(value: str) -> bool:
    return bool(_ENV_REFERENCE_RE.search(value) or _FILE_REFERENCE_RE.search(value))


def _dynamic_setting_key(value: object, path: str) -> str:
    return next(iter(_dynamic_setting_keys(value, path)), "")


def _dynamic_setting_keys(value: object, path: str) -> Iterable[str]:
    if not isinstance(value, dict):
        return
    for key, item in value.items():
        field = f"{path}.{key}"
        if _has_name_reference(key):
            yield field
        else:
            yield from _dynamic_setting_keys(item, field)


def _dynamic_key_shape(value: dict[str, Any]) -> dict[str, Any]:
    kept: dict[str, Any] = {}
    for key, item in value.items():
        if _has_name_reference(key):
            kept[key] = item
        elif isinstance(item, dict) and (nested := _dynamic_key_shape(item)):
            kept[key] = nested
    return kept


def _url_parses(url: str) -> bool:
    """Recognize ordinary URLs opencode accepts, including spaces in paths and queries."""
    url = _normalize_url(url)
    if _URL_SCHEME_RE.match(url) is None:
        return False
    try:
        parts = urlsplit(url)
        hostname = parts.hostname
    except ValueError:
        return False
    if parts.scheme.lower() not in {"http", "https"}:
        return True
    return bool(hostname) and not any(char.isspace() or ord(char) < 32 for char in hostname or "")


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
