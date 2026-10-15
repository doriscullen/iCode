# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""The MCP tab imports servers configured in other agents as unsaved cards."""

from __future__ import annotations

import json
import logging
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest
from textual.app import App, ComposeResult
from textual.widgets import Button, OptionList, Static

from chrys.app.tui.i18n import LocaleController
from chrys.app.tui.screens.agents.import_mcp_dialog import (
    _DESCRIPTION,
    _DESCRIPTION_SCOPED,
    _NO_AGENTS,
    _SCOPED_NOTICE,
    _UNSUPPORTED_TRANSPORT,
    ImportMcpServersDialog,
    _ImportCard,
    _source_error,
    is_already_added,
)
from chrys.app.tui.screens.agents.panels.mcp import MCPConfigPanel, MCPConnectionCard, format_mcp_command_line
from chrys.app.tui.widgets import Checkbox
from chrys.foundation.config.settings import Settings
from chrys.foundation.i18n import Localizer
from chrys.foundation.i18n.formatting import format_message
from chrys.service.mcp.importers import (
    ImportContext,
    ImportWarning,
    ImportWarningKind,
    McpImportCandidate,
    McpImportResult,
    McpSourceError,
    SourceErrorKind,
    UnsupportedReason,
)
from chrys.service.profiles.agents.schema import MCPServerConfig, ToolsConfig
from tests.app.tui.screens._agent_config_support import (
    _DEFAULT_WAIT_TIMEOUT,
    make_profile,
    open_agent_config,
    press_and_answer_confirm,
    registry_with,
)
from tests.support.tui_helpers import click_when_settled
from tests.support.waiting import wait_for

if TYPE_CHECKING:
    from textual.widget import Widget

pytestmark = [pytest.mark.timeout(120), pytest.mark.usefixtures("isolated_chrys_config_dir")]

_USER_ORIGIN = "Imported from Claude Code. There it is a user server, used in every project."

_CLAUDE_SERVERS: dict[str, Any] = {
    "github": {"command": "npx", "args": ["-y", "server-github"], "env": {"TOKEN": "${GITHUB_TOKEN}"}},
    "remote": {"type": "http", "url": "https://example.com/mcp", "headers": {"Authorization": "Bearer ${TOKEN}"}},
    "legacy": {"type": "sse", "url": "https://example.com/sse"},
    "templated": {"type": "http", "url": "https://${HOST}/mcp"},
}


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


@pytest.fixture
def claude_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point every agent's config dir (and home) at an empty temp dir."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(home))
    for name, value in _quiet_environ(tmp_path).items():
        monkeypatch.setenv(name, value)
    for name in ("OPENCODE_CONFIG", "OPENCODE_CONFIG_DIR", "OPENCODE_CONFIG_CONTENT", "PI_CODING_AGENT_DIR"):
        monkeypatch.setenv(name, "")
    return home


def _write_claude_servers(home: Path, servers: dict[str, Any]) -> None:
    (home / ".claude.json").write_text(json.dumps({"mcpServers": servers}), encoding="utf-8")


def _stdio(name: str, command: str = "python", *args: str) -> MCPServerConfig:
    return MCPServerConfig(name=name, transport="stdio", command=command, args=list(args))


def _row(dialog: ImportMcpServersDialog, index: int) -> Checkbox:
    return dialog.query_one(f"#mcp-import-row-{index}", Checkbox)


def _pane_text(dialog: ImportMcpServersDialog) -> str:
    return "\n".join(str(widget.render()) for widget in dialog.query("#mcp-import-servers Static"))


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


def test_already_added_compares_connections_not_names() -> None:
    existing = [
        _stdio("fs", "uvx", "mcp-fs"),
        MCPServerConfig(name="remote", transport="http", url="https://x.test/mcp", headers={"A": "1"}),
    ]

    assert is_already_added(_stdio("renamed", "uvx", "mcp-fs"), existing) is True
    assert is_already_added(_stdio("fs", "uvx", "other"), existing) is False
    assert is_already_added(_stdio("fs", "uvx"), existing) is False
    assert is_already_added(_stdio("fs", "mcp-fs", "uvx"), existing) is False
    trailing = MCPServerConfig(name="other", transport="http", url="https://x.test/mcp/", headers={"B": "2"})
    assert is_already_added(trailing, existing) is True
    stdio_named_like_url = _stdio("remote", "https://x.test/mcp")
    assert is_already_added(stdio_named_like_url, existing) is False
    with_env = MCPServerConfig(name="fs", transport="stdio", command="uvx", args=["mcp-fs"], env={"T": "x"})
    assert is_already_added(with_env, existing) is True


def test_import_messages_keep_english_and_localize_chinese() -> None:
    chinese = Localizer("zh-Hans")

    described = chinese.render(_DESCRIPTION.bind(agent="Claude Code", scope="用户"))
    assert described == "从 Claude Code 导入。它在 Claude Code 中是“用户”服务器，在所有项目中使用。"  # noqa: RUF001
    unsupported = chinese.render(_UNSUPPORTED_TRANSPORT.bind(app="iCode"))
    assert unsupported == "无法导入：iCode 仅支持 STDIO 和 HTTP MCP 服务器。"  # noqa: RUF001
    assert Localizer("en").render(_DESCRIPTION.bind(agent="Claude Code", scope="user")) == _USER_ORIGIN
    scoped = _DESCRIPTION_SCOPED.bind(agent="Claude Code", scope="项目", path="/work/app")
    assert chinese.render(scoped) == (
        "从 Claude Code 导入。它在 Claude Code 中是“项目”服务器，仅在 /work/app 中使用。"  # noqa: RUF001
    )
    notice = _SCOPED_NOTICE.bind(agent="Claude Code", local="本地", project="项目", app="iCode")
    assert chinese.render(notice) == (
        "标为“本地”或“项目”的服务器，Claude Code 只在卡片所写的目录中使用。"  # noqa: RUF001
        "导入后，iCode 会在所有项目中使用它们。"  # noqa: RUF001
    )


async def test_import_button_sits_beside_add_and_is_hidden_when_read_only() -> None:
    for read_only in (False, True):
        panel = MCPConfigPanel([], read_only=read_only)
        app = _PanelApp(panel)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            button = panel.query_one("#mcp-import-btn", Button)
            assert button.parent is panel.query_one("#mcp-add-btn", Button).parent
            assert button.display is not read_only
            assert button.disabled is read_only


type _FieldValue = str | list[tuple[str, str]]


def _field_value(widget: Widget) -> _FieldValue:
    """A single value's text, or the (name, value) rows of an env or headers grid."""
    if widget.has_class("mcp-import-pairs"):
        cells = [str(cell.render()) for cell in widget.children]
        return list(zip(cells[::2], cells[1::2], strict=True))
    return str(widget.render())


def _cards_in(dialog: ImportMcpServersDialog) -> list[tuple[str, list[tuple[str, _FieldValue]], bool]]:
    """Each card's title, its (field label, value) pairs and whether it can be ticked, in pane order."""
    cards = []
    for card in dialog.query(_ImportCard):
        labels = [str(label.render()) for label in card.query(".mcp-import-field-label")]
        values = [_field_value(value) for value in card.query(".mcp-import-field-value, .mcp-import-pairs")]
        title = f"{card.row.label} · {card.meta}"
        cards.append((title, list(zip(labels, values, strict=True)), not card.row.disabled))
    return cards


async def test_each_server_is_a_card_showing_the_settings_it_will_import(claude_home: Path) -> None:
    _write_claude_servers(
        claude_home,
        {
            "local": {"command": "npx", "args": ["-y", "my server"], "env": {"API_KEY": "${KEY}", "MODE": "fast"}},
            "remote": {"type": "http", "url": "https://x.test/mcp", "headers": {"Authorization": "Bearer abc123"}},
            "legacy": {"type": "sse", "url": "https://x.test/sse"},
            "socket": {"type": "ws", "url": "wss://x.test/mcp"},
            "untyped": {"command": "${HOME}/bin/server"},
        },
    )
    panel = MCPConfigPanel([])
    app = _PanelApp(panel)
    async with app.run_test(size=(120, 40)) as pilot:
        dialog = await _open_dialog(app, panel, pilot)
        await _wait_for_rows(dialog, pilot, 5)

        assert _cards_in(dialog) == [
            (
                "local · user · STDIO",
                [
                    ("Command", format_mcp_command_line("npx", ["-y", "my server"])),
                    ("Environment Variables", [("API_KEY", "{{KEY}}"), ("MODE", "fast")]),
                ],
                True,
            ),
            (
                "remote · user · HTTP",
                [("URL", "https://x.test/mcp"), ("Headers", [("Authorization", "Bearer abc123")])],
                True,
            ),
            # Not importable, SSE included: shown as written, greyed out, with the reason.
            ("legacy · user · SSE", [("URL", "https://x.test/sse")], False),
            ("socket · user · WS", [("URL", "wss://x.test/mcp")], False),
            # No type: STDIO, as Claude Code reads it, even though it can't be imported.
            ("untyped · user · STDIO", [("Command", format_mcp_command_line("${HOME}/bin/server", []))], False),
        ]
        assert [card.has_class("-unavailable") for card in dialog.query(_ImportCard)] == [
            False,
            False,
            True,
            True,
            True,
        ]
        assert _pane_text(dialog).count("Can't import: iCode supports only STDIO and HTTP") == 2
        # Every server here is one Claude Code uses in every project, so there is nothing to point out.
        assert not dialog.query(".mcp-import-notice")


async def test_env_values_wrap_in_their_own_column_beside_a_capped_name_column(claude_home: Path) -> None:
    long_name = "NODE_REPL_INSTRUCTIONS_USE_CASE_COMPUTER_USE_AND_MORE"
    long_value = "/Applications/" + "/".join(["Contents/Resources"] * 8)
    _write_claude_servers(
        claude_home,
        {
            "short": {"command": "a", "env": {"A": "1", "HOME_DIR": long_value}},
            "long": {"command": "b", "env": {long_name: long_value, "B": ""}},
        },
    )
    panel = MCPConfigPanel([])
    app = _PanelApp(panel)
    async with app.run_test(size=(120, 60)) as pilot:
        dialog = await _open_dialog(app, panel, pilot)
        await _wait_for_rows(dialog, pilot, 2)
        short, long = dialog.query(".mcp-import-pairs")

        def columns(grid: Widget) -> list[tuple[int, int]]:
            """Each cell's (x, width), in row order: name, value, name, value..."""
            return [(cell.region.x, cell.region.width) for cell in grid.children]

        # Short names: the column is as wide as the longest name plus its padding.
        await wait_for(
            lambda: columns(short)[0][1] == len("HOME_DIR") + 2, pilot=pilot, description="names fit their column"
        )
        # A long name: the column stops at 45% of the grid, and the name wraps inside it.
        cap = int(long.region.width * 0.45)
        await wait_for(lambda: columns(long)[0][1] == cap, pilot=pilot, description="the name column is capped")
        for grid in (short, long):
            names, values = columns(grid)[::2], columns(grid)[1::2]
            assert len(set(names)) == 1
            assert len(set(values)) == 1
            # A wrapped value stays in its column, and its name's cell is as tall as it.
            cells = list(grid.children)
            wrapped = next(i for i, cell in enumerate(cells) if str(cell.render()) == long_value)
            assert cells[wrapped].region.height > 1
            assert cells[wrapped - 1].region.height == cells[wrapped].region.height
        assert values[0][0] > names[0][0] + cap


async def test_cards_that_cant_be_ticked_open_folded_and_a_rebuild_keeps_the_users_folds(claude_home: Path) -> None:
    _write_claude_servers(
        claude_home,
        {
            "fs": {"command": "uvx", "args": ["mcp-fs"]},
            "new": {"command": "uvx", "args": ["new"]},
            "legacy": {"type": "sse", "url": "https://x.test/sse"},
        },
    )
    panel = MCPConfigPanel([_stdio("fs", "uvx", "mcp-fs")])
    app = _PanelApp(panel)
    async with app.run_test(size=(120, 40)) as pilot:
        await _cards(panel, pilot, 1)
        dialog = await _open_dialog(app, panel, pilot)
        await _wait_for_rows(dialog, pilot, 3)

        def folds() -> list[tuple[str, bool, bool]]:
            """Each card's server name, whether it is folded and whether its body shows, in pane order."""
            return [
                (str(card.row.label), card.collapsed, card.query_one(".config-card-body").display)
                for card in dialog.query(_ImportCard)
            ]

        # The server that can't be imported and the one already added open folded to their header.
        assert folds() == [("new", False, True), ("legacy", True, False), ("fs", True, False)]
        # The settings sit in a section titled like the agent's MCP card, whatever the transport.
        titles = [str(section.border_title) for section in dialog.query(".mcp-import-settings")]
        assert titles == ["STDIO Options", "SSE Options", "STDIO Options"]

        new, legacy, _fs = dialog.query(_ImportCard)
        await click_when_settled(pilot, legacy.query_one(".mcp-import-fold"))
        await click_when_settled(pilot, new.query_one(".mcp-import-fold"))
        expected = [("new", True, False), ("legacy", False, True), ("fs", True, False)]
        await wait_for(lambda: folds() == expected, pilot=pilot, description="the clicked cards fold and unfold")
        # The fold toggle is not the checkbox.
        assert new.row.value is False

        # A pane rebuild (as after a locale switch) keeps the folds the user chose.
        dialog.refresh_localization()
        await wait_for(
            lambda: not legacy.is_attached and len(dialog.query(_ImportCard)) == 3 and folds() == expected,
            pilot=pilot,
            description="the rebuilt cards keep their folds",
        )


async def test_local_and_project_servers_name_their_folder_in_the_card_and_the_description(tmp_path: Path) -> None:
    home = tmp_path / "home"
    workspace = tmp_path / "work" / "app"
    workspace.mkdir(parents=True)
    home.mkdir()
    # Where a tool installing a server "just for this project" puts it.
    (workspace / ".mcp.json").write_text(
        json.dumps(
            {"mcpServers": {"codegraph": {"type": "stdio", "command": "codegraph", "args": ["serve", "--mcp"]}}}
        ),
        encoding="utf-8",
    )
    (home / ".claude.json").write_text(
        json.dumps(
            {
                "mcpServers": {"everywhere": {"command": "uvx", "args": ["everywhere"]}},
                "projects": {
                    str(workspace): {
                        "mcpServers": {
                            "here": {"type": "http", "url": "https://x.test/mcp"},
                            "here-too": {"command": "uvx", "args": ["here-too"]},
                        }
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    results: list[list[MCPServerConfig] | None] = []
    app = _PanelApp(MCPConfigPanel([]))
    async with app.run_test(size=(120, 40)) as pilot:
        dialog = ImportMcpServersDialog(
            workspace_cwd=str(workspace),
            existing=[],
            locale_controller=app.locale_controller,
            context=ImportContext(workspace_cwd=str(workspace), home=home, environ=_quiet_environ(tmp_path)),
        )
        app.push_screen(dialog, callback=results.append)
        here, here_too, codegraph, everywhere = await _wait_for_rows(dialog, pilot, 4)

        folder = ("Claude Code uses it only in", str(workspace))
        # The folder sits in the card's body, where a long path can wrap, not beside the name.
        assert _cards_in(dialog) == [
            ("here · local · HTTP", [folder, ("URL", "https://x.test/mcp")], True),
            ("here-too · local · STDIO", [folder, ("Command", format_mcp_command_line("uvx", ["here-too"]))], True),
            (
                "codegraph · project · STDIO",
                [folder, ("Command", format_mcp_command_line("codegraph", ["serve", "--mcp"]))],
                True,
            ),
            ("everywhere · user · STDIO", [("Command", format_mcp_command_line("uvx", ["everywhere"]))], True),
        ]
        # What importing changes is said once, above the cards.
        (notice,) = dialog.query(".mcp-import-notice")
        assert str(notice.render()) == (
            "Claude Code uses the servers marked local or project only in the folder their card names. "
            "Once imported, iCode uses them in every project."
        )
        assert dialog.query_one("#mcp-import-servers").children[0] is notice

        here.value = True
        codegraph.value = True
        everywhere.value = True
        await _import_ticked(dialog, pilot)

        await wait_for(lambda: results, pilot=pilot, description="dialog dismissed with the ticked servers")
        (imported,) = results
        assert imported is not None
        assert [(config.name, config.description) for config in imported] == [
            ("here", f"Imported from Claude Code. There it is a local server, used only in {workspace}."),
            ("codegraph", f"Imported from Claude Code. There it is a project server, used only in {workspace}."),
            ("everywhere", _USER_ORIGIN),
        ]
        assert here_too.value is False


async def test_import_warnings_say_what_changed_and_stay_in_the_description(claude_home: Path) -> None:
    _write_claude_servers(
        claude_home,
        {
            "local": {"command": "npx", "env": {"HOST": "${API_HOST:-localhost}", "ROOT": "${CLAUDE_PROJECT_DIR}"}},
            "remote": {
                "type": "http",
                "url": "https://x.test/mcp",
                "headers": {"Authorization": "Bearer ${TOKEN:-sk-live-abc}"},
                "headersHelper": "./get-headers.sh",
            },
            "signed-in": {
                "type": "http",
                "url": "https://x.test/other",
                "headers": {"Authorization": "Bearer ${ANTHROPIC_API_KEY}"},
                "oauth": {"clientId": "id"},
                "future": True,
            },
        },
    )
    panel = MCPConfigPanel([])
    app = _PanelApp(panel)
    async with app.run_test(size=(120, 40)) as pilot:
        dialog = await _open_dialog(app, panel, pilot)
        local, remote, signed_in = await _wait_for_rows(dialog, pilot, 3)
        dropped = (
            "HOST gave API_HOST a default value. iCode variables can't have one, so it is now "
            "{{API_HOST}}. Set API_HOST before starting iCode, or the connection fails."
        )
        dropped_token = (
            "Authorization gave TOKEN a default value. iCode variables can't have one, so it is now "
            "{{TOKEN}}. Set TOKEN before starting iCode, or the connection fails."
        )
        project_dir = (
            "ROOT references CLAUDE_PROJECT_DIR, which Claude Code sets to the project folder for the servers it "
            "starts. iCode doesn't set it: set it before starting iCode, or write the folder path instead."
        )
        helper = (
            "The headers helper command ./get-headers.sh is not imported: iCode doesn't run it. "
            "Add the headers it produces under Headers yourself."
        )
        credential = (
            "Authorization references ANTHROPIC_API_KEY, which Claude Code never sends to a remote server. "
            "iCode does send its real value: remove the header, or copy the credential into a variable with "
            "another name and reference that."
        )
        oauth = (
            "The OAuth sign-in settings are not imported: iCode doesn't sign in to MCP servers with OAuth. "
            "Add an Authorization header under Headers yourself, or the server may refuse the connection."
        )
        unknown = "Not imported, iCode has no matching setting: future."
        warnings = [str(widget.render()) for widget in dialog.query(".mcp-import-warning")]
        assert warnings == [dropped, project_dir, dropped_token, helper, credential, oauth, unknown]

        local.value = True
        remote.value = True
        signed_in.value = True
        await _import_ticked(dialog, pilot)

        await _cards(panel, pilot, 3)
        assert [config.description for config in panel.get_config()] == [
            f"{_USER_ORIGIN}\n- {dropped}\n- {project_dir}",
            f"{_USER_ORIGIN}\n- {dropped_token}\n- {helper}",
            f"{_USER_ORIGIN}\n- {credential}\n- {oauth}\n- {unknown}",
        ]
        # A dropped default may be a secret: the saved description never repeats it.
        assert not any("sk-live-abc" in str(config.description) for config in panel.get_config())


async def test_import_lists_claude_servers_and_adds_ticked_ones_as_unsaved_cards(claude_home: Path) -> None:
    _write_claude_servers(claude_home, _CLAUDE_SERVERS)
    panel = MCPConfigPanel([_stdio("existing")])
    app = _PanelApp(panel)
    async with app.run_test(size=(120, 40)) as pilot:
        await _cards(panel, pilot, 1)
        dialog = await _open_dialog(app, panel, pilot)

        github, remote, legacy, templated = await _wait_for_rows(dialog, pilot, 4)
        sources = dialog.query_one("#mcp-import-sources", OptionList)
        assert [sources.get_option_at_index(i).id for i in range(sources.option_count)] == ["claude_code"]
        assert (github.disabled, remote.disabled, legacy.disabled, templated.disabled) == (False, False, True, True)
        text = _pane_text(dialog)
        assert "doesn't resolve in URLs" in text
        confirm = dialog.query_one("#mcp-import-confirm", Button)
        assert confirm.disabled is True

        await click_when_settled(pilot, github)  # the checkbox's label is the server name
        await wait_for(lambda: github.value is True, pilot=pilot, description="clicking the server name ticks it")
        remote.value = True
        await _import_ticked(dialog, pilot)

        cards = await _cards(panel, pilot, 3)
        assert [card.collapsed for card in cards] == [False, False, True]
        configs = panel.get_config()
        assert [config.name for config in configs] == ["github", "remote", "existing"]
        assert configs[0].env == {"TOKEN": "{{GITHUB_TOKEN}}"}
        assert configs[0].description == _USER_ORIGIN
        assert configs[1].headers == {"Authorization": "Bearer {{TOKEN}}"}
        assert configs[1].description == _USER_ORIGIN


def _ticks(dialog: ImportMcpServersDialog) -> list[tuple[str, bool, bool]]:
    """Each card's server name, whether its checkbox is ticked and whether it can be changed, in pane order."""
    return [(str(card.row.label), card.row.value, not card.row.disabled) for card in dialog.query(_ImportCard)]


async def test_servers_already_in_the_agent_show_ticked_and_locked_at_the_bottom(claude_home: Path) -> None:
    _write_claude_servers(
        claude_home,
        {
            "same-connection": {"command": "uvx", "args": ["mcp-fs"]},
            "templated": {"type": "http", "url": "https://${HOST}/mcp"},
            "fs": {"command": "uvx", "args": ["other"]},
        },
    )
    panel = MCPConfigPanel([_stdio("fs", "uvx", "mcp-fs")])
    app = _PanelApp(panel)
    async with app.run_test(size=(120, 40)) as pilot:
        await _cards(panel, pilot, 1)
        dialog = await _open_dialog(app, panel, pilot)
        await _wait_for_rows(dialog, pilot, 3)

        # Matched by connection: a different name doesn't make it a different server, and a shared name doesn't make it the same.
        # Importable first, then what can't be imported, then what the agent already has.
        assert _ticks(dialog) == [("fs", False, True), ("templated", False, False), ("same-connection", True, False)]
        assert "Already in this agent" not in _pane_text(dialog)
        # A locked tick is not a choice to import.
        assert dialog.query_one("#mcp-import-confirm", Button).disabled is True

        _row(dialog, 2).value = True
        await _import_ticked(dialog, pilot)
        imported, _original = await _cards(panel, pilot, 2)
        assert [config.name for config in panel.get_config()] == ["fs", "fs"]

        # The unsaved import counts as already added the next time.
        dialog = await _open_dialog(app, panel, pilot)
        await _wait_for_rows(dialog, pilot, 3)
        assert _ticks(dialog) == [("templated", False, False), ("same-connection", True, False), ("fs", True, False)]
        dialog.query_one("#mcp-import-cancel", Button).press()
        await wait_for(lambda: app.screen is not dialog, pilot=pilot, description="dialog closed")

        await press_and_answer_confirm(pilot, imported.query_one("#mcp-delete-btn-0", Button))
        await _cards(panel, pilot, 1)
        dialog = await _open_dialog(app, panel, pilot)
        await _wait_for_rows(dialog, pilot, 3)
        assert _ticks(dialog) == [("fs", False, True), ("templated", False, False), ("same-connection", True, False)]


async def test_cancel_and_escape_leave_the_cards_unchanged(claude_home: Path) -> None:
    _write_claude_servers(claude_home, {"github": {"command": "npx"}})
    panel = MCPConfigPanel([_stdio("existing")])
    app = _PanelApp(panel)
    async with app.run_test(size=(120, 40)) as pilot:
        await _cards(panel, pilot, 1)
        for close in ("cancel", "escape"):
            dialog = await _open_dialog(app, panel, pilot)
            (row,) = await _wait_for_rows(dialog, pilot, 1)
            row.value = True
            if close == "cancel":
                dialog.query_one("#mcp-import-cancel", Button).press()
            else:
                await pilot.press("escape")
            await wait_for(lambda dialog=dialog: app.screen is not dialog, pilot=pilot, description="dialog closed")
            assert [config.name for config in panel.get_config()] == ["existing"]


async def test_import_is_refused_while_a_connection_test_runs(claude_home: Path) -> None:
    panel = MCPConfigPanel([_stdio("existing")])
    app = _PanelApp(panel)
    async with app.run_test(size=(120, 40)) as pilot:
        await _cards(panel, pilot, 1)
        panel._testing.add(0)

        panel.query_one("#mcp-import-btn", Button).press()

        await wait_for(lambda: app.notifications, pilot=pilot, description="refusal notice")
        assert "before importing servers" in app.notifications[0]
        assert not isinstance(app.screen, ImportMcpServersDialog)


async def test_dialog_says_when_no_supported_agent_is_installed(tmp_path: Path) -> None:
    app = _PanelApp(MCPConfigPanel([]))
    async with app.run_test(size=(120, 40)) as pilot:
        dialog = ImportMcpServersDialog(
            workspace_cwd=None,
            existing=[],
            locale_controller=app.locale_controller,
            context=ImportContext(workspace_cwd=None, home=tmp_path, environ=_quiet_environ(tmp_path)),
        )
        app.push_screen(dialog)

        await wait_for(
            lambda: dialog.is_mounted and "No supported agents found" in _pane_text(dialog),
            pilot=pilot,
            description="no-agents message",
        )
        assert "Supported: Claude Code, Codex, opencode, pi." in _pane_text(dialog)
        assert dialog.query_one("#mcp-import-sidebar").display is False


async def test_a_message_in_place_of_servers_follows_a_locale_switch(tmp_path: Path) -> None:
    app = _PanelApp(MCPConfigPanel([]))
    # Its own controller: the class-level one is shared by every test in this module.
    app.locale_controller = LocaleController(Settings(locale="en"))
    async with app.run_test(size=(120, 40)) as pilot:
        dialog = ImportMcpServersDialog(
            workspace_cwd=None,
            existing=[],
            locale_controller=app.locale_controller,
            sources=[],
            context=ImportContext(workspace_cwd=None, home=tmp_path, environ=_quiet_environ(tmp_path)),
        )
        app.push_screen(dialog)
        english = Localizer("en").render(_NO_AGENTS.bind(agents=""))
        await wait_for(
            lambda: dialog.is_mounted and _pane_text(dialog) == english,
            pilot=pilot,
            description="no-agents message in English",
        )

        app.locale_controller.switch_locale("zh-Hans")

        chinese = Localizer("zh-Hans").render(_NO_AGENTS.bind(agents=""))
        assert chinese != english
        await wait_for(
            lambda: _pane_text(dialog) == chinese,
            pilot=pilot,
            description="the message is rendered again in Chinese",
        )


@dataclass
class _FakeSource:
    source_id: str
    display_name: str
    servers: list[str]
    discovered: list[str] = field(default_factory=list)

    def detect(self, ctx: ImportContext) -> bool:
        return True

    def discover(self, ctx: ImportContext) -> McpImportResult:
        self.discovered.append(self.source_id)
        return McpImportResult(
            candidates=tuple(
                McpImportCandidate(
                    name, "user", f"/{self.source_id}", _stdio(name, self.source_id, name), transport="stdio"
                )
                for name in self.servers
            )
        )


async def test_ticks_survive_switching_agents_and_import_collects_every_agent(tmp_path: Path) -> None:
    first = _FakeSource("first", "First Agent", ["a", "b"])
    second = _FakeSource("second", "Second Agent", ["c"])
    results: list[list[MCPServerConfig] | None] = []
    app = _PanelApp(MCPConfigPanel([]))
    async with app.run_test(size=(120, 40)) as pilot:
        dialog = ImportMcpServersDialog(
            workspace_cwd=None,
            existing=[],
            locale_controller=app.locale_controller,
            sources=[first, second],
            context=ImportContext(workspace_cwd=None, home=tmp_path, environ=_quiet_environ(tmp_path)),
        )
        app.push_screen(dialog, callback=results.append)
        await wait_for(lambda: dialog.is_mounted, pilot=pilot, description="import dialog is open")
        sources = dialog.query_one("#mcp-import-sources", OptionList)

        _a, b = await _wait_for_rows(dialog, pilot, 2)
        b.value = True
        sources.highlighted = 1
        await wait_for(
            lambda: [title for title, _fields, _available in _cards_in(dialog)] == ["c · user · STDIO"],
            pilot=pilot,
            description="second agent's servers are listed",
        )
        _row(dialog, 0).value = True
        sources.highlighted = 0
        await wait_for(
            lambda: len(dialog.query(".mcp-import-row")) == 2 and _row(dialog, 1).value is True,
            pilot=pilot,
            timeout=_DEFAULT_WAIT_TIMEOUT,
            description="first agent's tick is kept",
        )
        assert first.discovered == ["first"]  # read once, then served from the dialog's cache

        await _import_ticked(dialog, pilot)

        await wait_for(lambda: results, pilot=pilot, description="dialog dismissed with the ticked servers")
        (imported,) = results
        assert imported is not None
        assert [(config.name, config.description) for config in imported] == [
            ("b", "Imported from First Agent. There it is a user server, used in every project."),
            ("c", "Imported from Second Agent. There it is a user server, used in every project."),
        ]


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


async def test_a_ticked_server_locks_the_same_server_elsewhere_and_is_imported_once(tmp_path: Path) -> None:
    github = _stdio("github", "npx", "-y", "server-github")
    first = _ListedSource(
        "first",
        "First Agent",
        (
            _candidate("github", "user", github),
            _candidate("github", "project", github),
            _candidate("fs", "user", _stdio("fs", "uvx")),
        ),
    )
    second = _ListedSource("second", "Second Agent", (_candidate("gh", "user", github),))
    results: list[list[MCPServerConfig] | None] = []
    app = _PanelApp(MCPConfigPanel([]))
    async with app.run_test(size=(120, 40)) as pilot:
        dialog = _open_with(app, [first, second], tmp_path, results)
        await wait_for(lambda: dialog.is_mounted, pilot=pilot, description="import dialog is open")
        user, project, _fs = await _wait_for_rows(dialog, pilot, 3)
        covered = "Same server as github (First Agent, user), which is ticked. It is imported once."

        user.value = True
        await wait_for(lambda: project.disabled, pilot=pilot, description="the same server in another scope is locked")
        assert _ticks(dialog) == [("github", True, True), ("github", False, False), ("fs", False, True)]
        assert [card.has_class("-unavailable") for card in dialog.query(_ImportCard)] == [False, True, False]
        notes = [str(note.render()) for note in dialog.query(".mcp-import-note") if note.display]
        assert notes == [covered]

        # Unticking frees it again.
        user.value = False
        await wait_for(lambda: not project.disabled, pilot=pilot, description="unticking unlocks the other scope")
        assert not [note for note in dialog.query(".mcp-import-note") if note.display]

        # The lock spans agents.
        user.value = True
        await wait_for(lambda: project.disabled, pilot=pilot, description="ticked again")
        dialog.query_one("#mcp-import-sources", OptionList).highlighted = 1
        await wait_for(
            lambda: [title for title, _fields, _available in _cards_in(dialog)] == ["gh · user · STDIO"],
            pilot=pilot,
            description="second agent's servers are listed",
        )
        assert _ticks(dialog) == [("gh", False, False)]
        assert covered in _pane_text(dialog)

        await _import_ticked(dialog, pilot)
        await wait_for(lambda: results, pilot=pilot, description="dialog dismissed with the ticked servers")
        (imported,) = results
        assert imported is not None
        assert [(config.name, config.command) for config in imported] == [("github", "npx")]


async def test_two_servers_ticked_at_once_with_one_connection_are_imported_once(tmp_path: Path) -> None:
    github = _stdio("github", "npx", "-y", "server-github")
    source = _ListedSource(
        "first", "First Agent", (_candidate("github", "user", github), _candidate("github", "project", github))
    )
    results: list[list[MCPServerConfig] | None] = []
    app = _PanelApp(MCPConfigPanel([]))
    async with app.run_test(size=(120, 40)) as pilot:
        dialog = _open_with(app, [source], tmp_path, results)
        await wait_for(lambda: dialog.is_mounted, pilot=pilot, description="import dialog is open")
        user, project = await _wait_for_rows(dialog, pilot, 2)

        # Both ticks land before either Changed is handled: neither locks the other, and both stay ticked.
        user.value = True
        project.value = True
        await wait_for(lambda: dialog._ticked.get("first") == {0, 1}, pilot=pilot, description="both ticks recorded")
        assert _ticks(dialog) == [("github", True, True), ("github", True, True)]

        await _import_ticked(dialog, pilot)
        await wait_for(lambda: results, pilot=pilot, description="dialog dismissed with the ticked servers")
        (imported,) = results
        assert imported is not None
        assert [config.name for config in imported] == ["github"]


async def test_switching_back_before_the_pane_worker_starts_shows_the_highlighted_agent(tmp_path: Path) -> None:
    gate = threading.Event()
    first = _ListedSource("first", "First Agent", (_candidate("a", "user", _stdio("a", "first")),))
    second = _ListedSource("second", "Second Agent", (_candidate("b", "user", _stdio("b", "second")),), gate=gate)
    app = _PanelApp(MCPConfigPanel([]))
    async with app.run_test(size=(120, 40)) as pilot:
        dialog = _open_with(app, [first, second], tmp_path)
        await wait_for(lambda: dialog.is_mounted, pilot=pilot, description="import dialog is open")
        await _wait_for_rows(dialog, pilot, 1)
        sources = dialog.query_one("#mcp-import-sources", OptionList)

        # Highlight the second agent and return to the first with no await between: the
        # second agent's pane worker has not started when the return is handled.
        for index in (1, 0):
            option = sources.get_option_at_index(index)
            dialog._on_source_highlighted(OptionList.OptionHighlighted(sources, option, index))

        await wait_for(
            lambda: _ticks(dialog) == [("a", False, True)],
            pilot=pilot,
            description="the first agent's servers are listed",
        )
        gate.set()
        pane_workers = [worker for worker in app.workers if worker.node is dialog and worker.group == "mcp-import-pane"]
        await wait_for(
            lambda: all(worker.is_finished for worker in pane_workers),
            pilot=pilot,
            timeout=_DEFAULT_WAIT_TIMEOUT,
            description="the stale read of the second agent finished",
        )
        assert _ticks(dialog) == [("a", False, True)]


def _broken_pane(source: Any, result: Any) -> list[Any]:
    raise RuntimeError("pane bug")


async def _broken_detect() -> None:
    raise RuntimeError("detect bug")


@pytest.mark.parametrize(
    ("method", "broken", "message", "logged"),
    [
        (
            "_pane_widgets",
            _broken_pane,
            "Couldn't read the MCP servers of First Agent.",
            "Showing the MCP servers of First Agent failed",
        ),
        (
            "_list_sources",
            _broken_detect,
            "Couldn't look for other agents. See the log for details.",
            "Listing agents to import MCP servers from failed",
        ),
    ],
    ids=["pane", "detect"],
)
async def test_a_failing_pane_worker_says_so_and_logs_the_error(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
    method: str,
    broken: Any,
    message: str,
    logged: str,
) -> None:
    source = _ListedSource("first", "First Agent", (_candidate("a", "user", _stdio("a")),))
    app = _PanelApp(MCPConfigPanel([]))
    with caplog.at_level(logging.ERROR, logger="chrys.app.tui.screens.agents.import_mcp_dialog"):
        async with app.run_test(size=(120, 40)) as pilot:
            dialog = _new_dialog(app, [source], tmp_path)
            monkeypatch.setattr(dialog, method, broken)
            app.push_screen(dialog)
            await wait_for(
                lambda: dialog.is_mounted and _pane_text(dialog) == message,
                pilot=pilot,
                description="the failure is shown in place of the spinner message",
            )
    assert [record.getMessage() for record in caplog.records if record.exc_info] == [logged]


async def test_imported_servers_mark_the_draft_dirty_and_duplicate_names_fail_save(
    claude_home: Path, tmp_path: Path
) -> None:
    _write_claude_servers(claude_home, {"shared": {"command": "npx", "args": ["other-server"]}})
    registry = registry_with(
        make_profile(
            "mcp-agent",
            display_name="MCP Agent",
            description="Agent with one MCP server",
            tools=ToolsConfig(mcp=[_stdio("shared")]),
        ),
    )

    async with open_agent_config(registry, current_profile="MCP Agent", initial_tab="mcp") as (screen, pilot):
        panel = screen.query_one(MCPConfigPanel)
        await _cards(panel, pilot, 1)
        save = screen.query_one("#ac-save", Button)
        await wait_for(lambda: save.disabled, pilot=pilot, description="clean baseline")

        dialog = await _open_dialog(pilot.app, panel, pilot)
        (row,) = await _wait_for_rows(dialog, pilot, 1)
        row.value = True
        await _import_ticked(dialog, pilot)

        await wait_for(
            lambda: (
                not save.disabled
                and screen._drafts[screen._selected_draft_key].dirty
                and len(screen._drafts[screen._selected_draft_key].profile.tools.mcp) == 2
            ),
            pilot=pilot,
            timeout=_DEFAULT_WAIT_TIMEOUT,
            description="the import marks the draft dirty",
        )
        errors: list[str] = []
        screen.notify = lambda message, **kwargs: errors.append(message) if kwargs.get("severity") == "error" else None  # type: ignore[method-assign]

        save.press()

        await wait_for(lambda: errors, pilot=pilot, description="Save reports the duplicate server name")
        assert "shared" in errors[0]
        assert registry.get("mcp-agent").tools.mcp == [_stdio("shared")]


@pytest.mark.parametrize(
    "warning",
    [ImportWarning(kind, "VAR", field="FIELD", original="ORIGINAL") for kind in ImportWarningKind]
    + [ImportWarning(ImportWarningKind.MERGED, original="/a.json"), ImportWarning(ImportWarningKind.TOOLS_LIMITED)],
    ids=lambda warning: f"{warning.kind.value}-{bool(warning.field)}-{bool(warning.original)}",
)
def test_every_import_warning_has_a_message(warning: ImportWarning) -> None:
    dialog = ImportMcpServersDialog(workspace_cwd=None, existing=[], locale_controller=None, sources=[])

    text = dialog._warning_text(warning, "opencode")

    assert text and "{" not in text.replace("{{VAR}}", "")


@pytest.mark.parametrize("reason", list(UnsupportedReason))
def test_every_unsupported_reason_has_a_message(reason: UnsupportedReason) -> None:
    dialog = ImportMcpServersDialog(workspace_cwd=None, existing=[], locale_controller=None, sources=[])
    candidate = McpImportCandidate("s", "project", "/p", None, unsupported=reason)

    text = dialog._unsupported_note(candidate, "pi")

    assert text and text.startswith("Can't import:") and "{" not in text


@pytest.mark.parametrize("kind", list(SourceErrorKind))
def test_every_source_error_has_a_message(kind: SourceErrorKind) -> None:
    text = format_message(_source_error(McpSourceError("/x/opencode.json", kind), "opencode"))

    assert "/x/opencode.json" in text and "{" not in text


async def test_opencode_and_pi_servers_are_listed_and_imported_with_what_they_bring(tmp_path: Path) -> None:
    home = tmp_path / "home"
    workspace = tmp_path / "work"
    workspace.mkdir()
    opencode_config = home / ".config" / "opencode" / "opencode.jsonc"
    opencode_config.parent.mkdir(parents=True)
    opencode_config.write_text(
        """{
  // opencode allows comments
  "mcp": {
    "files": {"type": "local", "command": ["npx", "server-files"], "environment": {"T": "{env:FILES_TOKEN}"},
              "enabled": false},
  },
}""",
        encoding="utf-8",
    )
    pi_project = workspace / ".pi" / "mcp.json"
    pi_project.parent.mkdir(parents=True)
    pi_project.write_text(
        json.dumps({"mcpServers": {"docs": {"url": "https://docs.test/mcp", "description": "Searches the docs"}}}),
        encoding="utf-8",
    )
    environ = {**_quiet_environ(tmp_path), "XDG_CONFIG_HOME": str(home / ".config"), "FILES_TOKEN": "t"}
    results: list[list[MCPServerConfig] | None] = []
    app = _PanelApp(MCPConfigPanel([]))
    async with app.run_test(size=(120, 40)) as pilot:
        dialog = ImportMcpServersDialog(
            workspace_cwd=str(workspace),
            existing=[],
            locale_controller=app.locale_controller,
            context=ImportContext(workspace_cwd=str(workspace), home=home, environ=environ),
        )
        app.push_screen(dialog, callback=results.append)
        (files,) = await _wait_for_rows(dialog, pilot, 1)
        sources = dialog.query_one("#mcp-import-sources", OptionList)
        assert [str(sources.get_option_at_index(i).prompt) for i in range(sources.option_count)] == ["opencode", "pi"]
        disabled = (
            "opencode has this server turned off, so it is imported turned off. Tick Enabled in its card to use it."
        )
        assert _cards_in(dialog) == [
            (
                "files · global · STDIO",
                [
                    ("Command", format_mcp_command_line("npx", ["server-files"])),
                    ("Environment Variables", [("T", "{{FILES_TOKEN}}")]),
                ],
                True,
            )
        ]
        assert [str(widget.render()) for widget in dialog.query(".mcp-import-warning")] == [disabled]
        files.value = True

        sources.highlighted = 1
        await wait_for(
            lambda: [title for title, _fields, _tickable in _cards_in(dialog)] == ["docs · project · HTTP"],
            pilot=pilot,
            description="pi's servers are shown",
        )
        (notice,) = dialog.query(".mcp-import-notice")
        assert str(notice.render()) == (
            "pi uses the servers marked project only in the folder their card names. "
            "Once imported, iCode uses them in every project."
        )
        (docs,) = await _wait_for_rows(dialog, pilot, 1)
        docs.value = True
        await _import_ticked(dialog, pilot)

        await wait_for(lambda: results, pilot=pilot, description="dialog dismissed with the ticked servers")
        (imported,) = results
        assert imported is not None
        assert [(config.name, config.enabled, config.description) for config in imported] == [
            (
                "files",
                False,
                f"Imported from opencode. There it is a global server, used in every project.\n- {disabled}",
            ),
            (
                "docs",
                True,
                f"Searches the docs\nImported from pi. There it is a project server, used only in {workspace}.",
            ),
        ]


async def test_codex_servers_are_detected_previewed_and_imported(tmp_path: Path) -> None:
    home = tmp_path / "home"
    workspace = tmp_path / "work"
    workspace.mkdir()
    path = home / ".codex/config.toml"
    path.parent.mkdir(parents=True)
    path.write_text(
        '[mcp_servers.files]\ncommand = "server-files"\nenv_vars = ["TOKEN"]\n'
        'enabled = false\ntool_timeout_sec = 42\nenabled_tools = ["read", "write"]\ndisabled_tools = ["write"]\n'
        '[mcp_servers.remote]\ncommand = "server-remote"\nenvironment_id = "remote"\n',
        encoding="utf-8",
    )
    results: list[list[MCPServerConfig] | None] = []
    app = _PanelApp(MCPConfigPanel([]))
    async with app.run_test(size=(120, 40)) as pilot:
        dialog = ImportMcpServersDialog(
            workspace_cwd=str(workspace),
            existing=[],
            locale_controller=app.locale_controller,
            context=ImportContext(str(workspace), home, _quiet_environ(tmp_path)),
        )
        app.push_screen(dialog, callback=results.append)
        files, remote = await _wait_for_rows(dialog, pilot, 2)
        sources = dialog.query_one("#mcp-import-sources", OptionList)
        assert [str(sources.get_option_at_index(i).prompt) for i in range(sources.option_count)] == ["Codex"]
        assert remote.disabled is True
        assert "('TOKEN', '{{TOKEN}}')" in str(_cards_in(dialog))
        files.value = True
        await _import_ticked(dialog, pilot)
        await wait_for(lambda: results, pilot=pilot, description="Codex server imported")
        (imported,) = results
        assert imported is not None and len(imported) == 1
        (server,) = imported
        assert server.name == "files"
        assert server.command == "server-files"
        assert server.env == {"TOKEN": "{{TOKEN}}"}
        assert server.enabled is False
        assert server.request_timeout == 42
        assert server.allowed_tools == ["read"]
        assert server.description.startswith("Imported from Codex.")
        assert "requires a nonempty value" in server.description
