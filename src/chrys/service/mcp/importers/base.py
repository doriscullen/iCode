# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Contract and shared helpers for importing MCP servers from other agents.

An importer reads another agent's own config files and turns each MCP server
it finds into a candidate :class:`MCPServerConfig`. Importers only read: the
user picks candidates in the UI, and nothing is written until the agent
profile is saved. Results carry codes, never prose, so the UI localizes them.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
from dataclasses import dataclass, field, replace
from datetime import timedelta
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol

from chrys.foundation.platform.files import secure_open_regular_binary
from chrys.foundation.text.encoding import decode_bytes
from chrys.service.mcp.validation import MCP_PROGRESSIVE_CONTROL_TOOL_NAMES, validate_mcp_tool_name_prefix

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

    from chrys.service.profiles.agents.schema import MCPServerConfig

# Agent config files may also hold per-project state, so the cap is generous;
# it only stops an unbounded read of something that is not a config file.
MAX_SOURCE_BYTES = 16 * 1024 * 1024

# Claude Code's placeholder syntax: ``${VAR}`` or ``${VAR:-default}``
# [doc: mcp, "Environment variable expansion in .mcp.json"].
_DOLLAR_TEMPLATE_RE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(:-[^}]*)?\}")


class UnsupportedReason(StrEnum):
    """Why a server the source runs can't be imported."""

    UNSUPPORTED_TRANSPORT = "unsupported_transport"
    TEMPLATE_IN_URL = "template_in_url"
    TEMPLATE_IN_COMMAND = "template_in_command"
    TEMPLATE_NEXT_TO_BRACE = "template_next_to_brace"
    FILE_REFERENCE = "file_reference"
    VALUE_FROM_COMMAND = "value_from_command"
    VARIABLE_NAME = "variable_name"
    REMOTE_ENVIRONMENT = "remote_environment"
    HEADER_NAME_COLLISION = "header_name_collision"
    TEMPLATE_IN_FIELD_NAME = "template_in_field_name"
    HEADER_VALUE_ENCODING = "header_value_encoding"


class ImportWarningKind(StrEnum):
    """Something the conversion changed or left out."""

    DEFAULT_DROPPED = "default_dropped"
    HEADERS_HELPER_IGNORED = "headers_helper_ignored"
    OAUTH_IGNORED = "oauth_ignored"
    CREDENTIAL_VARIABLE = "credential_variable"
    CLAUDE_PROJECT_DIR = "claude_project_dir"
    UNKNOWN_FIELDS_IGNORED = "unknown_fields_ignored"
    CWD_IGNORED = "cwd_ignored"
    MERGED = "merged"
    AUTH_PROVIDER_IGNORED = "auth_provider_ignored"
    TOOL_RULES_IGNORED = "tool_rules_ignored"
    TOOL_PERMISSIONS_IGNORED = "tool_permissions_ignored"
    TOOLS_LIMITED = "tools_limited"
    OPTIONAL_VARIABLE = "optional_variable"
    DENY_LIST = "deny_list"
    LITERAL_BRACES_ESCAPED = "literal_braces_escaped"
    INVALID_HEADER_IGNORED = "invalid_header_ignored"
    ZERO_TIMEOUT_IGNORED = "zero_timeout_ignored"
    STARTUP_TIMEOUT_IGNORED = "startup_timeout_ignored"
    TOOL_OUTPUT_LIMIT_IGNORED = "tool_output_limit_ignored"


class SourceErrorKind(StrEnum):
    """Why a config file that exists could not be read."""

    UNREADABLE = "unreadable"
    TOO_LARGE = "too_large"
    INVALID_JSON = "invalid_json"
    INVALID_TOML = "invalid_toml"
    NOT_AN_OBJECT = "not_an_object"
    INVALID_CONFIG = "invalid_config"
    INVALID_ENCODING = "invalid_encoding"


@dataclass(frozen=True, slots=True)
class ImportWarning:
    """What changed, and where: the setting (env or header name), never its value.

    The UI saves warnings in the imported server's description, and an env or
    header value may hold a secret. ``variable`` names the placeholder whose
    default was dropped or that is unset, the credential variable a header
    references, or the variable the source sets for its servers that iCode
    doesn't. ``field`` names the env or header setting; for ``MERGED`` it lists
    the env and header names that came from another file than the command or
    URL, and for ``TOOLS_LIMITED`` the source setting that hides the tools.
    ``original`` is the helper command that was not imported, the
    comma-separated names of the fields, files or tool rules not imported,
    the working folder or sign-in provider that was dropped, or the tools
    still offered. For ``INVALID_HEADER_IGNORED``, ``field`` is the source
    setting path and ``original`` is the reason code ``name`` or ``value``.
    ``TOOL_OUTPUT_LIMIT_IGNORED`` names the omitted settings without their values.
    """

    kind: ImportWarningKind
    variable: str = ""
    field: str = ""
    original: str = ""


@dataclass(frozen=True, slots=True)
class McpSourceError:
    """Safe source-location metadata without the source line or exception text."""

    path: str
    kind: SourceErrorKind
    field: str = ""
    line: int | None = None
    column: int | None = None


@dataclass(frozen=True, slots=True)
class McpImportCandidate:
    """One server another agent runs, found in its config.

    Entries with invalid server settings are not candidates. Source application
    permissions and project trust do not gate import. ``config`` is set
    exactly when the server can be imported; otherwise ``unsupported`` says why
    not, and ``entry`` keeps the server's settings as the source wrote them so
    the UI can show them, in the keys ``command`` (the program), ``args``,
    ``env``, ``url`` and ``headers``. A ``config.description`` set here is the
    source's own description of the server.
    ``transport`` is the connection type the source declares, or infers when it
    declares none, for importable and unsupported servers alike; empty when
    the entry names none the importer can read. ``scope_path`` is the folder
    the source uses the server in, as the source recorded it; empty when the
    source uses it in every folder, as iCode will once it is imported.
    ``unsupported_field`` and ``unsupported_variable`` identify the source
    setting and variable name behind a refusal; they never contain a value.
    """

    name: str
    scope: str
    source_path: str
    config: MCPServerConfig | None
    unsupported: UnsupportedReason | None = None
    warnings: tuple[ImportWarning, ...] = ()
    entry: Mapping[str, Any] | None = None
    transport: str = ""
    scope_path: str = ""
    # Preview metadata only; the connection uses config.request_timeout in seconds.
    timeout_source_ms: int | None = None
    timeout_source_seconds: int | float | None = None
    unsupported_field: str = ""
    unsupported_variable: str = ""


@dataclass(frozen=True, slots=True)
class McpImportResult:
    candidates: tuple[McpImportCandidate, ...] = ()
    source_errors: tuple[McpSourceError, ...] = ()


@dataclass(frozen=True, slots=True)
class ImportContext:
    """Where an importer looks: the workspace for project files, home and env for user files."""

    workspace_cwd: str | None
    home: Path
    environ: Mapping[str, str] = field(default_factory=dict)

    @classmethod
    def current(cls, workspace_cwd: str | None) -> ImportContext:
        return cls(workspace_cwd=workspace_cwd, home=Path.home(), environ=dict(os.environ))


class McpImportSource(Protocol):
    """An agent whose MCP servers can be imported."""

    @property
    def source_id(self) -> str: ...

    @property
    def display_name(self) -> str:
        """Product name shown as is (not localized)."""
        ...

    def detect(self, ctx: ImportContext) -> bool:
        """Cheap check that this agent has config files to read; never parses them."""
        ...

    def discover(self, ctx: ImportContext) -> McpImportResult:
        """Read the agent's config files; blocking file I/O, run it off the event loop."""
        ...


class SourceReadError(Exception):
    def __init__(
        self,
        kind: SourceErrorKind,
        *,
        field: str = "",
        line: int | None = None,
        column: int | None = None,
    ) -> None:
        super().__init__(kind.value)
        self.kind = kind
        self.field = field
        self.line = line
        self.column = column


def read_source_bytes(path: Path, *, max_bytes: int = MAX_SOURCE_BYTES) -> bytes | None:
    """The bytes of *path*; ``None`` when the file does not exist.

    Raises :class:`SourceReadError` when the file exists but can't be read.
    A link is followed, as the agent that owns the file follows it (dotfile
    managers link these files); a dangling link counts as no file.
    """
    if not os.path.exists(path):
        return None
    try:
        # The leaf is opened no-follow, so open the link's target, still checked to be a regular file.
        with secure_open_regular_binary(Path(os.path.realpath(path))) as handle:
            raw = handle.read(max_bytes + 1)
    except OSError as exc:
        raise SourceReadError(SourceErrorKind.UNREADABLE) from exc
    if len(raw) > max_bytes:
        raise SourceReadError(SourceErrorKind.TOO_LARGE)
    return raw


def decode_source(raw: bytes) -> str:
    """*raw* as text; a file that isn't text can't be a config file."""
    try:
        return decode_bytes(raw, errors="strict")
    except UnicodeDecodeError as exc:
        raise SourceReadError(SourceErrorKind.INVALID_JSON) from exc


def parse_json_object(text: str) -> dict[str, Any]:
    """The JSON object *text* holds; raises :class:`SourceReadError` otherwise."""
    try:
        document = json.loads(text)
    except (ValueError, RecursionError) as exc:
        raise SourceReadError(SourceErrorKind.INVALID_JSON) from exc
    if not isinstance(document, dict):
        raise SourceReadError(SourceErrorKind.NOT_AN_OBJECT)
    return document


def read_json_object(path: Path, *, max_bytes: int = MAX_SOURCE_BYTES) -> dict[str, Any] | None:
    """Read a JSON object from *path*; ``None`` when the file does not exist.

    Raises :class:`SourceReadError` when the file exists but can't be used.
    """
    raw = read_source_bytes(path, max_bytes=max_bytes)
    return None if raw is None else parse_json_object(decode_source(raw))


# The longest request timeout iCode's MCP client can wait, which it holds as a timedelta.
# Whole days: ``timedelta.max.total_seconds()`` rounds up past what it holds.
MAX_REQUEST_TIMEOUT_SECONDS = timedelta.max.days * 86_400


def request_timeout_seconds(seconds: float) -> int:
    """A positive timeout in *seconds* as iCode's request timeout: rounded up, at most what the client can wait.

    An infinite one, a source's "no time limit", is the longest the client can wait.
    """
    # Compared, not passed to math.isinf, which can't take an int too long for a float.
    if seconds == math.inf:
        return MAX_REQUEST_TIMEOUT_SECONDS
    return min(math.ceil(seconds), MAX_REQUEST_TIMEOUT_SECONDS)


# HTTP whitespace, which fetch strips from both ends of a header value before sending it: Claude Code,
# opencode and pi send their headers so. Codex sends the spaces and tabs, which a server doesn't read as
# part of the value [RFC 9110, "Field Values"]. iCode's HTTP client refuses to send a value that starts
# or ends with any of them.
_HTTP_WHITESPACE = " \t\r\n"


def trim_header_value(value: str) -> str:
    """A header value as an importer writes it, without the HTTP whitespace at either end, as servers receive it."""
    return value.strip(_HTTP_WHITESPACE)


# Characters Claude Code and opencode replace with ``_`` when they write a server name into its
# tools' names [code: opencode packages/opencode/src/mcp/catalog.ts:117-119; binary Claude Code 2.1.303].
_TOOL_NAME_UNSAFE_RE = re.compile(r"[^A-Za-z0-9_-]")
# A name with nothing a prefix can keep gets ``mcp_`` and this many hex digits of its hash.
_NAMELESS_PREFIX_HASH_LENGTH = 8
# An imported prefix is at most 24 characters, so ``<prefix>_`` takes at most 25 of an exposed tool
# name's 64 and leaves at least 39 for the tool's own name: a longer one keeps its first 17
# characters, then ``-`` and 6 hex digits of the name's hash (17 + 1 + 6 = 24).
_MAX_IMPORTED_PREFIX_LENGTH = 24
_LONG_PREFIX_HEAD_LENGTH = 17
_LONG_PREFIX_HASH_LENGTH = 6


def with_tool_name_prefix(config: MCPServerConfig) -> MCPServerConfig:
    """*config* with its server name as its tool name prefix, unless it has a prefix already.

    The agents iCode imports from name a server's tools after it (opencode
    ``<server>_<tool>``, Claude Code ``mcp__<server>__<tool>``), so their users
    run several servers with the same tool names; without a prefix, iCode
    fails the agent's start on them. The prefix is the name as opencode writes
    it, every character other than a letter, digit, ``_`` or ``-`` as ``_``
    (two for one beyond U+FFFF, as JavaScript counts), less the ``_`` and ``-``
    at either end, which a prefix can't have; iCode then names the tools
    ``<prefix>_<tool>``, as opencode does. A name with nothing left gets
    ``mcp_`` and a hash of the name, so two such servers still differ. A
    prefix longer than 24 characters keeps its first 17, less any ``_`` and
    ``-`` they end with, then ``-`` and a hash of the name, so the server's
    tool names still fit iCode's 64 characters and two long names with the
    same start still differ. A prefix iCode would still refuse is not set.
    """
    if config.tool_name_prefix:
        return config
    prefix = _TOOL_NAME_UNSAFE_RE.sub(lambda match: "__" if ord(match.group()) > 0xFFFF else "_", config.name)
    prefix = prefix.strip("_-")
    # A lone surrogate, which JSON can hold, has no UTF-8 form of its own.
    digest = hashlib.sha256(config.name.encode("utf-8", "surrogatepass")).hexdigest()
    if len(prefix) > _MAX_IMPORTED_PREFIX_LENGTH:
        head = prefix[:_LONG_PREFIX_HEAD_LENGTH].rstrip("_-")
        prefix = f"{head}-{digest[:_LONG_PREFIX_HASH_LENGTH]}" if head else ""
    if not prefix:
        prefix = f"mcp_{digest[:_NAMELESS_PREFIX_HASH_LENGTH]}"
    generated = MCP_PROGRESSIVE_CONTROL_TOOL_NAMES if config.use_progressive_disclosure else ()
    if validate_mcp_tool_name_prefix(prefix, generated_suffixes=generated) is not None:
        return config
    return replace(config, tool_name_prefix=prefix)


def json_number(value: object) -> float | None:
    """*value* as JavaScript holds a JSON number: a double, infinite for an int too long for one; None if not a number.

    Agents written in JavaScript read config numbers so; converting a long int to a float in Python raises instead.
    """
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    try:
        return float(value)
    except OverflowError:
        return math.inf if value > 0 else -math.inf


def git_root(path: str) -> str | None:
    """The nearest folder at or above *path* holding ``.git``: a directory, or a file in a worktree or submodule."""
    try:
        folder = Path(os.path.realpath(path))
    except OSError, ValueError:
        return None
    for candidate in (folder, *folder.parents):
        if os.path.lexists(candidate / ".git"):
            return str(candidate)
    return None


def has_dollar_template(value: str) -> bool:
    return _DOLLAR_TEMPLATE_RE.search(value) is not None


def dollar_template_variables(value: str) -> list[str]:
    """The variables *value* references, each once, in order of first appearance."""
    return list(dict.fromkeys(match.group(1) for match in _DOLLAR_TEMPLATE_RE.finditer(value)))


def dollar_templates_convertible(value: str, *, read_as_empty: frozenset[str] = frozenset()) -> bool:
    """Whether :func:`convert_dollar_templates` writes *value* so iCode reads it back as the source meant."""
    return templates_convertible(_dollar_parts(value, read_as_empty)[0])


def convert_dollar_templates(
    value: str, *, read_as_empty: frozenset[str] = frozenset()
) -> tuple[str, tuple[ImportWarning, ...]]:
    """Rewrite ``${VAR}`` placeholders as iCode's ``{{VAR}}`` for a field iCode resolves.

    Literal ``{{``/``}}`` are escaped so iCode reads them back unchanged. iCode
    has no default syntax: ``${VAR:-default}`` becomes ``{{VAR}}`` with a
    warning naming the variable. A variable in *read_as_empty*, which the source
    never expands there, becomes nothing, its default included. Its names are
    upper case and matched in any case, as Windows looks variables up.
    """
    parts, warnings = _dollar_parts(value, read_as_empty)
    return render_template(parts), warnings


def _dollar_parts(
    value: str, read_as_empty: frozenset[str] = frozenset()
) -> tuple[list[TemplatePart], tuple[ImportWarning, ...]]:
    parts: list[TemplatePart] = []
    warnings: list[ImportWarning] = []
    position = 0
    for match in _DOLLAR_TEMPLATE_RE.finditer(value):
        parts.append(value[position : match.start()])
        variable = match.group(1)
        position = match.end()
        if variable.upper() in read_as_empty:
            continue
        parts.append(TemplateVariable(variable))
        if match.group(2) is not None:
            warnings.append(ImportWarning(ImportWarningKind.DEFAULT_DROPPED, variable))
    parts.append(value[position:])
    return parts, tuple(warnings)


@dataclass(frozen=True, slots=True)
class TemplateVariable:
    """A reference to an environment variable in a source value, written ``{{name}}`` for iCode."""

    name: str


type TemplatePart = str | TemplateVariable


def templates_convertible(parts: Sequence[TemplatePart]) -> bool:
    """Whether :func:`render_template` writes *parts* so iCode reads them back as the source meant.

    iCode escapes braces only in pairs: one ``{`` left from an odd run right
    before a placeholder joins its ``{{``, and a ``}}`` right after one is read
    together with its ``}}``. Neither can be written another way.
    """
    merged = _merge_literals(parts)
    for index, part in enumerate(merged):
        if not isinstance(part, TemplateVariable):
            continue
        before = _literal_at(merged, index - 1)
        after = _literal_at(merged, index + 1)
        if (len(before) - len(before.rstrip("{"))) % 2 or after.startswith("}}"):
            return False
    return True


def render_template(parts: Sequence[TemplatePart]) -> str:
    """*parts* as an iCode value: variables as ``{{NAME}}``, literal ``{{``/``}}`` escaped."""
    return "".join(
        "{{" + part.name + "}}" if isinstance(part, TemplateVariable) else _escape_braces(part)
        for part in _merge_literals(parts)
    )


def _literal_at(parts: Sequence[TemplatePart], index: int) -> str:
    part = parts[index] if 0 <= index < len(parts) else ""
    return part if isinstance(part, str) else ""


def _merge_literals(parts: Sequence[TemplatePart]) -> list[TemplatePart]:
    merged: list[TemplatePart] = []
    for part in parts:
        if isinstance(part, str) and merged and isinstance(merged[-1], str):
            merged[-1] += part
        elif part != "":
            merged.append(part)
    return merged


def _escape_braces(text: str) -> str:
    return text.replace("{{", "{{{{").replace("}}", "}}}}")
