# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""The agents iCode can import MCP servers from.

Supporting another agent means one importer module implementing
:class:`McpImportSource` and one entry in :data:`IMPORT_SOURCES`.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from chrys.service.mcp.importers.claude_code import ClaudeCodeSource
from chrys.service.mcp.importers.codex import CodexSource
from chrys.service.mcp.importers.opencode import OpenCodeSource
from chrys.service.mcp.importers.pi import PiSource

if TYPE_CHECKING:
    from collections.abc import Sequence

    from chrys.service.mcp.importers.base import ImportContext, McpImportSource

logger = logging.getLogger(__name__)

IMPORT_SOURCES: tuple[McpImportSource, ...] = (ClaudeCodeSource(), CodexSource(), OpenCodeSource(), PiSource())


def detected_sources(ctx: ImportContext, sources: Sequence[McpImportSource] | None = None) -> list[McpImportSource]:
    """The sources whose config files exist, in registry order.

    A source whose check fails is logged and left out, so the others are still offered.
    """
    found: list[McpImportSource] = []
    for source in IMPORT_SOURCES if sources is None else sources:
        try:
            if source.detect(ctx):
                found.append(source)
        except Exception:
            logger.exception("Looking for %s's MCP configuration failed", source.display_name)
    return found
