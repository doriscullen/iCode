# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Normalize Codex headers without resolving credentials or choosing conflicting aliases.

Codex rust-v0.166.0 ``rmcp-client/src/utils.rs::build_default_headers``
ignores invalid names and static values before inserting them into a HeaderMap.
Its source maps are HashMaps, so differently cased aliases have no stable winner.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum

from chrys.service.mcp.importers.base import trim_header_value

_HEADER_NAME = re.compile(r"[!#$%&'*+\-.^_`|~0-9A-Za-z]+")


class HeaderIgnoreReason(StrEnum):
    """Why Codex would omit a configured header."""

    INVALID_NAME = "invalid_name"
    INVALID_VALUE = "invalid_value"


@dataclass(frozen=True, slots=True)
class IgnoredCodexHeader:
    """A source field Codex ignores, without its possibly secret value."""

    field: str
    reason: HeaderIgnoreReason


@dataclass(frozen=True, slots=True)
class CodexHeaders:
    """Validated static literals and unresolved environment-variable names.

    Static values have their HTTP whitespace trimmed, but are not template-escaped.
    Environment headers override static headers of the same normalized name.
    A caller must reject an entry with ``conflicts`` instead of using a partial
    result. When bearer authorization is selected, both maps omit Authorization;
    the caller adds its bearer reference separately.
    """

    static_headers: dict[str, str]
    env_headers: dict[str, str]
    ignored: tuple[IgnoredCodexHeader, ...]
    conflicts: tuple[str, ...]


def _valid_value(value: str) -> bool:
    # Rust HeaderValue allows HTAB, visible ASCII and UTF-8 bytes, but no other
    # C0 control or DEL. Rust strings cannot contain Python's lone surrogates.
    return all(
        char == "\t" or (ord(char) >= 0x20 and ord(char) != 0x7F and not 0xD800 <= ord(char) <= 0xDFFF)
        for char in value
    )


def normalize_codex_headers(
    static_headers: Mapping[str, str],
    env_headers: Mapping[str, str],
    *,
    bearer_authorization: bool = False,
) -> CodexHeaders:
    """Validate raw headers and report only conflicts surviving source precedence.

    Environment-variable names are returned unchanged for the caller's template
    validation. No current environment values are read. Static aliases are
    equivalent when their server-observed values match after HTTP whitespace
    trimming. An environment reference masks a static conflict because import
    already requires that reference to resolve to a nonempty value. Codex can
    still use its static fallback when the variable is unavailable; the caller's
    optional-variable warning and user guide must explain that separate boundary.
    """
    static_values: dict[str, set[str]] = {}
    env_variables: dict[str, set[str]] = {}
    ignored: list[IgnoredCodexHeader] = []
    for name, value in static_headers.items():
        field = f"http_headers.{name}"
        if _HEADER_NAME.fullmatch(name) is None:
            ignored.append(IgnoredCodexHeader(field, HeaderIgnoreReason.INVALID_NAME))
        elif not _valid_value(value):
            ignored.append(IgnoredCodexHeader(field, HeaderIgnoreReason.INVALID_VALUE))
        else:
            static_values.setdefault(name.lower(), set()).add(trim_header_value(value))
    for name, variable in env_headers.items():
        if _HEADER_NAME.fullmatch(name) is None:
            ignored.append(IgnoredCodexHeader(f"env_http_headers.{name}", HeaderIgnoreReason.INVALID_NAME))
        else:
            env_variables.setdefault(name.lower(), set()).add(variable)

    if bearer_authorization:
        static_values.pop("authorization", None)
        env_variables.pop("authorization", None)
    conflicts = {name for name, values in env_variables.items() if len(values) > 1}
    conflicts.update(name for name, values in static_values.items() if len(values) > 1 and name not in env_variables)
    return CodexHeaders(
        static_headers={name: next(iter(values)) for name, values in static_values.items() if len(values) == 1},
        env_headers={name: next(iter(values)) for name, values in env_variables.items() if len(values) == 1},
        ignored=tuple(ignored),
        conflicts=tuple(sorted(conflicts)),
    )
