# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""The MCP import cards: one-line titles, lazy bodies, folds and ticks that stay apart."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
from textual.geometry import Region
from textual.widgets import Button, Input, OptionList

from chrys.app.tui.screens.agents.import_mcp_dialog import _ImportCard
from chrys.app.tui.screens.agents.panels.mcp import MCPConfigPanel, MCPConnectionCard
from chrys.service.mcp.importers import ImportWarning, ImportWarningKind
from chrys.service.profiles.agents.schema import MCPServerConfig
from tests.app.tui.screens._mcp_import_support import (
    _candidate,
    _ListedSource,
    _open_with,
    _PanelApp,
    _row,
    _stdio,
    _titles,
    _wait_for_cards,
    _wait_for_rows,
)
from tests.support.pilot_barrier import screen_is_settled
from tests.support.tui_helpers import click_when_settled, resize_when_settled
from tests.support.waiting import wait_for

pytestmark = [pytest.mark.timeout(120), pytest.mark.usefixtures("isolated_chrys_config_dir")]

_HOST = "mcp.example.company-internal.test"
_LONG_NAME = "a-very-long-server-name-for-the-narrow-title"


@pytest.mark.parametrize("name", ["long-server-name-" * 12, "服务器名称很长的配置-" * 12])
async def test_mcp_configuration_long_titles_paint_an_ellipsis_and_keep_the_name_field(name: str) -> None:
    app = _PanelApp(MCPConfigPanel([_stdio(name)]))
    async with app.run_test(size=(80, 40)) as pilot:
        await wait_for(
            lambda: bool(app.query(MCPConnectionCard)) and screen_is_settled(app, app.screen),
            pilot=pilot,
            description="the MCP configuration card is painted",
        )
        card = app.query_one(MCPConnectionCard)
        title = card.query_one(".mcp-title")
        for collapsed in (True, False, True):
            card.set_collapsed(collapsed)
            await wait_for(lambda: screen_is_settled(app, app.screen), pilot=pilot, description="the card settles")
            painted = "".join(strip.text for strip in title.render_lines(Region(0, 0, title.region.width, 1))).strip()
            assert painted.startswith(f"{'▶' if collapsed else '▼'} {name[:4]}")
            assert painted.endswith("…")
            assert painted in app.screen._compositor.render_strips()[title.region.y].text
            assert title.region.height == 1
            assert card.query_one("#mcp-name-0", Input).value == name
            assert card.header_title == name


async def _open(app: _PanelApp, pilot, tmp_path: Path, candidates: tuple[Any, ...], *, count: int) -> Any:
    dialog = _open_with(app, [_ListedSource("first", "First Agent", candidates)], tmp_path)
    await wait_for(lambda: dialog.is_mounted, pilot=pilot, description="import dialog is open")
    await _wait_for_rows(dialog, pilot, count)
    return dialog


async def test_the_title_keeps_the_name_and_gives_the_summary_up_first(tmp_path: Path) -> None:
    """The target is cut, then the summary is dropped; the name is cut last."""
    app = _PanelApp(MCPConfigPanel([]))
    async with app.run_test(size=(120, 40)) as pilot:
        dialog = await _open(
            app,
            pilot,
            tmp_path,
            (
                _candidate(
                    "remote", "user", MCPServerConfig(name="remote", transport="http", url=f"https://{_HOST}/mcp")
                ),
                _candidate(
                    "nine-name", "user", MCPServerConfig(name="nine", transport="http", url="https://example.com/mcp")
                ),
                _candidate(_LONG_NAME, "user", _stdio(_LONG_NAME, "npx")),
            ),
            count=3,
        )
        assert _titles(dialog) == [
            f"▶ remote: HTTP · {_HOST}",
            "▶ nine-name: HTTP · example.com",
            f"▶ {_LONG_NAME}: STDIO · npx",
        ]

        # Narrower, each host gives way before the name.
        await resize_when_settled(pilot, 90, 40)
        assert _titles(dialog)[:2] == [
            "▶ remote: HTTP · mcp.example.company-interna…",
            "▶ nine-name: HTTP · example.com",
        ]
        # Narrower still, only the start of each host fits.
        await resize_when_settled(pilot, 60, 40)
        assert _titles(dialog)[:2] == ["▶ remote: HTTP · mcp.ex…", "▶ nine-name: HTTP · exa…"]
        # …while a name too long for the line gives its summary up and is cut last.
        assert _titles(dialog)[2] == "▶ a-very-long-server-na…"
        # No title line ever wraps, and none runs past its card.
        for card in dialog.query(_ImportCard):
            title = card.query_one(".mcp-import-title")
            assert title.region.height == 1
            assert title.region.right <= card.content_region.right


@pytest.mark.parametrize("name", [_LONG_NAME, "服务器名称很长的配置-" * 5])
async def test_a_long_name_stays_on_one_line_with_its_warning(tmp_path: Path, name: str) -> None:
    candidate = replace(
        _candidate(name, "user", _stdio(name, "python")),
        warnings=(ImportWarning(ImportWarningKind.TOOL_PERMISSIONS_IGNORED, field="tools"),),
    )
    app = _PanelApp(MCPConfigPanel([]))
    async with app.run_test(size=(120, 40)) as pilot:
        dialog = await _open(app, pilot, tmp_path, (candidate,), count=1)
        card = dialog.query_one(_ImportCard)
        for width in (120, 90, 60):
            await resize_when_settled(pilot, width, 40)
            (line,) = _titles(dialog)
            assert line.endswith("  ⚠")
            assert line.startswith(f"▶ {name[:4]}")
            title = card.query_one(".mcp-import-title")
            assert title.region.height == 1
            assert title.region.right <= card.content_region.right
        assert "…" in line
        assert "STDIO" not in line
        card.set_collapsed(False)
        await wait_for(
            lambda: bool(card.query(".mcp-import-section")) and screen_is_settled(app, dialog),
            pilot=pilot,
            description="the long-name card unfolds",
        )
        assert not card.query(".mcp-import-full-name")
        assert card.query_one(".mcp-import-title").region.height == 1
        assert "STDIO" not in _titles(dialog)[0]
        assert "…" in _titles(dialog)[0]
        await resize_when_settled(pilot, 120, 40)
        assert "STDIO" not in _titles(dialog)[0]
        assert card.query_one(".mcp-import-title").region.height == 1


async def test_a_body_is_built_on_the_first_unfold_and_reused_after(tmp_path: Path) -> None:
    app = _PanelApp(MCPConfigPanel([]))
    async with app.run_test(size=(120, 40)) as pilot:
        dialog = await _open(app, pilot, tmp_path, (_candidate("fs", "user", _stdio("fs", "uvx")),), count=1)
        card = dialog.query_one(_ImportCard)
        # Folded, the card shows only its title line: nothing is built that nobody reads.
        assert card.collapsed is True
        assert not card.query(".mcp-import-section")
        assert not card.query(".mcp-import-field-value")

        await click_when_settled(pilot, card.query_one(".mcp-import-title"))
        await wait_for(lambda: bool(card.query(".mcp-import-section")), pilot=pilot, description="the body is built")
        built = list(card.query(".mcp-import-section"))

        card.set_collapsed(True)
        await wait_for(lambda: card.query_one(".config-card-body").display is False, pilot=pilot, description="folded")
        card.set_collapsed(False)
        await wait_for(lambda: card.query_one(".config-card-body").display is True, pilot=pilot, description="unfolded")
        # The same widgets come back: the body is built once, not again on every unfold.
        assert list(card.query(".mcp-import-section")) == built
        assert all(section.is_attached for section in built)


async def test_the_checkbox_and_the_title_line_stay_two_targets(tmp_path: Path) -> None:
    app = _PanelApp(MCPConfigPanel([]))
    async with app.run_test(size=(120, 40)) as pilot:
        dialog = await _open(
            app,
            pilot,
            tmp_path,
            (_candidate("fs", "user", _stdio("fs", "uvx")), _candidate("other", "user", _stdio("other", "npx"))),
            count=2,
        )
        (first_row, _second_row) = await _wait_for_rows(dialog, pilot, 2)
        card = dialog.query(_ImportCard)[0]

        # Ticking never unfolds the card.
        first_row.focus()
        await pilot.press("space")
        await wait_for(lambda: first_row.value is True, pilot=pilot, description="the tick lands")
        assert card.collapsed is True

        # The title line never ticks: it folds, and leaves the tick alone.
        title = card.query_one(".mcp-import-title")
        await click_when_settled(pilot, title)
        await wait_for(lambda: card.collapsed is False, pilot=pilot, description="the title unfolds the card")
        assert first_row.value is True

        # Tab walks the cards' two targets in order and never enters a body, unfolded or not.
        await wait_for(lambda: bool(card.query(".mcp-import-field-value")), pilot=pilot, description="body built")
        second_row = _row(dialog, 1)
        second_title = dialog.query(_ImportCard)[1].query_one(".mcp-import-title")
        first_row.focus()
        for expected, description in (
            (title, "Tab reaches the first title line"),
            (second_row, "Tab reaches the second tick"),
            (second_title, "Tab reaches the second title line"),
        ):
            await pilot.press("tab")
            await wait_for(lambda expected=expected: app.focused is expected, pilot=pilot, description=description)

        # Space on the title folds the card rather than ticking it.
        title.focus()
        await pilot.press("space")
        await wait_for(lambda: card.collapsed is True, pilot=pilot, description="Space folds the card")
        assert first_row.value is True


async def test_ticks_and_folds_come_back_after_switching_agents(tmp_path: Path) -> None:
    first = _ListedSource("first", "First Agent", (_candidate("a", "user", _stdio("a", "first")),))
    second = _ListedSource("second", "Second Agent", (_candidate("b", "user", _stdio("b", "second")),))
    app = _PanelApp(MCPConfigPanel([]))
    async with app.run_test(size=(120, 40)) as pilot:
        dialog = _open_with(app, [first, second], tmp_path)
        await wait_for(lambda: dialog.is_mounted, pilot=pilot, description="import dialog is open")
        sources = dialog.query_one("#mcp-import-sources", OptionList)
        (row,) = await _wait_for_rows(dialog, pilot, 1)
        row.value = True
        card = dialog.query_one(_ImportCard)
        card.set_collapsed(False)
        await wait_for(
            lambda: bool(card.query(".mcp-import-section")) and screen_is_settled(app, dialog),
            pilot=pilot,
            description="the first agent's card unfolds",
        )

        sources.highlighted = sources.get_option_index("second")
        await _wait_for_cards(dialog, pilot, ["b"], "second agent's servers are listed")
        sources.highlighted = sources.get_option_index("first")
        await _wait_for_cards(dialog, pilot, ["a"], "first agent's servers are listed again")

        # The tick and the fold the user chose both come back with the agent.
        assert _row(dialog, 0).value is True
        restored = dialog.query_one(_ImportCard)
        assert restored.collapsed is False
        await wait_for(
            lambda: bool(restored.query(".mcp-import-section")),
            pilot=pilot,
            description="the restored card builds its body again",
        )


async def test_reading_and_importing_servers_opens_no_connection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from chrys.service.mcp import _connection

    def refuse(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("the import must not connect to a server")

    monkeypatch.setattr(_connection, "_create_mcp_tool", refuse)
    monkeypatch.setattr(_connection, "_create_tool_with_timeout_floor", refuse)
    results: list[Any] = []
    source = _ListedSource(
        "first",
        "First Agent",
        (_candidate("a", "user", _stdio("a", "uvx")), _candidate("b", "user", _stdio("b", "uvx"))),
    )
    app = _PanelApp(MCPConfigPanel([]))
    async with app.run_test(size=(120, 40)) as pilot:
        dialog = _open_with(app, [source], tmp_path, results)
        await wait_for(lambda: dialog.is_mounted, pilot=pilot, description="import dialog is open")
        rows = await _wait_for_rows(dialog, pilot, 2)
        for card in dialog.query(_ImportCard):
            card.set_collapsed(False)
        await wait_for(
            lambda: bool(dialog.query(".mcp-import-field-value")),
            pilot=pilot,
            description="the cards show their settings",
        )

        rows[0].value = True
        await wait_for(lambda: not dialog.query_one("#mcp-import-confirm", Button).disabled, pilot=pilot)
        dialog.query_one("#mcp-import-confirm", Button).press()
        await wait_for(lambda: results, pilot=pilot, description="dialog dismissed with the ticked server")
        (imported,) = results
        assert [config.name for config in imported] == ["a"]
