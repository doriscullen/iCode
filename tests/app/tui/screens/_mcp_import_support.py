# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Host app, fixtures and pane readers for the MCP import dialog's screen tests."""

from __future__ import annotations

import json
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from textual.app import App
from textual.widgets import Button, Static

from chrys.app.tui.i18n import LocaleController
from chrys.app.tui.screens.agents.import_mcp_dialog import ImportMcpServersDialog, _ImportCard
from chrys.app.tui.screens.agents.panels.mcp import MCPConfigPanel, MCPConnectionCard
from chrys.app.tui.widgets import Checkbox
from chrys.foundation.config.settings import Settings
from chrys.service.mcp.importers import ImportContext, McpImportCandidate, McpImportResult
from chrys.service.profiles.agents.schema import MCPServerConfig
from tests.app.tui.screens._agent_config_support import _DEFAULT_WAIT_TIMEOUT
from tests.support.pilot_barrier import screen_is_settled
from tests.support.waiting import wait_for

if TYPE_CHECKING:
    from textual.app import ComposeResult
    from textual.widget import Widget


class _PanelApp(App):
    locale_controller = LocaleController(Settings(locale="en"))

    def __init__(self, panel: MCPConfigPanel) -> None:
        super().__init__()
        self._panel = panel
        self.notifications: list[str] = []

    def compose(self) -> ComposeResult:
        yield Static("placeholder")
        yield self._panel

    def notify(self, message: str, **kwargs: Any) -> None:  # type: ignore[override]
        self.notifications.append(message)


def _quiet_environ(tmp_path: Path) -> dict[str, str]:
    """An environment in which no agent's config outside *tmp_path* is found, system-wide ones included."""
    return {
        "XDG_CONFIG_HOME": str(tmp_path / "xdg"),
        "OPENCODE_TEST_MANAGED_CONFIG_DIR": str(tmp_path / "opencode-managed"),
        # Otherwise opencode's project search goes up past tmp_path, to the real home folder on Windows.
        "OPENCODE_DISABLE_PROJECT_CONFIG": "1",
    }


def _write_claude_servers(home: Path, servers: dict[str, Any]) -> None:
    (home / ".claude.json").write_text(json.dumps({"mcpServers": servers}), encoding="utf-8")


def _stdio(name: str, command: str = "python", *args: str) -> MCPServerConfig:
    return MCPServerConfig(name=name, transport="stdio", command=command, args=list(args))


def _row(dialog: ImportMcpServersDialog, index: int) -> Checkbox:
    return dialog.query_one(f"#mcp-import-row-{index}", Checkbox)


def _pane_text(dialog: ImportMcpServersDialog) -> str:
    return "\n".join(str(widget.render()) for widget in dialog.query("#mcp-import-servers Static"))


def _hints(dialog: ImportMcpServersDialog) -> list[str]:
    """Each shown hint under a card's title, in pane order."""
    return [str(hint.render()) for hint in dialog.query(".mcp-import-hint") if hint.display]


def _guidance(dialog: ImportMcpServersDialog) -> str:
    """Every hint under a title and every warning in a body, joined: what import would change or can't."""
    return " ".join([*_hints(dialog), *_warning_texts(dialog)])


def _warning_texts(widget: Widget) -> list[str]:
    return [
        " ".join(str(part.render()) for part in group.query(Static))
        for group in widget.query(".mcp-import-warning-group")
    ]


async def _open_dialog(app: App, panel: MCPConfigPanel, pilot) -> ImportMcpServersDialog:
    panel.query_one("#mcp-import-btn", Button).press()
    await wait_for(
        lambda: isinstance(app.screen, ImportMcpServersDialog) and app.screen.is_mounted,
        pilot=pilot,
        description="import dialog is open",
    )
    dialog = app.screen
    if not isinstance(dialog, ImportMcpServersDialog):
        raise AssertionError(f"unexpected screen {dialog!r}")
    return dialog


async def _wait_for_rows(dialog: ImportMcpServersDialog, pilot, count: int) -> list[Checkbox]:
    await wait_for(
        lambda: len(dialog.query(".mcp-import-row")) == count,
        pilot=pilot,
        description=f"{count} import rows are listed",
    )
    # A card's body and grids compose in later passes than its row appears: wait until nothing is pending.
    await wait_for(
        lambda: screen_is_settled(dialog.app, dialog),
        pilot=pilot,
        description="every card has composed its fields",
    )
    return list(dialog.query(".mcp-import-row").results(Checkbox))


async def _import_ticked(dialog: ImportMcpServersDialog, pilot) -> None:
    await pilot.pause()  # let every tick's Changed reach the dialog before Import reads the ticks
    confirm = dialog.query_one("#mcp-import-confirm", Button)
    await wait_for(lambda: not confirm.disabled, pilot=pilot, description="Import is enabled")
    confirm.press()


async def _cards(panel: MCPConfigPanel, pilot, count: int) -> list[MCPConnectionCard]:
    await wait_for(
        lambda: len(panel.query(MCPConnectionCard)) == count,
        pilot=pilot,
        description=f"{count} MCP cards are mounted",
    )
    return list(panel.query(MCPConnectionCard))


type _FieldValue = str | list[tuple[str, str]]


def _field_value(widget: Widget) -> _FieldValue:
    """A single value's text, or the (name, value) rows of an env or headers grid."""
    if widget.has_class("mcp-import-pairs"):
        cells = [str(cell.render()) for cell in widget.children]
        return list(zip(cells[::2], cells[1::2], strict=True))
    return str(widget.render())


def _cards_in(dialog: ImportMcpServersDialog) -> list[tuple[str, list[tuple[str, _FieldValue]], bool]]:
    """Each card's name, its (field label, value) pairs and whether it can be ticked, in pane order."""
    cards = []
    for card in dialog.query(_ImportCard):
        labels = [str(label.render()) for label in card.query(".mcp-import-field-label")]
        values = [_field_value(value) for value in card.query(".mcp-import-field-value, .mcp-import-pairs")]
        cards.append((card.row.server_name, list(zip(labels, values, strict=True)), not card.row.disabled))
    return cards


async def _wait_for_cards(dialog: ImportMcpServersDialog, pilot, names: list[str], description: str) -> None:
    """Wait until the pane lists *names*, as after an agent switch."""
    await wait_for(
        lambda: (
            [name for name, _fields, _tickable in _cards_in(dialog)] == names and screen_is_settled(dialog.app, dialog)
        ),
        pilot=pilot,
        description=description,
    )


def _expand_cards(dialog: ImportMcpServersDialog) -> None:
    """Unfold every card, so body layout (sizes, wrapping) can be asserted."""
    for card in dialog.query(_ImportCard):
        card.set_collapsed(False)


async def _card_text(card: _ImportCard, pilot) -> str:
    """Everything *card* shows once unfolded: a folded card has composed no settings yet."""
    card.set_collapsed(False, scroll_visible=False)
    await wait_for(
        lambda: bool(card.query_one(".config-card-body").children) and screen_is_settled(card.app, card.screen),
        pilot=pilot,
        description="the card composes its settings",
    )
    return "\n".join(str(widget.render()) for widget in card.query(Static))


def _titles(dialog: ImportMcpServersDialog) -> list[str]:
    """Each title's label and its separate status, without layout padding."""
    titles = []
    for title in dialog.query(".mcp-import-title"):
        status = title.query_one(".mcp-import-status")
        titles.append(str(title.render()) + (f"  {status.render()}" if status.display else ""))
    return titles


async def _read_cards(dialog: ImportMcpServersDialog, pilot) -> list[tuple[str, list[tuple[str, _FieldValue]], bool]]:
    """Unfold every card and return ``_cards_in`` once the settings each one composes have composed in turn."""
    _expand_cards(dialog)
    await wait_for(
        lambda: (
            # A card's boxes compose in later passes than the fields above them.
            not any(not section.children for section in dialog.query(".mcp-import-section"))
            and not any(not card.query_one(".config-card-body").children for card in dialog.query(_ImportCard))
            and screen_is_settled(dialog.app, dialog)
        ),
        pilot=pilot,
        description="the unfolded cards compose their settings",
    )
    return _cards_in(dialog)


def _transport(name: str) -> tuple[str, str]:
    return ("Transport", name)


def _source(path: Path | str) -> tuple[str, str]:
    return ("Configuration Source", str(path))


def _scope(agent: str, value: str) -> tuple[str, str]:
    return (f"Scope in {agent}", value)


def _prefix(value: str) -> tuple[str, str]:
    return ("Tool Name Prefix", value)


_CLAUDE_USER = _scope("Claude Code", "user, used in every project")


@dataclass
class _ListedSource:
    """An agent with the given candidates; ``gate``, when set, holds ``discover`` until the test releases it."""

    source_id: str
    display_name: str
    candidates: tuple[McpImportCandidate, ...]
    gate: threading.Event | None = None

    def detect(self, ctx: ImportContext) -> bool:
        return True

    def discover(self, ctx: ImportContext) -> McpImportResult:
        if self.gate is not None and not self.gate.wait(timeout=_DEFAULT_WAIT_TIMEOUT):
            raise TimeoutError("the test never released discover")
        return McpImportResult(candidates=self.candidates)


def _candidate(name: str, scope: str, config: MCPServerConfig) -> McpImportCandidate:
    return McpImportCandidate(name, scope, f"/{scope}", config, transport=config.transport)


def _new_dialog(app: _PanelApp, sources: list[Any], tmp_path: Path) -> ImportMcpServersDialog:
    return ImportMcpServersDialog(
        workspace_cwd=None,
        existing=[],
        locale_controller=app.locale_controller,
        sources=sources,
        context=ImportContext(workspace_cwd=None, home=tmp_path, environ=_quiet_environ(tmp_path)),
    )


def _open_with(
    app: _PanelApp, sources: list[Any], tmp_path: Path, results: list[Any] | None = None
) -> ImportMcpServersDialog:
    dialog = _new_dialog(app, sources, tmp_path)
    app.push_screen(dialog, callback=None if results is None else results.append)
    return dialog
