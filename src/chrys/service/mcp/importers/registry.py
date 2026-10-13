# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""The agents iCode can import MCP servers from.

Supporting another agent means one importer module implementing
:class:`McpImportSource` and one entry in :data:`IMPORT_SOURCES`.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from chrys.service.mcp.importers.claude_code import ClaudeCodeSource

if TYPE_CHECKING:
    from collections.abc import Sequence

    from chrys.service.mcp.importers.base import ImportContext, McpImportSource

IMPORT_SOURCES: tuple[McpImportSource, ...] = (ClaudeCodeSource(),)


def detected_sources(ctx: ImportContext, sources: Sequence[McpImportSource] | None = None) -> list[McpImportSource]:
    """The sources whose config files exist, in registry order."""
    return [source for source in (IMPORT_SOURCES if sources is None else sources) if source.detect(ctx)]
