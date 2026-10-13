# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Contract and shared helpers for importing MCP servers from other agents.

An importer reads another agent's own config files and turns each MCP server
it finds into a candidate :class:`MCPServerConfig`. Importers only read: the
user picks candidates in the UI, and nothing is written until the agent
profile is saved. Results carry codes, never prose, so the UI localizes them.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol

from chrys.foundation.platform.files import secure_open_regular_binary
from chrys.foundation.text.encoding import decode_bytes

if TYPE_CHECKING:
    from collections.abc import Mapping

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


class ImportWarningKind(StrEnum):
    """Something the conversion changed or left out."""

    DEFAULT_DROPPED = "default_dropped"
    HEADERS_HELPER_IGNORED = "headers_helper_ignored"
    OAUTH_IGNORED = "oauth_ignored"
    CREDENTIAL_VARIABLE = "credential_variable"
    CLAUDE_PROJECT_DIR = "claude_project_dir"
    UNKNOWN_FIELDS_IGNORED = "unknown_fields_ignored"


class SourceErrorKind(StrEnum):
    """Why a config file that exists could not be read."""

    UNREADABLE = "unreadable"
    TOO_LARGE = "too_large"
    INVALID_JSON = "invalid_json"
    NOT_AN_OBJECT = "not_an_object"


@dataclass(frozen=True, slots=True)
class ImportWarning:
    """What changed, and where: the setting (env or header name), never its value.

    The UI saves warnings in the imported server's description, and an env or
    header value may hold a secret. ``variable`` names the placeholder whose
    default was dropped, the credential variable a header references, or the
    variable the source sets for its servers that iCode doesn't; ``original``
    is the helper command that was not imported, or the comma-separated names
    of the fields that were not imported.
    """

    kind: ImportWarningKind
    variable: str = ""
    field: str = ""
    original: str = ""


@dataclass(frozen=True, slots=True)
class McpSourceError:
    path: str
    kind: SourceErrorKind


@dataclass(frozen=True, slots=True)
class McpImportCandidate:
    """One server another agent runs, found in its config.

    Entries the agent itself skips are not candidates. ``config`` is set
    exactly when the server can be imported; otherwise ``unsupported`` says why
    not, and ``entry`` keeps the server's settings as the source wrote them so
    the UI can show them.
    ``transport`` is the connection type the source declares, or infers when it
    declares none, for importable and unsupported servers alike; empty when
    the entry names none the importer can read. ``scope_path`` is the folder
    the source uses the server in, as the source recorded it; empty when the
    source uses it in every folder, as iCode will once it is imported.
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
    def __init__(self, kind: SourceErrorKind) -> None:
        super().__init__(kind.value)
        self.kind = kind


def read_json_object(path: Path, *, max_bytes: int = MAX_SOURCE_BYTES) -> dict[str, Any] | None:
    """Read a JSON object from *path*; ``None`` when the file does not exist.

    Raises :class:`SourceReadError` when the file exists but can't be used.
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
    try:
        document = json.loads(decode_bytes(raw, errors="strict"))
    except (UnicodeDecodeError, ValueError, RecursionError) as exc:
        raise SourceReadError(SourceErrorKind.INVALID_JSON) from exc
    if not isinstance(document, dict):
        raise SourceReadError(SourceErrorKind.NOT_AN_OBJECT)
    return document


def has_dollar_template(value: str) -> bool:
    return _DOLLAR_TEMPLATE_RE.search(value) is not None


def dollar_template_variables(value: str) -> list[str]:
    """The variables *value* references, each once, in order of first appearance."""
    return list(dict.fromkeys(match.group(1) for match in _DOLLAR_TEMPLATE_RE.finditer(value)))


def dollar_templates_convertible(value: str) -> bool:
    """Whether :func:`convert_dollar_templates` writes *value* so iCode reads it back as the source meant.

    iCode escapes braces only in pairs: one ``{`` left from an odd run right
    before a placeholder joins its ``{{``, and a ``}}`` right after one is read
    together with its ``}}``. Neither can be written another way.
    """
    position = 0
    for match in _DOLLAR_TEMPLATE_RE.finditer(value):
        before = value[position : match.start()]
        if (len(before) - len(before.rstrip("{"))) % 2 or value.startswith("}}", match.end()):
            return False
        position = match.end()
    return True


def convert_dollar_templates(value: str) -> tuple[str, tuple[ImportWarning, ...]]:
    """Rewrite ``${VAR}`` placeholders as iCode's ``{{VAR}}`` for a field iCode resolves.

    Literal ``{{``/``}}`` are escaped so iCode reads them back unchanged. iCode
    has no default syntax: ``${VAR:-default}`` becomes ``{{VAR}}`` with a
    warning naming the variable.
    """
    parts: list[str] = []
    warnings: list[ImportWarning] = []
    position = 0
    for match in _DOLLAR_TEMPLATE_RE.finditer(value):
        parts.append(_escape_braces(value[position : match.start()]))
        variable = match.group(1)
        parts.append("{{" + variable + "}}")
        if match.group(2) is not None:
            warnings.append(ImportWarning(ImportWarningKind.DEFAULT_DROPPED, variable))
        position = match.end()
    parts.append(_escape_braces(value[position:]))
    return "".join(parts), tuple(warnings)


def _escape_braces(text: str) -> str:
    return text.replace("{{", "{{{{").replace("}}", "}}}}")
