# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""What the import cards paint: the tick's own block, the summary's colours, and where warnings go."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
from textual.color import Color
from textual.geometry import Region

from chrys.app.tui.i18n import LocaleController
from chrys.app.tui.screens.agents.import_mcp_dialog import ImportMcpServersDialog, _ImportCard
from chrys.app.tui.screens.agents.panels.mcp import MCPConfigPanel
from chrys.foundation.config.settings import Settings
from chrys.service.mcp.importers import ImportWarning, ImportWarningKind, McpImportCandidate, UnsupportedReason
from chrys.service.profiles.agents.schema import MCPServerConfig
from tests.app.tui.screens._mcp_import_support import (
    _candidate,
    _hints,
    _import_ticked,
    _ListedSource,
    _open_with,
    _PanelApp,
    _stdio,
    _titles,
    _wait_for_rows,
    _warning_texts,
)
from tests.support.pilot_barrier import screen_is_settled
from tests.support.tui_helpers import click_when_settled, resize_when_settled
from tests.support.waiting import wait_for

pytestmark = [pytest.mark.timeout(120), pytest.mark.usefixtures("isolated_chrys_config_dir")]


def _first_line(widget: Any) -> list[tuple[str, str]]:
    """Each painted cell of *widget*'s first line: its text and its resolved style."""
    row = widget.render_lines(Region(0, 0, widget.region.width, 1))
    return [(cell.text, str(cell.style)) for segment in row for cell in segment]


def _colour(style: str) -> str:
    """The colour a cell's text paints in, without its text style or the background behind it."""
    return style.split(" on ", 1)[0].split()[-1]


def _warned(name: str, config: MCPServerConfig, warning: ImportWarning) -> McpImportCandidate:
    """*name* as a candidate that converts, with *warning* about what the conversion changes."""
    return replace(_candidate(name, "user", config), warnings=(warning,))


async def _open(app: _PanelApp, pilot, tmp_path: Path, candidates: tuple[Any, ...]) -> Any:
    dialog = _open_with(app, [_ListedSource("first", "First Agent", candidates)], tmp_path)
    await wait_for(lambda: dialog.is_mounted, pilot=pilot, description="import dialog is open")
    await _wait_for_rows(dialog, pilot, len(candidates))
    return dialog


async def test_the_tick_paints_no_block_of_its_own(tmp_path: Path) -> None:
    """The tick carries no label, so nothing next to it may paint — not even while hovered or focused."""
    app = _PanelApp(MCPConfigPanel([]))
    async with app.run_test(size=(120, 40)) as pilot:
        dialog = await _open(app, pilot, tmp_path, (_candidate("fs", "user", _stdio("fs", "uvx")),))
        (row,) = dialog.query(".mcp-import-row").results()
        await wait_for(lambda: screen_is_settled(app, dialog), pilot=pilot, description="the pane settles")
        card = dialog.query_one(_ImportCard)
        assert row.region.width == 3
        assert card.region.x - row.region.right == 1
        assert row.styles.background.a == 0
        assert row.styles.background_tint.a == 0
        assert row.get_visual_style("toggle--button").background == row.visual_style.background

        async def hovered() -> None:
            assert await pilot.hover(row)
            await wait_for(lambda: "hover" in row.pseudo_classes, pilot=pilot, description="the tick is hovered")

        async def focused() -> None:
            row.focus()
            await wait_for(lambda: "focus" in row.pseudo_classes, pilot=pilot, description="the tick is focused")

        for state, enter in (("hovered", hovered), ("focused", focused)):
            await enter()
            # Textual pads an empty label into a block with a background of its own; only the tick may show.
            marker = "[*]" if row.value else "[ ]"
            content = row.render()
            assert content.plain == marker, state
            assert len(content.spans) == 1, state
            assert [text for text, _style in _first_line(row) if text.strip()] == [marker], state

        # The same with the tick on.
        row.value = True
        await wait_for(lambda: row.render().plain == "[*]", pilot=pilot, description="the tick shows")
        assert len(row.render().spans) == 1
        assert [text for text, _style in _first_line(row) if text.strip()] == ["[*]"]
        assert row.get_visual_style("toggle--button").background == row.visual_style.background
        assert row.get_visual_style("toggle--button").foreground == Color.parse(app.get_css_variables()["success"])
        assert row.region.width == 3
        assert row.styles.background.a > 0

        row.blur()
        await wait_for(lambda: not row.has_focus, pilot=pilot, description="the tick loses focus")
        assert row.styles.background.a == 0


async def test_the_summary_keeps_its_contrast_on_hover_and_focus(tmp_path: Path) -> None:
    """The muted summary follows the title line's own colours instead of staying a fixed gray."""
    app = _PanelApp(MCPConfigPanel([]))
    async with app.run_test(size=(120, 40)) as pilot:
        dialog = await _open(app, pilot, tmp_path, (_candidate("fs", "user", _stdio("fs", "uvx")),))
        title = dialog.query_one(_ImportCard).query_one(".mcp-import-title")

        def summary_style() -> str:
            return next(style for text, style in _first_line(title) if "STDIO" in text)

        def name_style() -> str:
            return next(style for text, style in _first_line(title) if text == "fs")

        at_rest = summary_style()
        # At rest the summary is still the quieter half of the line.
        assert "not bold" in at_rest
        assert _colour(at_rest) != _colour(name_style())

        title.focus()
        await wait_for(
            lambda: _colour(summary_style()) == _colour(name_style()),
            pilot=pilot,
            description="the focused line gives the summary its own colour",
        )
        assert "not bold" in summary_style()

        title.blur()
        await wait_for(lambda: summary_style() == at_rest, pilot=pilot, description="the muted summary returns")
        await pilot.hover(title)
        await wait_for(
            lambda: _colour(summary_style()) == _colour(name_style()),
            pilot=pilot,
            description="the hovered line gives the summary its own colour",
        )


async def test_an_unfolded_card_drops_its_hint_and_puts_the_warning_first(tmp_path: Path) -> None:
    """The hint says what the body's warning says, so only the warning shows once the body does."""
    warning = ImportWarning(ImportWarningKind.TOOL_PERMISSIONS_IGNORED, field="tools")
    app = _PanelApp(MCPConfigPanel([]))
    async with app.run_test(size=(120, 40)) as pilot:
        dialog = await _open(app, pilot, tmp_path, (_warned("lab", _stdio("lab", "python"), warning),))
        card = dialog.query_one(_ImportCard)
        hint = card.query_one(".mcp-import-hint")
        # Folded, one short line stands in for the warning the unbuilt body would spell out.
        assert _hints(dialog) == []
        assert _titles(dialog)[0].endswith("⚠")

        await click_when_settled(pilot, card.query_one(".mcp-import-title"))
        await wait_for(
            lambda: bool(card.query(".mcp-import-warning-group")) and screen_is_settled(app, dialog),
            pilot=pilot,
            description="the body is built",
        )
        # Unfolded, the short line stands down and its warning comes first, above the settings.
        assert hint.display is False
        assert _hints(dialog) == []
        (text,) = _warning_texts(card)
        assert "Tool permissions from First Agent are not imported" in text
        first = card.query_one(".mcp-import-notes.-attention")
        assert first.styles.border_left[0] == "solid"
        assert first.styles.border_left[1] == Color.parse(app.get_css_variables()["tui-border-warning"])
        heading = first.query_one(".mcp-import-notes-title")
        assert str(heading.render()) == "⚠ Import Warnings"
        assert heading.styles.color == Color.parse(app.get_css_variables()["warning"])
        assert card.query_one(".mcp-import-title").tooltip is None
        assert first.region.y < card.query_one(".mcp-import-field-value").region.y

        # Folded again, the short line comes back; the body is kept, not rebuilt.
        await click_when_settled(pilot, card.query_one(".mcp-import-status"))
        await wait_for(lambda: card.collapsed is True, pilot=pilot, description="the card folds")
        assert hint.display is False
        assert _hints(dialog) == []
        assert _titles(dialog)[0].endswith("⚠")


async def test_a_lock_stays_under_the_title_when_unfolded(tmp_path: Path) -> None:
    """A lock has no trace in the body, so its note outlives the fold that would hide it."""
    github = _stdio("github", "gh")
    app = _PanelApp(MCPConfigPanel([]))
    async with app.run_test(size=(120, 40)) as pilot:
        dialog = await _open(
            app, pilot, tmp_path, (_candidate("github", "user", github), _candidate("github", "project", github))
        )
        user, project = dialog.query(".mcp-import-row").results()
        user.value = True
        await wait_for(lambda: project.disabled, pilot=pilot, description="the same server is locked")
        covered = "Same configuration as the selected github (First Agent, user). It will be imported once."
        assert _hints(dialog) == []
        assert _titles(dialog)[1].endswith("Duplicate")

        locked = dialog.query(_ImportCard)[1]
        locked.set_collapsed(False)
        await wait_for(
            lambda: bool(locked.query(".mcp-import-section")) and screen_is_settled(app, dialog),
            pilot=pilot,
            description="the locked card unfolds",
        )
        assert _hints(dialog) == [covered]
        assert not locked.query(".mcp-import-warning-group")
        assert locked.query_one(".mcp-import-notes").has_class("-error") is False


@pytest.mark.parametrize("locale", ["en", "zh-Hans"])
async def test_imported_descriptions_keep_notes_separate_from_warnings(tmp_path: Path, locale: str) -> None:
    literal = ImportWarning(ImportWarningKind.LITERAL_BRACES_ESCAPED)
    candidate = replace(
        _candidate("notes", "user", _stdio("notes")),
        warnings=(
            literal,
            literal,
            ImportWarning(ImportWarningKind.MERGED, original="/additional.json"),
            ImportWarning(ImportWarningKind.UNKNOWN_FIELDS_IGNORED, original="future"),
        ),
    )
    app = _PanelApp(MCPConfigPanel([]))
    app.locale_controller = LocaleController(Settings(locale=locale))
    results: list[list[MCPServerConfig] | None] = []
    async with app.run_test(size=(120, 40)) as pilot:
        dialog = _open_with(app, [_ListedSource("first", "First Agent", (candidate,))], tmp_path, results)
        (row,) = await _wait_for_rows(dialog, pilot, 1)
        row.value = True
        await _import_ticked(dialog, pilot)
        await wait_for(lambda: bool(results), pilot=pilot, description="import returned the selected configuration")
        (configs,) = results
        assert configs is not None
        (config,) = configs
        lines = config.description.splitlines()
        assert len([line for line in lines if line.startswith("⚠ ")]) == 1
        note_prefix = "Note: " if locale == "en" else "补充： "  # noqa: RUF001
        assert len([line for line in lines if line.startswith(note_prefix)]) == 2
        assert config.description.count("/additional.json") == 1
        assert "future" in lines[-1]


async def test_a_refusal_stays_under_the_title_when_unfolded(tmp_path: Path) -> None:
    """A server that can't be imported has its reason only under the title; its body shows what it wrote."""
    sse = McpImportCandidate(
        "legacy",
        "user",
        "/user",
        None,
        UnsupportedReason.UNSUPPORTED_TRANSPORT,
        entry={"type": "sse", "url": "https://legacy.example.test/sse"},
        transport="sse",
    )
    app = _PanelApp(MCPConfigPanel([]))
    async with app.run_test(size=(120, 40)) as pilot:
        dialog = await _open(app, pilot, tmp_path, (sse,))
        card = dialog.query_one(_ImportCard)
        assert _hints(dialog) == []
        assert _titles(dialog)[0].endswith("Can't import")

        card.set_collapsed(False)
        await wait_for(
            lambda: bool(card.query(".mcp-import-section")) and screen_is_settled(app, dialog),
            pilot=pilot,
            description="the refused card unfolds",
        )
        # The body shows the source's own connection settings; the reason under the title is the only one said.
        (reason,) = _hints(dialog)
        assert reason.startswith("iCode supports only STDIO and HTTP")
        assert "Can't import:" not in reason
        assert not card.query(".mcp-import-warning-group")
        assert not card.query(".mcp-import-full-name")
        panel = card.query_one(".mcp-import-notes")
        assert panel.has_class("-error")
        assert panel.styles.background.a > 0
        assert panel.region.y < card.query_one(".mcp-import-field-value").region.y


@pytest.mark.parametrize("labels", [("Can't import", "Duplicate", "Imported"), ("无法导入", "重复项", "已导入")])
async def test_the_refusal_status_is_painted_in_full_after_unfolding(tmp_path: Path, labels: tuple[str, ...]) -> None:
    candidate = McpImportCandidate(
        "lab-cc-sse",
        "user",
        "/user",
        None,
        UnsupportedReason.UNSUPPORTED_TRANSPORT,
        entry={"type": "sse", "url": "http://127.0.0.1/sse"},
        transport="sse",
    )
    app = _PanelApp(MCPConfigPanel([]))
    async with app.run_test(size=(120, 40)) as pilot:
        dialog = await _open(app, pilot, tmp_path, (candidate,))
        card = dialog.query_one(_ImportCard)
        card._status_labels = ("⚠", *labels)
        card._sync_hint()
        for width in (120, 90, 60):
            await resize_when_settled(pilot, width, 40)
            title = card.query_one(".mcp-import-title")
            status = title.query_one(".mcp-import-status")
            assert "".join(text for text, _style in _first_line(status)).strip() == labels[0]
            for collapsed in (True, False, True, False):
                card.set_collapsed(collapsed)
                await wait_for(lambda: screen_is_settled(app, dialog), pilot=pilot, description="the card settles")
                title = card.query_one(".mcp-import-title")
                painted = "".join(text for text, _style in _first_line(status)).strip()
                assert painted == labels[0], painted
                screen_line = app.screen._compositor.render_strips()[title.region.y].text
                assert labels[0] in screen_line

        def status_colour() -> str:
            return next(_colour(style) for text, style in _first_line(status) if labels[0] in text)

        def name_colour() -> str:
            return next(_colour(style) for text, style in _first_line(title) if text.strip())

        title.focus()
        await wait_for(
            lambda: title.has_focus and status_colour() == name_colour(),
            pilot=pilot,
            description="the focused status follows the title foreground",
        )
        title.blur()
        await pilot.hover(title)
        await wait_for(
            lambda: "hover" in title.pseudo_classes and status_colour() == name_colour(),
            pilot=pilot,
            description="the hovered status follows the title foreground",
        )


async def test_header_and_environment_names_paint_in_the_same_colour_as_values(tmp_path: Path) -> None:
    app = _PanelApp(MCPConfigPanel([]))
    configs = (
        MCPServerConfig(name="env", transport="stdio", command="python", env={"LAB_NAME": "lab-value"}),
        MCPServerConfig(name="http", transport="http", url="https://mcp.test", headers={"X-Lab": "lab-value"}),
    )
    async with app.run_test(size=(120, 50)) as pilot:
        dialog = await _open(app, pilot, tmp_path, tuple(_candidate(cfg.name, "user", cfg) for cfg in configs))
        for card in dialog.query(_ImportCard):
            card.set_collapsed(False, scroll_visible=False)
        await wait_for(
            lambda: len(dialog.query(".mcp-import-pair-key")) == 2 and screen_is_settled(app, dialog),
            pilot=pilot,
            description="the header and environment rows are painted",
        )
        for grid in dialog.query(".mcp-import-pairs"):
            key = grid.query_one(".mcp-import-pair-key")
            value = grid.query_one(".mcp-import-pair-value")
            key_colour = next(_colour(style) for text, style in _first_line(key) if text.strip())
            value_colour = next(_colour(style) for text, style in _first_line(value) if text.strip())
            assert key_colour == value_colour


# The folded line names one change, so each band below must outrank every band after it: a
# server that has both must show the worse one. A new kind goes into a band here and into
# `_HINT_ORDER` together.
_HINT_BANDS = (
    # It cannot start or cannot authenticate until you fix this.
    (
        ImportWarningKind.OPTIONAL_VARIABLE,
        ImportWarningKind.CLAUDE_PROJECT_DIR,
        ImportWarningKind.CREDENTIAL_VARIABLE,
        ImportWarningKind.AUTH_PROVIDER_IGNORED,
        ImportWarningKind.OAUTH_IGNORED,
        ImportWarningKind.HEADERS_HELPER_IGNORED,
    ),
    # It starts, but offers fewer tools than the source gives it, or none at all.
    (ImportWarningKind.DENY_LIST, ImportWarningKind.TOOLS_LIMITED),
    # It starts and works, but not as the source application had it run.
    (
        ImportWarningKind.DEFAULT_DROPPED,
        ImportWarningKind.CWD_IGNORED,
        ImportWarningKind.UNKNOWN_FIELDS_IGNORED,
        ImportWarningKind.TOOL_RULES_IGNORED,
        ImportWarningKind.TOOL_PERMISSIONS_IGNORED,
        ImportWarningKind.TOOL_OUTPUT_LIMIT_IGNORED,
        ImportWarningKind.ZERO_TIMEOUT_IGNORED,
        ImportWarningKind.STARTUP_TIMEOUT_IGNORED,
        ImportWarningKind.INVALID_HEADER_IGNORED,
    ),
    # Nothing it receives or does changes; the line only records a difference.
    (ImportWarningKind.LITERAL_BRACES_ESCAPED, ImportWarningKind.MERGED),
)


def test_every_warning_kind_has_a_rank() -> None:
    """A new warning kind must be placed in a band, or its severity would be left unstated."""
    ranked = [kind for band in _HINT_BANDS for kind in band]

    assert sorted(ranked, key=lambda kind: kind.value) == sorted(ImportWarningKind, key=lambda kind: kind.value)


@pytest.mark.parametrize(
    ("worse", "better"),
    [
        (worse, better)
        for low, _band in enumerate(_HINT_BANDS)
        for better_band in _HINT_BANDS[low + 1 :]
        for worse in _band
        for better in better_band
    ],
    ids=lambda kind: kind.value,
)
def test_the_folded_line_names_the_worse_of_two_changes(worse: ImportWarningKind, better: ImportWarningKind) -> None:
    """A worse band outranks every better one, whichever order the server's warnings arrive in."""
    dialog = ImportMcpServersDialog(workspace_cwd=None, existing=[], locale_controller=None, sources=[])
    candidate = _candidate("both", "user", _stdio("both", "python"))

    def hint(*kinds: ImportWarningKind) -> str | None:
        return dialog._hint(replace(candidate, warnings=tuple(ImportWarning(kind) for kind in kinds)))

    worst = hint(worse)
    assert worst is not None
    assert hint(better, worse) == hint(worse, better) == worst
    assert worst != hint(better)
