# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""The MCP tab imports servers configured in other agents as unsaved cards."""

from __future__ import annotations

import json
import logging
import threading
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest
from textual.geometry import Region
from textual.widgets import Button, Collapsible, OptionList, Static

from chrys.app.tui.i18n import LocaleController
from chrys.app.tui.screens.agents.import_mcp_dialog import (
    _DESCRIPTION,
    _DESCRIPTION_SCOPED,
    _EMPTY_SOURCE,
    _NO_AGENTS,
    _SCOPE,
    _SCOPE_AFTER_IMPORT,
    _SCOPE_EVERY_PROJECT,
    _SCOPE_IMPORTED_ALL,
    _SCOPE_ONLY_IN,
    _UNSUPPORTED_TRANSPORT,
    ImportMcpServersDialog,
    _ImportCard,
    _ImportItem,
    _ImportRow,
    _source_error,
    is_already_added,
)
from chrys.app.tui.screens.agents.panels.mcp import MCPConfigPanel, format_mcp_command_line
from chrys.app.tui.theme import CHRYS_THEME
from chrys.app.tui.widgets import HatchedEmptyState
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
from tests.app.tui.screens._mcp_import_support import (
    _CLAUDE_USER,
    _candidate,
    _cards,
    _expand_cards,
    _guidance,
    _hints,
    _import_ticked,
    _ListedSource,
    _new_dialog,
    _open_dialog,
    _open_with,
    _pane_text,
    _PanelApp,
    _prefix,
    _quiet_environ,
    _read_cards,
    _row,
    _scope,
    _source,
    _stdio,
    _titles,
    _transport,
    _wait_for_cards,
    _wait_for_rows,
    _warning_texts,
    _write_claude_servers,
)
from tests.support.pilot_barrier import screen_is_settled
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


def test_already_added_compares_connections_not_names() -> None:
    existing = [
        _stdio("fs", "uvx", "mcp-fs"),
        MCPServerConfig(name="remote", transport="http", url="https://x.test/mcp", headers={"A": "1"}),
    ]

    assert is_already_added(_stdio("renamed", "uvx", "mcp-fs"), existing) is True
    assert is_already_added(_stdio("fs", "uvx", "other"), existing) is False
    assert is_already_added(_stdio("fs", "uvx"), existing) is False
    assert is_already_added(_stdio("fs", "mcp-fs", "uvx"), existing) is False
    trailing = replace(existing[1], name="other", url="https://x.test/mcp/")
    assert is_already_added(trailing, existing) is False
    assert is_already_added(replace(existing[1], name="renamed"), existing) is True
    assert is_already_added(replace(existing[1], headers={"A": "2"}), existing) is False
    stdio_named_like_url = _stdio("remote", "https://x.test/mcp")
    assert is_already_added(stdio_named_like_url, existing) is False
    with_env = MCPServerConfig(name="fs", transport="stdio", command="uvx", args=["mcp-fs"], env={"T": "x"})
    assert is_already_added(with_env, existing) is False
    assert is_already_added(replace(existing[0], allowed_tools=["read"]), existing) is False
    assert is_already_added(replace(existing[0], enabled=False), existing) is False


def test_import_messages_keep_english_and_localize_chinese() -> None:
    chinese = Localizer("zh-Hans")

    described = chinese.render(_DESCRIPTION.bind(agent="Claude Code", scope="用户"))
    assert described == "从 Claude Code 导入。它在 Claude Code 中是“用户”服务器，在所有项目中使用。"  # noqa: RUF001
    unsupported = chinese.render(_UNSUPPORTED_TRANSPORT.bind(app="iCode"))
    assert (
        unsupported == "iCode 仅支持 STDIO 和 HTTP MCP 服务器。请使用服务器提供的 STDIO 启动命令或 HTTP 地址手动添加。"
    )
    assert Localizer("en").render(_DESCRIPTION.bind(agent="Claude Code", scope="user")) == _USER_ORIGIN
    scoped = _DESCRIPTION_SCOPED.bind(agent="Claude Code", scope="项目", path="/work/app")
    assert chinese.render(scoped) == (
        "从 Claude Code 导入。它在 Claude Code 中是“项目”服务器，仅在 /work/app 中使用。"  # noqa: RUF001
    )
    assert chinese.render(_SCOPE_AFTER_IMPORT.bind()) == "导入后的作用范围"
    assert chinese.render(_SCOPE_IMPORTED_ALL.bind()) == "当前智能体可在所有项目中使用此服务器"
    assert chinese.render(_SCOPE.bind(agent="Claude Code")) == "Claude Code 中的作用范围"
    assert chinese.render(_SCOPE_EVERY_PROJECT.bind(scope="用户")) == "用户，在所有项目中使用"  # noqa: RUF001
    assert chinese.render(_SCOPE_ONLY_IN.bind(scope="本地", path="/work/app")) == (
        "本地，只在 /work/app 中使用"  # noqa: RUF001
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

        assert await _read_cards(dialog, pilot) == [
            (
                "local",
                [
                    _source(claude_home / ".claude.json"),
                    _CLAUDE_USER,
                    _transport("STDIO"),
                    ("Command", format_mcp_command_line("npx", ["-y", "my server"])),
                    ("Environment Variables", [("API_KEY", "{{KEY}}"), ("MODE", "fast")]),
                    # The prefix the import gives it, below the settings, as the agent's MCP card shows it.
                    ("Available Tool Scope", "All server tools"),
                    ("Loading Strategy", "Full — load all available tools"),
                    _prefix("local"),
                ],
                True,
            ),
            (
                "remote",
                [
                    _source(claude_home / ".claude.json"),
                    _CLAUDE_USER,
                    _transport("HTTP"),
                    ("URL", "https://x.test/mcp"),
                    ("Headers", [("Authorization", "Bearer abc123")]),
                    ("Available Tool Scope", "All server tools"),
                    ("Loading Strategy", "Full — load all available tools"),
                    _prefix("remote"),
                ],
                True,
            ),
            # Not importable: shown as written under its source settings, greyed out, with the reason the transport gives.
            (
                "legacy",
                [
                    _source(claude_home / ".claude.json"),
                    _CLAUDE_USER,
                    _transport("SSE"),
                    ("URL", "https://x.test/sse"),
                ],
                False,
            ),
            (
                "socket",
                [
                    _source(claude_home / ".claude.json"),
                    _CLAUDE_USER,
                    _transport("WS"),
                    ("URL", "wss://x.test/mcp"),
                ],
                False,
            ),
            # No type: STDIO, as Claude Code reads it, even though it can't be imported.
            (
                "untyped",
                [
                    _source(claude_home / ".claude.json"),
                    _CLAUDE_USER,
                    _transport("STDIO"),
                    ("Command", format_mcp_command_line("${HOME}/bin/server", [])),
                ],
                False,
            ),
        ]
        assert [card.has_class("-unavailable") for card in dialog.query(_ImportCard)] == [
            False,
            False,
            True,
            True,
            True,
        ]
        # The tick sits outside the card, level with its title line; the title alone folds it.
        for item in dialog.query(_ImportItem):
            assert (item.row, item.card) == (item.query_one(_ImportRow), item.query_one(_ImportCard))
            assert item.row.parent is item
        assert _pane_text(dialog).count("iCode supports only STDIO and HTTP") == 2
        # Every server here is one Claude Code uses in every project, so there is nothing to point out.
        assert not dialog.query(".mcp-import-notice")


async def test_env_vars_headers_and_urls_show_one_line_each_in_equal_columns_like_the_mcp_card(
    claude_home: Path,
) -> None:
    long_name = "NODE_REPL_INSTRUCTIONS_USE_CASE_COMPUTER_USE_AND_MORE"
    # A wrapped first line would hold only "Bearer"; one cut at the edge runs on into the token.
    long_value = "Bearer " + "t" * 200
    long_url = "https://x.test/mcp/" + "/".join(["segment"] * 30)
    _write_claude_servers(
        claude_home,
        {
            "local": {"command": "a", "env": {"A": "1", long_name: long_value}},
            "remote": {"type": "http", "url": long_url, "headers": {"Authorization": long_value}},
        },
    )
    panel = MCPConfigPanel([])
    app = _PanelApp(panel)
    async with app.run_test(size=(120, 60)) as pilot:
        dialog = await _open_dialog(app, panel, pilot)
        await _wait_for_rows(dialog, pilot, 2)
        # The cards open folded; the pair grids and values below assert painted layout.
        _expand_cards(dialog)
        await pilot.pause()
        grids = list(dialog.query(".mcp-import-pairs"))
        (url,) = [field for field in dialog.query(".mcp-import-field-value") if str(field.render()) == long_url]

        def visible_text(cell: Widget) -> str:
            """The cell's one line as painted, inside its padding."""
            (line,) = cell.render_lines(Region(0, 0, cell.size.width, 1))
            return line.text[1 : cell.size.width - 1]

        await wait_for(
            lambda: all(cell.size.width > 0 for grid in grids for cell in grid.children) and url.size.width > 0,
            pilot=pilot,
            description="the cards are laid out",
        )
        for grid in grids:
            cells = list(grid.children)
            names, values = cells[::2], cells[1::2]
            # Names and values share the grid evenly; an odd width leaves one cell over.
            assert {cell.region.x for cell in names} == {names[0].region.x}
            assert {cell.region.x for cell in values} == {values[0].region.x}
            assert abs(names[0].region.width - values[0].region.width) <= 1
            for cell in cells:
                assert cell.region.height == 1
                text = str(cell.render())
                assert visible_text(cell) == text[: cell.size.width - 2].ljust(cell.size.width - 2)
        assert url.region.height == 1
        assert visible_text(url) == long_url[: url.size.width - 2]


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
    async with app.run_test(size=(120, 60)) as pilot:
        await _cards(panel, pilot, 1)
        dialog = await _open_dialog(app, panel, pilot)
        await _wait_for_rows(dialog, pilot, 3)

        def folds() -> list[tuple[str, bool, bool]]:
            """Each card's server name, whether it is folded and whether its body shows, in pane order."""
            return [
                (card.row.server_name, card.collapsed, card.query_one(".config-card-body").display)
                for card in dialog.query(_ImportCard)
            ]

        # Every card opens folded to its title line, as on the agent's MCP card.
        assert folds() == [("new", True, False), ("legacy", True, False), ("fs", True, False)]
        # The title says what the server is: transport, then its program or URL host, right after the name.
        titles = _titles(dialog)
        assert titles[0] == "▶ new: STDIO · uvx"
        assert titles[1].startswith("▶ legacy: SSE · x.test")
        assert titles[1].endswith("Can't import")
        assert titles[2] == "▶ fs: STDIO · uvx  Imported"

        new, legacy, _fs = dialog.query(_ImportCard)
        await click_when_settled(pilot, legacy.query_one(".mcp-import-title"))
        new.query_one(".mcp-import-title").scroll_visible(animate=False)
        await click_when_settled(pilot, new.query_one(".mcp-import-title"))
        expected = [("new", False, True), ("legacy", False, True), ("fs", True, False)]
        await wait_for(lambda: folds() == expected, pilot=pilot, description="the clicked cards fold and unfold")
        # The fold target is not the checkbox.
        assert new.row.value is False

        _expand_cards(dialog)
        await wait_for(
            lambda: dialog.query(".mcp-import-section"),
            pilot=pilot,
            description="the unfolded cards compose their settings",
        )
        # The settings sit in a section titled like the agent's MCP card; the SSE server, which can't be
        # imported, has no converted boxes — only what its source wrote, in its one box.
        titles = [str(section.border_title) for section in dialog.query(".mcp-import-section")]
        assert titles == [
            "STDIO Options",
            "Tool Access",
            "Tool Loading",
            "Naming & Limits",
            "Connection Settings in Source",
            "STDIO Options",
            "Tool Access",
            "Tool Loading",
            "Naming & Limits",
        ]

        # A pane rebuild (as after a locale switch) keeps the folds the user chose.
        dialog.refresh_localization()
        await wait_for(
            lambda: not legacy.is_attached and len(dialog.query(_ImportCard)) == 3 and folds() == expected,
            pilot=pilot,
            description="the rebuilt cards keep their folds",
        )


async def test_the_title_line_folds_the_card_away_from_the_checkbox(claude_home: Path) -> None:
    """The title line folds the card, arrow and all, and leaves its tick alone."""
    _write_claude_servers(claude_home, {"fs": {"command": "uvx", "args": ["mcp-fs"]}})
    panel = MCPConfigPanel([])
    app = _PanelApp(panel)
    async with app.run_test(size=(120, 40)) as pilot:
        dialog = await _open_dialog(app, panel, pilot)
        (row,) = await _wait_for_rows(dialog, pilot, 1)
        card = dialog.query_one(_ImportCard)
        title = card.query_one(".mcp-import-title")
        assert card.collapsed is True and card.query_one(".config-card-body").display is False
        assert _titles(dialog) == ["▶ fs: STDIO · uvx"]

        # Clicking the title line unfolds the card…
        await click_when_settled(pilot, title)
        await wait_for(
            lambda: card.collapsed is False and card.query_one(".config-card-body").display is True,
            pilot=pilot,
            description="the title line unfolds the card",
        )
        # …its arrow now pointing the way back.
        assert _titles(dialog) == ["▼ fs"]
        await click_when_settled(pilot, title)
        await wait_for(
            lambda: card.collapsed is True and card.query_one(".config-card-body").display is False,
            pilot=pilot,
            description="the title line folds the card again",
        )
        # Enter and Space fold it too, and never tick it.
        title.focus()
        await pilot.press("enter")
        await wait_for(lambda: card.collapsed is False, pilot=pilot, description="Enter unfolds the card")
        await pilot.press("space")
        await wait_for(lambda: card.collapsed is True, pilot=pilot, description="Space folds the card again")
        # It is a fold target, never a tick: the checkbox keeps its own value.
        assert row.value is False


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

        local = _scope("Claude Code", f"local, used only in {workspace}")
        project = _scope("Claude Code", f"project, used only in {workspace}")
        claude_json = _source(home / ".claude.json")
        mcp_json = _source(workspace / ".mcp.json")
        # The scope names the folder in the card's body, where a long path can wrap, not beside the name.
        assert await _read_cards(dialog, pilot) == [
            (
                "here",
                [
                    claude_json,
                    local,
                    ("Scope After Import", "Available to this agent in every project"),
                    _transport("HTTP"),
                    ("URL", "https://x.test/mcp"),
                    ("Available Tool Scope", "All server tools"),
                    ("Loading Strategy", "Full — load all available tools"),
                    _prefix("here"),
                ],
                True,
            ),
            (
                "here-too",
                [
                    claude_json,
                    local,
                    ("Scope After Import", "Available to this agent in every project"),
                    _transport("STDIO"),
                    ("Command", format_mcp_command_line("uvx", ["here-too"])),
                    ("Available Tool Scope", "All server tools"),
                    ("Loading Strategy", "Full — load all available tools"),
                    _prefix("here-too"),
                ],
                True,
            ),
            (
                "codegraph",
                [
                    mcp_json,
                    project,
                    ("Scope After Import", "Available to this agent in every project"),
                    _transport("STDIO"),
                    ("Command", format_mcp_command_line("codegraph", ["serve", "--mcp"])),
                    ("Available Tool Scope", "All server tools"),
                    ("Loading Strategy", "Full — load all available tools"),
                    _prefix("codegraph"),
                ],
                True,
            ),
            (
                "everywhere",
                [
                    claude_json,
                    _CLAUDE_USER,
                    _transport("STDIO"),
                    ("Command", format_mcp_command_line("uvx", ["everywhere"])),
                    ("Available Tool Scope", "All server tools"),
                    ("Loading Strategy", "Full — load all available tools"),
                    _prefix("everywhere"),
                ],
                True,
            ),
        ]
        assert not dialog.query(".mcp-import-notice")

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
            "Defaults for API_HOST in HOST are not imported. "
            "Set API_HOST before starting iCode, or enter a fixed value after import."
        )
        dropped_token = (
            "Defaults for TOKEN in Authorization are not imported. "
            "Set TOKEN before starting iCode, or enter a fixed value after import."
        )
        project_dir = (
            "ROOT references CLAUDE_PROJECT_DIR, which iCode does not set automatically. "
            "After import, fill in the required project folder path in this field, "
            "or set CLAUDE_PROJECT_DIR before starting iCode."
        )
        # The command may hold a credential: the warning, saved in the description, never names it.
        helper = (
            "The headers helper is not imported. After import, add the required headers in this server's settings. "
            "To obtain each header's value using {{!command}}, see the User Guide (F8)."
        )
        credential = (
            "To avoid unintentionally sending local credentials to an MCP server, references to ANTHROPIC_API_KEY "
            "in headers Authorization are removed, matching Claude Code. "
            "Other text is kept; headers with no remaining value are omitted."
        )
        oauth = (
            "OAuth settings and sign-in state are not imported. If the server accepts a token or another "
            "authentication method, configure Headers after import, or see the User Guide (F8) to obtain credentials "
            "using {{!command}}."
        )
        unknown = (
            "These source settings are not imported: future. "
            "Check whether your server relies on them before using it in iCode. "
            "For supported settings, see the User Guide (F8)."
        )
        # Warnings sit in the card's body, which the card builds when it is unfolded.
        await _read_cards(dialog, pilot)
        warnings = _warning_texts(dialog)
        assert len(warnings) == 7
        assert warnings[:3] == [dropped, project_dir, dropped_token]
        assert warnings[3] == helper
        assert warnings[4] == credential
        assert warnings[5:] == [oauth, unknown]

        local.value = True
        remote.value = True
        signed_in.value = True
        await _import_ticked(dialog, pilot)

        await _cards(panel, pilot, 3)
        assert [config.description for config in panel.get_config()] == [
            f"{_USER_ORIGIN}\n⚠ {dropped}\n⚠ {project_dir}",
            f"{_USER_ORIGIN}\n⚠ {dropped_token}\n⚠ {helper}",
            f"{_USER_ORIGIN}\n⚠ {credential}\n⚠ {oauth}\n⚠ {unknown}",
        ]
        assert panel.get_config()[1].headers == {"Authorization": "Bearer {{TOKEN}}"}
        # A dropped default may be a secret, and so may the helper command: the saved description repeats neither.
        assert not any(
            "sk-live-abc" in config.description or "get-headers.sh" in config.description
            for config in panel.get_config()
        )
        # The credential is sent empty, as Claude Code sends it (the card trims the value it holds).
        assert panel.get_config()[2].headers == {"Authorization": "Bearer"}


@pytest.mark.parametrize("locale", ["en", "zh-Hans"])
@pytest.mark.parametrize(
    "variable, value, fragment_en, fragment_zh",
    [
        ("AWS_BEARER_TOKEN_BEDROCK", "", "empty values", "值为空"),
        ("API_KEY", "", "empty values", "值为空"),
        ("API_KEY", "  ", "empty values", "值为空"),
        ("API_KEY", " padded-secret ", "leading or trailing whitespace", "首尾空白"),
    ],
)
async def test_unsupported_environment_values_cannot_be_imported(
    claude_home: Path, locale: str, variable: str, value: str, fragment_en: str, fragment_zh: str
) -> None:
    _write_claude_servers(
        claude_home,
        {"blocked": {"command": "server", "env": {variable: value}}, "valid": {"command": "valid-server"}},
    )
    panel = MCPConfigPanel([])
    app = _PanelApp(panel)
    app.locale_controller = LocaleController(Settings(locale=locale))
    async with app.run_test(size=(120, 60)) as pilot:
        dialog = await _open_dialog(app, panel, pilot)
        await _wait_for_rows(dialog, pilot, 2)
        rows = {card.row.server_name: card.row for card in dialog.query(_ImportCard)}
        blocked, valid = rows["blocked"], rows["valid"]
        assert blocked.disabled and not blocked.value
        assert not valid.disabled
        text = _pane_text(dialog)
        assert f"mcpServers.blocked.env.{variable}" in text
        assert (fragment_en if locale == "en" else fragment_zh) in text
        assert "padded-secret" not in text
        assert dialog.query_one("#mcp-import-confirm", Button).disabled
        await _read_cards(dialog, pilot)
        assert variable in _pane_text(dialog)
        valid.value = True
        await _import_ticked(dialog, pilot)
        (card,) = await _cards(panel, pilot, 1)
        assert card.validate() == []
        assert [config.name for config in panel.get_config()] == ["valid"]


@pytest.mark.parametrize("locale", ["en", "zh-Hans"])
async def test_project_directory_warnings_group_defaults_without_hiding_actions(claude_home: Path, locale: str) -> None:
    _write_claude_servers(
        claude_home,
        {
            "directory": {
                "command": "server",
                "env": {
                    "MCP_IMPORT_TEST": "${CLAUDE_PROJECT_DIR}",
                    "WITH_DEFAULT": "${CLAUDE_PROJECT_DIR:-private-default-value}",
                },
            }
        },
    )
    panel = MCPConfigPanel([])
    app = _PanelApp(panel)
    app.locale_controller = LocaleController(Settings(locale=locale))
    async with app.run_test(size=(120, 60)) as pilot:
        dialog = await _open_dialog(app, panel, pilot)
        (row,) = await _wait_for_rows(dialog, pilot, 1)
        card = dialog.query_one(_ImportCard)
        # Warnings sit in the card's body, which the card builds when it is unfolded.
        await _read_cards(dialog, pilot)
        (text,) = _warning_texts(card)
        assert not text.startswith("⚠")
        assert "MCP_IMPORT_TEST, WITH_DEFAULT" in text
        assert (
            "The variable default in these fields will not be kept: WITH_DEFAULT."
            if locale == "en"
            else "以下字段中的变量默认值不会保留：WITH_DEFAULT。"  # noqa: RUF001
        ) in text
        assert ("before starting iCode" if locale == "en" else "启动 iCode 前") in text
        assert "private-default-value" not in _pane_text(dialog)
        assert not card.query(Collapsible)
        assert not card.query(".mcp-import-warning-fields, .mcp-import-warning-title")
        assert ("does not set automatically" if locale == "en" else "不会自动设置") in text
        # Folded back, as it opened.
        card.set_collapsed(True)
        await wait_for(lambda: card.collapsed and not card.query_one(".config-card-body").display, pilot=pilot)
        row.value = True
        await pilot.pause()
        # Ticking leaves the card folded; only a server just covered unfolds.
        assert card.collapsed is True
        assert row.value is True and row.disabled is False
        await _import_ticked(dialog, pilot)
        await _cards(panel, pilot, 1)
        (config,) = panel.get_config()
        assert config.env == {"MCP_IMPORT_TEST": "{{CLAUDE_PROJECT_DIR}}", "WITH_DEFAULT": "{{CLAUDE_PROJECT_DIR}}"}
        assert config.description.count("\n⚠ ") == 3
        assert "private-default-value" not in config.description


@pytest.mark.parametrize("locale", ["en", "zh-Hans"])
async def test_same_variable_warnings_group_fields_within_each_server(claude_home: Path, locale: str) -> None:
    _write_claude_servers(
        claude_home,
        {
            "defaults": {"command": "server", "env": {"A": "${TOKEN:-secret-a}", "B": "${TOKEN:-secret-b}"}},
            "credentials": {
                "type": "http",
                "url": "https://example.test/mcp",
                "headers": {"Authorization": "Bearer ${NPM_TOKEN}", "X-Token": "${NPM_TOKEN}"},
            },
            "other-default": {"command": "other", "env": {"C": "${TOKEN:-secret-c}"}},
        },
    )
    panel = MCPConfigPanel([])
    app = _PanelApp(panel)
    app.locale_controller = LocaleController(Settings(locale=locale))
    async with app.run_test(size=(120, 60)) as pilot:
        dialog = await _open_dialog(app, panel, pilot)
        await _wait_for_rows(dialog, pilot, 3)
        # Warnings sit in the card's body, which the card builds when it is unfolded.
        await _read_cards(dialog, pilot)
        cards = list(dialog.query(_ImportCard))
        texts = []
        for card in cards:
            (text,) = _warning_texts(card)
            texts.append(text)
            assert not card.query(Collapsible)
        assert "A, B" in texts[0] and "TOKEN" in texts[0]
        assert ("Set TOKEN before starting iCode" if locale == "en" else "启动 iCode 前设置 TOKEN") in texts[0]
        assert "Authorization, X-Token" in texts[1] and "NPM_TOKEN" in texts[1]
        assert ("Other text is kept" if locale == "en" else "其他文字保留") in texts[1]
        assert (
            "unintentionally sending local credentials" if locale == "en" else "意外向 MCP 服务器发送本机凭据"
        ) in texts[1]
        assert ("no remaining value are omitted" if locale == "en" else "没有剩余值的请求头会被省略") in texts[1]
        assert "F8" not in texts[1]
        assert ("different name" if locale == "en" else "另一个") not in texts[1]
        assert "C" in texts[2] and "TOKEN" in texts[2]
        assert not dialog.query(".mcp-import-warning-fields, .mcp-import-warning-title")
        assert "secret-" not in "\n".join(texts)


@pytest.mark.parametrize("locale", ["en", "zh-Hans"])
async def test_warning_values_wrap_to_available_width(claude_home: Path, locale: str) -> None:
    _write_claude_servers(
        claude_home,
        {"defaults": {"command": "server", "env": {"X-Test": "${ICODE_TEST_UNSET:-private-default}"}}},
    )
    app = _PanelApp(MCPConfigPanel([]))
    app.locale_controller = LocaleController(Settings(locale=locale))
    async with app.run_test(size=(120, 42)) as pilot:
        dialog = await _open_dialog(app, app._panel, pilot)
        await _wait_for_rows(dialog, pilot, 1)
        # The card opens folded; the wrapping assertions below need it laid out.
        _expand_cards(dialog)
        await pilot.pause()
        warning = dialog.query_one(".mcp-import-warning", Static)
        wide_width, wide_height = warning.size.width, warning.size.height
        text = str(warning.render())
        assert "ICODE_TEST_UNSET" in text and "X-Test" in text
        assert "\n" not in text
        assert not dialog.query(".mcp-import-warning-fields, .mcp-import-warning-title")
        assert not dialog.query(Collapsible)
        await pilot.resize_terminal(80, 42)
        await pilot.pause()
        assert warning.size.width < wide_width
        assert warning.size.height > wide_height
        assert warning.region.right <= dialog.query_one(_ImportCard).content_region.right
        assert "private-default" not in _pane_text(dialog)


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
        assert "doesn't support environment variable references in URLs" in text
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


async def test_imported_environment_survives_editing_saving_and_runtime_resolution(
    claude_home: Path, tmp_path: Path
) -> None:
    from chrys.service.mcp.cache import resolve_mcp_effective_config
    from chrys.service.profiles.agents.loader import load_profile_from_yaml
    from chrys.service.profiles.agents.schema import AgentProfile
    from chrys.service.profiles.agents.serializer import save_profile

    env = {"PASSWORD": "secret", "SPACES": "a b", "LINES": "first\nsecond"}
    _write_claude_servers(claude_home, {"values": {"command": "server", "env": env}})
    panel = MCPConfigPanel([])
    app = _PanelApp(panel)
    async with app.run_test(size=(120, 40)) as pilot:
        dialog = await _open_dialog(app, panel, pilot)
        (row,) = await _wait_for_rows(dialog, pilot, 1)
        row.value = True
        await _import_ticked(dialog, pilot)
        (card,) = await _cards(panel, pilot, 1)
        assert card.validate() == []
        configs = panel.get_config()
        assert configs[0].env == env
        path = save_profile(
            AgentProfile(name="Imported", tools=ToolsConfig(mcp=configs)), target_dir=tmp_path / "profiles"
        )
        (loaded,) = load_profile_from_yaml(path).tools.mcp
        effective = resolve_mcp_effective_config(loaded)
        assert effective.stdio_env is not None
        assert {key: effective.stdio_env[key] for key in env} == env


@pytest.mark.parametrize(
    ("source_ms", "seconds", "preview"),
    [(1000, 1, "1 s"), (1501, 2, "2 s (source: 1501 ms)")],
    ids=["one-second", "rounded-up"],
)
async def test_claude_timeout_and_literal_braces_survive_preview_edit_and_save(
    claude_home: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    source_ms: int,
    seconds: int,
    preview: str,
) -> None:
    from chrys.service.mcp._connection import DEFAULT_CONNECT_TIMEOUT_SECONDS, _create_tool_with_timeout_floor
    from chrys.service.mcp.cache import resolve_mcp_effective_config
    from chrys.service.profiles.agents.loader import load_profile_from_yaml
    from chrys.service.profiles.agents.schema import AgentProfile
    from chrys.service.profiles.agents.serializer import save_profile

    monkeypatch.setenv("ICODE_TEST_PADDED", "plain-value")
    _write_claude_servers(
        claude_home,
        {
            "converted": {
                "type": "http",
                "url": "https://example.test/mcp",
                "timeout": source_ms,
                "headers": {"X-Test": "${ICODE_TEST_PADDED}", "X-Literal": "{{literal}}"},
                "tools": [{"name": "inspect", "permission_policy": "always_deny"}],
                "toolPermissions": {"inspect": "ask"},
            },
            "plain": {"command": "server"},
        },
    )
    panel = MCPConfigPanel([])
    app = _PanelApp(panel)
    async with app.run_test(size=(120, 40)) as pilot:
        dialog = await _open_dialog(app, panel, pilot)
        converted, _plain = await _wait_for_rows(dialog, pilot, 2)
        # The timeout, the escaped braces and the dropped settings sit in the card's body.
        await _read_cards(dialog, pilot)
        text = _pane_text(dialog)
        assert preview in text
        assert "1 seconds" not in text
        assert text.count("MCP Request Timeout") == 1
        assert "Braces look different in the preview; the text sent to the server is unchanged." in text
        assert "Tool permissions from Claude Code are not imported (toolPermissions)." in text
        assert "These source settings are not imported: tools." in text
        converted.value = True
        await _import_ticked(dialog, pilot)
        await _cards(panel, pilot, 1)
        (config,) = panel.get_config()
        assert config.headers == {"X-Test": "{{ICODE_TEST_PADDED}}", "X-Literal": "{{{{literal}}}}"}
        path = save_profile(
            AgentProfile(name="Imported", tools=ToolsConfig(mcp=[config])), target_dir=tmp_path / "profiles"
        )
        (loaded,) = load_profile_from_yaml(path).tools.mcp
        assert loaded == config
        effective = resolve_mcp_effective_config(loaded)
        tool = _create_tool_with_timeout_floor(loaded, DEFAULT_CONNECT_TIMEOUT_SECONDS)
        assert tool.request_timeout == loaded.request_timeout == seconds
        assert effective.http_headers == tool._static_headers == {"X-Test": "plain-value", "X-Literal": "{{literal}}"}


def _ticks(dialog: ImportMcpServersDialog) -> list[tuple[str, bool, bool]]:
    """Each card's server name, whether its checkbox is ticked and whether it can be changed, in pane order."""
    return [(card.row.server_name, card.row.value, not card.row.disabled) for card in dialog.query(_ImportCard)]


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


async def test_dialog_says_when_no_supported_mcp_configuration_is_found(tmp_path: Path) -> None:
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
            lambda: dialog.is_mounted and "No MCP configuration found" in _pane_text(dialog),
            pilot=pilot,
            description="no-agents message",
        )
        assert "supported applications: Claude Code, Codex, opencode, pi." in _pane_text(dialog)
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
        sources.highlighted = sources.get_option_index("second")
        await _wait_for_cards(dialog, pilot, ["c"], "second agent's servers are listed")
        _row(dialog, 0).value = True
        sources.highlighted = sources.get_option_index("first")
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
        covered = "Same configuration as the selected github (First Agent, user). It will be imported once."

        user.value = True
        await wait_for(lambda: project.disabled, pilot=pilot, description="the same server in another scope is locked")
        assert _ticks(dialog) == [("github", True, True), ("github", False, False), ("fs", False, True)]
        assert [card.has_class("-unavailable") for card in dialog.query(_ImportCard)] == [False, True, False]
        # A folded duplicate shows a short status; its explanation appears on unfolding.
        assert _hints(dialog) == []
        assert _titles(dialog)[1].endswith("Duplicate")
        dialog.query(_ImportCard)[1].set_collapsed(False)
        assert _hints(dialog) == [covered]

        # Unticking frees it again.
        user.value = False
        await wait_for(lambda: not project.disabled, pilot=pilot, description="unticking unlocks the other scope")
        assert _hints(dialog) == []

        # The lock spans agents.
        user.value = True
        await wait_for(lambda: project.disabled, pilot=pilot, description="ticked again")
        sources = dialog.query_one("#mcp-import-sources", OptionList)
        sources.highlighted = sources.get_option_index("second")
        await _wait_for_cards(dialog, pilot, ["gh"], "second agent's servers are listed")
        assert _ticks(dialog) == [("gh", False, False)]
        assert covered in _pane_text(dialog)

        await _import_ticked(dialog, pilot)
        await wait_for(lambda: results, pilot=pilot, description="dialog dismissed with the ticked servers")
        (imported,) = results
        assert imported is not None
        assert [(config.name, config.command) for config in imported] == [("github", "npx")]


@pytest.mark.parametrize("transport", ["stdio", "http"])
async def test_same_endpoint_with_different_credentials_can_be_imported_together(
    tmp_path: Path, transport: str
) -> None:
    configs = (
        [
            MCPServerConfig(name=name, transport="stdio", command="server", env={"DATABASE_URL": name})
            for name in ("a", "b")
        ]
        if transport == "stdio"
        else [
            MCPServerConfig(name=name, transport="http", url="https://x.test/mcp", headers={"Authorization": name})
            for name in ("a", "b")
        ]
    )
    source = _ListedSource("example", "Example", tuple(_candidate(c.name, "user", c) for c in configs))
    results: list[list[MCPServerConfig] | None] = []
    app = _PanelApp(MCPConfigPanel([]))
    async with app.run_test(size=(120, 40)) as pilot:
        dialog = _open_with(app, [source], tmp_path, results)
        first, second = await _wait_for_rows(dialog, pilot, 2)
        first.value = True
        await pilot.pause()
        assert not second.disabled
        second.value = True
        await _import_ticked(dialog, pilot)
        await wait_for(lambda: results, pilot=pilot, description="both configurations imported")
        imported = results[0]
        assert imported is not None
        assert [(c.env, c.headers) for c in imported] == [(c.env, c.headers) for c in configs]


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


async def test_imported_servers_are_named_after_their_server_so_their_tools_dont_clash(tmp_path: Path) -> None:
    # Servers whose source names their tools after them, so they may share tool names; one has a prefix already.
    candidates = (
        _candidate("lab-cc-helper", "user", _stdio("lab-cc-helper", "helper")),
        _candidate("my files.v2", "user", _stdio("my files.v2", "files")),
        _candidate("文件系统", "user", _stdio("文件系统", "fs")),
        _candidate("github", "user", replace(_stdio("github", "gh"), tool_name_prefix="gh")),
        # Too long for a prefix that leaves room for the tools' names: shortened, with a hash of the name.
        _candidate("s" * 50, "user", replace(_stdio("s" * 50, "long"), use_progressive_disclosure=True)),
        McpImportCandidate("legacy", "user", "/user", None, UnsupportedReason.UNSUPPORTED_TRANSPORT, transport="sse"),
    )
    results: list[list[MCPServerConfig] | None] = []
    app = _PanelApp(MCPConfigPanel([]))
    async with app.run_test(size=(120, 40)) as pilot:
        dialog = _open_with(app, [_ListedSource("first", "First Agent", candidates)], tmp_path, results)
        rows = await _wait_for_rows(dialog, pilot, 6)

        # Each card says the prefix it will get; one that can't be imported shows none.
        assert [
            (name, [value for label, value in fields if label == "Tool Name Prefix"])
            for name, fields, _tickable in await _read_cards(dialog, pilot)
        ] == [
            ("lab-cc-helper", ["lab-cc-helper"]),
            ("my files.v2", ["my_files_v2"]),
            ("文件系统", ["mcp_47b61747"]),
            ("github", ["gh"]),
            ("s" * 50, ["sssssssssssssssss-089f83"]),
            ("legacy", []),
        ]

        for row in rows[:5]:
            row.value = True
        await _import_ticked(dialog, pilot)
        await wait_for(lambda: results, pilot=pilot, description="dialog dismissed with the ticked servers")
        (imported,) = results
        assert imported is not None
        assert [(config.name, config.tool_name_prefix) for config in imported] == [
            ("lab-cc-helper", "lab-cc-helper"),
            ("my files.v2", "my_files_v2"),
            ("文件系统", "mcp_47b61747"),
            ("github", "gh"),
            ("s" * 50, "sssssssssssssssss-089f83"),
        ]
        # Nothing else of the server changes.
        assert [replace(config, tool_name_prefix="", description="") for config in imported] == [
            replace(candidate.config, tool_name_prefix="")
            for candidate in candidates[:5]
            if candidate.config is not None
        ]


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
        for source_id in ("second", "first"):
            index = sources.get_option_index(source_id)
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


@pytest.mark.parametrize("refresh", ["source", "locale"])
@pytest.mark.parametrize("failed", [False, True])
async def test_pane_rebuilds_share_an_inflight_source_result(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, refresh: str, failed: bool
) -> None:
    gate = threading.Event()
    calls: list[ImportContext] = []
    first = _ListedSource("first", "First Agent", (_candidate("old-server", "user", _stdio("old-server")),))
    second = _ListedSource("second", "Second Agent", ())

    def discover(ctx: ImportContext) -> McpImportResult:
        calls.append(ctx)
        if len(calls) > 1:
            return McpImportResult((_candidate("new-server", "user", _stdio("new-server")),))
        if not gate.wait(timeout=_DEFAULT_WAIT_TIMEOUT):
            raise TimeoutError("the test never released discover")
        if failed:
            raise RuntimeError("source read failed")
        return McpImportResult(first.candidates)

    monkeypatch.setattr(first, "discover", discover)
    app = _PanelApp(MCPConfigPanel([]))
    app.locale_controller = LocaleController(Settings(locale="en"))
    async with app.run_test(size=(120, 40)) as pilot:
        dialog = _open_with(app, [first, second], tmp_path)
        try:
            await wait_for(lambda: bool(calls), pilot=pilot, description="the first source is loading")
            if refresh == "locale":
                app.locale_controller.switch_locale("zh-Hans")
            else:
                dialog._request_source("second")
                dialog._request_source("first")
            await pilot.pause()
            assert len(calls) == 1
        finally:
            gate.set()
        await wait_for(
            lambda: (
                "first" in dialog._results
                and all(worker.is_finished for worker in app.workers if worker.node is dialog)
            ),
            pilot=pilot,
            timeout=_DEFAULT_WAIT_TIMEOUT,
            description="all pane workers use the same completed read",
        )
        if failed:
            assert dialog._results["first"] is None
            assert not dialog.query(_ImportRow)
        else:
            assert _ticks(dialog) == [("old-server", False, True)]
            _row(dialog, 0).value = True
            await pilot.pause()
            assert [config.name for config in dialog._selected()] == ["old-server"]
        dialog.refresh_localization()
        await pilot.pause()
        assert len(calls) == 1


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
            "Couldn't load MCP configurations from First Agent. Reopen import to try again. If it still fails, see the User Guide (F8) to locate iCode's debug.log.",
            "Showing the MCP servers of First Agent failed",
        ),
        (
            "_list_sources",
            _broken_detect,
            "Couldn't search for MCP configurations. Reopen import to try again. If it still fails, see the User Guide (F8) to locate iCode's debug.log.",
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
    # Its own controller: the class-level one is shared by every test in this module.
    app.locale_controller = LocaleController(Settings(locale="en"))
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
            # Red, as a file that can't be read, and still red once re-rendered in another language.
            assert dialog.query_one("#mcp-import-servers Static").has_class("mcp-import-error")
            app.locale_controller.switch_locale("zh-Hans")
            await wait_for(
                lambda: _pane_text(dialog) not in ("", message),
                pilot=pilot,
                description="the failure is rendered again in Chinese",
            )
            assert dialog.query_one("#mcp-import-servers Static").has_class("mcp-import-error")
    # A broken pane fails again when the switch rebuilds it; detection runs once.
    assert {record.getMessage() for record in caplog.records if record.exc_info} == {logged}


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

    assert text and "{" not in text.replace("{{VAR}}", "").replace("{{!command}}", "")


@pytest.mark.parametrize("reason", list(UnsupportedReason))
@pytest.mark.parametrize("locale", ["en", "zh-Hans"])
def test_every_unsupported_reason_has_a_message(reason: UnsupportedReason, locale: str) -> None:
    dialog = ImportMcpServersDialog(
        workspace_cwd=None, existing=[], locale_controller=LocaleController(Settings(locale=locale)), sources=[]
    )
    candidate = McpImportCandidate(
        "s",
        "project",
        "/p",
        None,
        unsupported=reason,
        unsupported_field="headers.X-Test",
        unsupported_variable="BAD-NAME",
    )

    text = dialog._unsupported_note(candidate, "pi")

    assert text and "{" not in text.replace("{{!command}}", "")
    assert "headers.X-Test" in text


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
  "agent": {"build": {"tools": {"files_write_note": false}}},
  "mcp": {
    "files": {"type": "local", "command": ["npx", "server-files"], "environment": {"T": "{env:FILES_TOKEN}"},
              "enabled": false},
  },
}""",
        encoding="utf-8",
    )
    pi_project = workspace / ".pi" / "mcp.json"
    pi_project.parent.mkdir(parents=True)
    description = "Searches the [bold]product docs[/bold]\nReads API references and examples."
    pi_project.write_text(
        json.dumps({"mcpServers": {"docs": {"url": "https://docs.test/mcp", "description": description}}}),
        encoding="utf-8",
    )
    environ = {**_quiet_environ(tmp_path), "XDG_CONFIG_HOME": str(home / ".config")}
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
        opencode_path = _source(opencode_config)
        assert await _read_cards(dialog, pilot) == [
            (
                "files",
                [
                    opencode_path,
                    _scope("opencode", "global, used in every project"),
                    ("Enabled", "No"),
                    _transport("STDIO"),
                    ("Command", format_mcp_command_line("npx", ["server-files"])),
                    ("Environment Variables", [("T", "{{FILES_TOKEN}}")]),
                    ("Available Tool Scope", "All server tools"),
                    ("Loading Strategy", "Full — load all available tools"),
                    _prefix("files"),
                    ("MCP Request Timeout", "60 s"),
                ],
                True,
            )
        ]
        assert not dialog.query(".mcp-import-warning")
        files.value = True

        sources.highlighted = sources.get_option_index("pi")
        await _wait_for_cards(dialog, pilot, ["docs"], "pi's servers are shown")
        assert await _read_cards(dialog, pilot) == [
            (
                "docs",
                [
                    _source(pi_project),
                    _scope("pi", f"project, used only in {workspace}"),
                    ("Scope After Import", "Available to this agent in every project"),
                    _transport("HTTP"),
                    ("Description", description),
                    ("URL", "https://docs.test/mcp"),
                    ("Available Tool Scope", "All server tools"),
                    ("Loading Strategy", "On demand — load tools as needed"),
                    ("Initially Visible Tools (optional)", "No tools"),
                    _prefix("docs"),
                    ("MCP Request Timeout", "60 s"),
                ],
                True,
            )
        ]
        description_widget = dialog.query(".mcp-import-field-value")[1]
        # The card opened folded; unfold it so the two lines wrap once the switched pane is laid out.
        _expand_cards(dialog)
        await pilot.pause()
        await wait_for(lambda: description_widget.region.height >= 2, pilot=pilot, description="the description wraps")
        # The source area, then the transport and description the MCP card shows, then its boxes: the
        # transport's options, Tool Access, Naming & Limits.
        (card,) = dialog.query(_ImportCard)
        order = [
            "box" if widget.has_class("mcp-import-section") else str(widget.render())
            for widget in card.query(".mcp-import-field-label, .mcp-import-section")
        ]
        assert order == [
            "Configuration Source",
            "Scope in pi",
            "Scope After Import",
            "Transport",
            "Description",
            "box",
            "URL",
            "box",
            "Available Tool Scope",
            "box",
            "Loading Strategy",
            "Initially Visible Tools (optional)",
            "box",
            "Tool Name Prefix",
            "MCP Request Timeout",
        ]
        assert str(card.query(".mcp-import-section")[0].border_title) == "HTTP Options"
        assert not dialog.query(".mcp-import-notice")
        (docs,) = await _wait_for_rows(dialog, pilot, 1)
        docs.value = True
        await _import_ticked(dialog, pilot)

        await wait_for(lambda: results, pilot=pilot, description="dialog dismissed with the ticked servers")
        (imported,) = results
        assert imported is not None
        assert [(config.name, config.enabled, config.tool_name_prefix, config.description) for config in imported] == [
            (
                "files",
                False,
                "files",
                "Imported from opencode. There it is a global server, used in every project.",
            ),
            (
                "docs",
                True,
                "docs",
                (f"{description}\nImported from pi. There it is a project server, used only in {workspace}."),
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
        files.value = True
        await _import_ticked(dialog, pilot)
        await wait_for(lambda: results, pilot=pilot, description="Codex server imported")
        (imported,) = results
        assert imported is not None and len(imported) == 1
        (server,) = imported
        assert server.name == "files"
        assert server.command == "server-files"
        assert server.env == {}
        assert server.enabled is False
        assert server.request_timeout == 42
        assert server.allowed_tools == ["read"]
        assert server.description.startswith("Imported from Codex.")


@pytest.mark.parametrize("source_id", ["pi", "claude_code", "codex"])
async def test_command_migration_guidance_is_shown_without_copying_the_command(tmp_path: Path, source_id: str) -> None:
    home = tmp_path / "home"
    command = "credential-helper --secret private-command-argument"
    if source_id == "codex":
        path = home / ".codex" / "config.toml"
        text = (
            '[mcp_servers.remote]\nurl = "https://example.test/mcp"\nhttp_headers_helper = '
            + json.dumps(command)
            + "\n"
        )
    else:
        path = home / ".pi" / "agent" / "mcp.json" if source_id == "pi" else home / ".claude.json"
        server: dict[str, Any] = {"type": "http", "url": "https://example.test/mcp"}
        server["headers" if source_id == "pi" else "headersHelper"] = (
            {"Authorization": "!" + command} if source_id == "pi" else command
        )
        text = json.dumps({"mcpServers": {"remote": server}})
    path.parent.mkdir(parents=True)
    path.write_text(text, encoding="utf-8")
    results: list[list[MCPServerConfig] | None] = []
    app = _PanelApp(MCPConfigPanel([]))
    app.locale_controller = LocaleController(Settings(locale="en"))
    async with app.run_test(size=(120, 40)) as pilot:
        dialog = ImportMcpServersDialog(
            workspace_cwd=None,
            existing=[],
            locale_controller=app.locale_controller,
            context=ImportContext(None, home, _quiet_environ(tmp_path)),
        )
        app.push_screen(dialog, callback=results.append)
        (row,) = await _wait_for_rows(dialog, pilot, 1)
        assert row.disabled is (source_id == "pi")
        # A refused server says why under its title; an imported one says what changed in its body.
        await _read_cards(dialog, pilot)
        guidance = _guidance(dialog)
        assert "{{!command}}" in guidance
        assert "User Guide (F8)" in guidance
        assert command not in guidance and "private-command-argument" not in guidance

        app.locale_controller.switch_locale("zh-Hans")
        await wait_for(
            lambda: ("手动" if source_id == "pi" else "逐项添加所需请求头") in _guidance(dialog),
            pilot=pilot,
            description="the command migration guidance is translated",
        )
        if source_id != "pi":
            (row,) = await _wait_for_rows(dialog, pilot, 1)
            row.value = True
            await _import_ticked(dialog, pilot)
            await wait_for(lambda: results, pilot=pilot, description="the server with omitted helper is imported")
            (imported,) = results
            assert imported is not None
            (config,) = imported
            assert "{{!command}}" in config.description
            assert "private-command-argument" not in config.description


def _option_list_lines(option_list: OptionList) -> list[str]:
    return [option_list.render_line(y).text.strip() for y in range(option_list.size.height)]


async def test_sidebar_hover_keeps_separator_background_when_focus_returns(tmp_path: Path) -> None:
    sources = [
        _ListedSource(source_id, source_id, (_candidate(source_id, "user", _stdio(source_id, source_id)),))
        for source_id in ("first", "second", "third")
    ]
    app = _PanelApp(MCPConfigPanel([]))
    app.register_theme(CHRYS_THEME)
    app.theme = CHRYS_THEME.name
    async with app.run_test(size=(120, 40)) as pilot:
        dialog = _open_with(app, sources, tmp_path)
        await _wait_for_rows(dialog, pilot, 1)
        option_list = dialog.query_one("#mcp-import-sources", OptionList)
        dialog.query_one("#mcp-import-cancel", Button).focus()
        await pilot.hover(option_list, offset=(1, 2))
        await pilot.pause()
        # Cache the hovered option together with the separator beneath it while blurred.
        option_list.render_line(2)
        option_list.focus()
        await pilot.pause()
        background = option_list.rich_style.bgcolor
        for y in (1, 3, 4, 5):
            assert all(segment.style.bgcolor == background for segment in option_list.render_line(y))


async def test_agents_in_the_sidebar_are_divided_by_rules_the_cursor_skips(tmp_path: Path) -> None:
    sources = [
        _ListedSource(source_id, name, (_candidate(source_id, "user", _stdio(source_id, source_id)),))
        for source_id, name in (("first", "First Agent"), ("second", "Second Agent"), ("third", "Third Agent"))
    ]
    app = _PanelApp(MCPConfigPanel([]))
    async with app.run_test(size=(120, 40)) as pilot:
        dialog = _open_with(app, sources, tmp_path)
        await _wait_for_rows(dialog, pilot, 1)
        option_list = dialog.query_one("#mcp-import-sources", OptionList)

        lines = [line for line in _option_list_lines(option_list) if line]
        # A rule between each two agents, as between models in the model list, and none after the last.
        assert [line if not set(line) <= {"─"} else "rule" for line in lines] == [
            "First Agent",
            "rule",
            "Second Agent",
            "rule",
            "Third Agent",
        ]
        await pilot.press("down")
        await wait_for(
            lambda: _ticks(dialog) == [("second", False, True)],
            pilot=pilot,
            description="the cursor moved past the rule to the next agent",
        )
        assert option_list.highlighted == option_list.get_option_index("second")


async def test_an_agent_with_no_servers_shows_the_hatched_empty_state_of_the_mcp_tab(tmp_path: Path) -> None:
    app = _PanelApp(MCPConfigPanel([]))
    async with app.run_test(size=(120, 40)) as pilot:
        dialog = _open_with(app, [_ListedSource("empty", "Empty Agent", ())], tmp_path)
        await wait_for(
            lambda: bool(dialog.query(HatchedEmptyState)), pilot=pilot, description="the empty state is shown"
        )
        (empty,) = dialog.query(HatchedEmptyState)

        # Empty sources need only the short hatch label.
        assert empty.label == Localizer("en").render(_EMPTY_SOURCE.bind())
        assert empty.has_class("mcp-import-empty")
        assert not dialog.query(".mcp-import-message")
        # It ends where the cards end, a line above the buttons.
        assert (empty.styles.margin.right, empty.styles.margin.bottom) == (1, 1)


async def test_a_server_whose_source_names_no_transport_has_no_transport_field(tmp_path: Path) -> None:
    untyped = McpImportCandidate("untyped", "user", "/user", _stdio("untyped", "uvx"))
    app = _PanelApp(MCPConfigPanel([]))
    async with app.run_test(size=(120, 40)) as pilot:
        dialog = _open_with(app, [_ListedSource("plain", "Plain Agent", (untyped,))], tmp_path)
        await _wait_for_rows(dialog, pilot, 1)

        assert await _read_cards(dialog, pilot) == [
            (
                "untyped",
                [
                    _source("/user"),
                    _scope("Plain Agent", "user, used in every project"),
                    ("Command", format_mcp_command_line("uvx", [])),
                    ("Available Tool Scope", "All server tools"),
                    ("Loading Strategy", "Full — load all available tools"),
                    _prefix("untyped"),
                ],
                True,
            )
        ]
        # Nor a transport in the options box's title.
        assert not dialog.query_one(".mcp-import-section").border_title


@dataclass
class _UnreadableSource:
    """An agent whose only config file can't be read."""

    source_id: str = "broken"
    display_name: str = "Broken Agent"

    def detect(self, ctx: ImportContext) -> bool:
        return True

    def discover(self, ctx: ImportContext) -> McpImportResult:
        return McpImportResult(source_errors=(McpSourceError("/broken.json", SourceErrorKind.INVALID_JSON),))


async def test_a_file_that_cant_be_read_is_not_followed_by_the_empty_state(tmp_path: Path) -> None:
    app = _PanelApp(MCPConfigPanel([]))
    async with app.run_test(size=(120, 40)) as pilot:
        dialog = _open_with(app, [_UnreadableSource()], tmp_path)
        await wait_for(
            lambda: bool(dialog.query(".mcp-import-error")), pilot=pilot, description="the file error is shown"
        )

        # The red error already says why nothing is listed; "No MCP servers configured" would contradict it.
        assert not dialog.query(HatchedEmptyState)


async def test_cards_and_the_empty_state_follow_a_locale_switch(tmp_path: Path) -> None:
    listed = _ListedSource(
        "listed", "Listed Agent", (_candidate("a", "user", replace(_stdio("a", "uvx"), enabled=False)),)
    )
    empty = _ListedSource("empty", "Empty Agent", ())
    app = _PanelApp(MCPConfigPanel([]))
    # Its own controller: the class-level one is shared by every test in this module.
    app.locale_controller = LocaleController(Settings(locale="en"))
    async with app.run_test(size=(120, 40)) as pilot:
        dialog = _open_with(app, [listed, empty], tmp_path)
        await _wait_for_rows(dialog, pilot, 1)

        app.locale_controller.switch_locale("zh-Hans")
        chinese = Localizer("zh-Hans")
        scope = chinese.render(_SCOPE.bind(agent="Listed Agent"))
        # The pane is rebuilt folded; unfold it again to read the fields it composes in Chinese.
        await wait_for(
            lambda: bool(dialog.query(_ImportCard)) and screen_is_settled(app, dialog),
            pilot=pilot,
            description="the card is rebuilt after the locale switch",
        )
        fields = (await _read_cards(dialog, pilot))[0][1]
        assert fields[1] == (scope, "用户，在所有项目中使用")  # noqa: RUF001
        assert fields[2] == ("启用", "否")

        sources = dialog.query_one("#mcp-import-sources", OptionList)
        sources.highlighted = sources.get_option_index("empty")
        await wait_for(lambda: bool(dialog.query(HatchedEmptyState)), pilot=pilot, description="the empty state")
        assert dialog.query_one(HatchedEmptyState).label == chinese.render(_EMPTY_SOURCE.bind())
