# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""pi previews expose final tool settings and actionable compatibility limits."""

from __future__ import annotations

import json
import re
from pathlib import Path
from xml.etree import ElementTree

import pytest
from textual.app import App

from chrys.app.tui.i18n import LocaleController
from chrys.app.tui.screens.agents.import_mcp_dialog import ImportMcpServersDialog, _ImportCard
from chrys.app.tui.theme import CHRYS_THEME, TuiVariableDefaultsMixin
from chrys.app.tui.widgets import HatchedEmptyState
from chrys.foundation.config.settings import Settings
from chrys.service.mcp.importers import ImportContext
from chrys.service.mcp.importers.pi import PiSource
from tests.app.tui.screens._mcp_import_support import _card_text, _cards_in
from tests.support.pilot_barrier import screen_is_settled
from tests.support.waiting import wait_for


class _Preview(TuiVariableDefaultsMixin, App):
    def __init__(self, context: ImportContext, locale: str) -> None:
        super().__init__()
        self.locale_controller = LocaleController(Settings(locale=locale))
        self.register_theme(CHRYS_THEME)
        self.theme = CHRYS_THEME.name
        self.dialog = ImportMcpServersDialog(
            workspace_cwd=context.workspace_cwd,
            existing=[],
            locale_controller=self.locale_controller,
            sources=[PiSource()],
            context=context,
        )

    def on_mount(self) -> None:
        self.push_screen(self.dialog)


def _source(tmp_path: Path, raw: str | bytes) -> ImportContext:
    home = tmp_path / "home"
    config = home / ".pi" / "agent" / "mcp.json"
    config.parent.mkdir(parents=True)
    config.write_bytes(raw.encode("utf-8") if isinstance(raw, str) else raw)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    return ImportContext(str(workspace), home, {})


@pytest.mark.parametrize("locale", ["en", "zh-Hans"])
async def test_pi_preview_shows_final_loading_and_initial_tools(tmp_path: Path, locale: str) -> None:
    context = _source(
        tmp_path,
        json.dumps(
            {
                "mcpServers": {
                    "on-demand": {"command": "server", "toolExposure": {"inspect": "direct", "write_*": "hidden"}},
                    "full": {"command": "server-full", "exposure": "direct", "toolExposure": {"read": "hidden"}},
                    "none": {"command": "server-none", "exposure": "hidden", "toolExposure": {"write_*": "direct"}},
                }
            }
        ),
    )
    app = _Preview(context, locale)
    async with app.run_test(size=(120, 44)) as pilot:
        await wait_for(
            lambda: len(app.dialog.query(_ImportCard)) == 3 and screen_is_settled(app, app.dialog),
            pilot=pilot,
            description="pi loading previews have composed",
        )
        cards = {card.row.server_name: card for card in app.dialog.query(_ImportCard)}
        on_demand = await _card_text(cards["on-demand"], pilot)
        assert ("60 s" if locale == "en" else "60 秒") in on_demand
        assert ("On demand" if locale == "en" else "按需加载") in on_demand
        assert ("Initially Visible Tools" if locale == "en" else "初始可见工具") in on_demand
        assert "inspect" in on_demand and "write_*" in on_demand
        assert ("this server's settings" if locale == "en" else "该服务器设置") in on_demand
        full = await _card_text(cards["full"], pilot)
        assert ("Full — load all available tools" if locale == "en" else "全部加载") in full
        assert ("Initially Visible Tools" if locale == "en" else "初始可见工具") not in full
        none = await _card_text(cards["none"], pilot)
        assert ("No tools" if locale == "en" else "无工具") in none
        assert "write_*" in none
        assert ("User Guide (F8)" if locale == "en" else "用户指南（F8）") in none  # noqa: RUF001
        labels = [str(widget.render()) for widget in cards["none"].query(".mcp-import-field-label")]
        assert ("Loading Strategy" if locale == "en" else "加载策略") not in labels


@pytest.mark.parametrize("locale", ["en", "zh-Hans"])
async def test_pi_project_scope_and_credential_command_guidance_stay_in_the_card(tmp_path: Path, locale: str) -> None:
    context = _source(tmp_path, '{"mcpServers":{"global":{"command":"global-server"}}}')
    project = tmp_path / "workspace" / ".pi" / "mcp.json"
    project.parent.mkdir()
    project.write_text('{"mcpServers":{"project":{"url":"https://example.test/mcp","oauth":{}}}}', encoding="utf-8")
    app = _Preview(context, locale)
    async with app.run_test(size=(120, 44)) as pilot:
        await wait_for(
            lambda: len(app.dialog.query(_ImportCard)) == 2 and screen_is_settled(app, app.dialog),
            pilot=pilot,
            description="pi's global and project cards are listed",
        )
        assert not app.dialog.query(".mcp-import-notice")
        cards = {card.row.server_name: card for card in app.dialog.query(_ImportCard)}
        await _card_text(cards["global"], pilot)
        text = await _card_text(cards["project"], pilot)
        scope_label = "Scope After Import" if locale == "en" else "导入后的作用范围"
        values = {name: dict(fields) for name, fields, _tickable in _cards_in(app.dialog)}
        assert scope_label not in values["global"]
        assert values["project"][scope_label] == (
            "Available to this agent in every project" if locale == "en" else "当前智能体可在所有项目中使用此服务器"
        )
        assert "{{!command}}" in text
        assert ("User Guide (F8)" if locale == "en" else "用户指南（F8）") in text  # noqa: RUF001


@pytest.mark.parametrize("locale", ["en", "zh-Hans"])
async def test_pi_literal_timeout_and_cwd_repairs_are_visible(tmp_path: Path, locale: str) -> None:
    context = _source(
        tmp_path,
        json.dumps(
            {
                "mcpServers": {
                    "relative": {
                        "command": "server",
                        "cwd": "server-folder",
                        "env": {"LITERAL": "{{literal}}"},
                        "timeout": 1.501,
                    },
                    "home": {"command": "home-server", "cwd": "~/server-home"},
                    "unlimited": {"command": "unlimited-server", "timeout": 1e309},
                }
            }
        ).replace("Infinity", "1e309"),
    )
    app = _Preview(context, locale)
    async with app.run_test(size=(120, 44)) as pilot:
        await wait_for(
            lambda: len(app.dialog.query(_ImportCard)) == 3 and screen_is_settled(app, app.dialog),
            pilot=pilot,
            description="pi conversion previews have composed",
        )
        cards = {card.row.server_name: card for card in app.dialog.query(_ImportCard)}
        relative = await _card_text(cards["relative"], pilot)
        assert "server-folder" in relative and context.workspace_cwd in relative
        assert ("Braces look different in the preview" if locale == "en" else "预览中的花括号写法有所变化") in relative
        assert ("2 s (source: 1.501 s)" if locale == "en" else "2 秒（源配置：1.501 秒）") in relative  # noqa: RUF001
        assert str(context.home / "server-home") in await _card_text(cards["home"], pilot)
        unlimited = await _card_text(cards["unlimited"], pilot)
        assert ("source: no timeout limit" if locale == "en" else "源配置：无超时上限") in unlimited  # noqa: RUF001


@pytest.mark.parametrize("locale", ["en", "zh-Hans"])
async def test_pi_non_ascii_header_refusal_has_safe_actionable_field(tmp_path: Path, locale: str) -> None:
    context = _source(
        tmp_path,
        json.dumps(
            {
                "mcpServers": {
                    "encoding": {"url": "https://example.test/mcp", "headers": {"X-Test": "private-secret-café"}},
                    "command": {"command": "server", "env": {"TOKEN": "!private-secret-command"}},
                    "variable": {"url": "https://example.test/mcp", "headers": {"X-Test": "$HEADER_VALUE"}},
                }
            }
        ),
    )
    app = _Preview(context, locale)
    async with app.run_test(size=(120, 44)) as pilot:
        await wait_for(
            lambda: len(app.dialog.query(_ImportCard)) == 3 and screen_is_settled(app, app.dialog),
            pilot=pilot,
            description="pi unsupported previews have composed",
        )
        cards = {card.row.server_name: card for card in app.dialog.query(_ImportCard)}
        for name in ("encoding", "command"):
            cards[name].set_collapsed(False, scroll_visible=False)
            assert cards[name].row.disabled
        await pilot.pause()
        encoding = await _card_text(cards["encoding"], pilot)
        assert "mcpServers.encoding.headers.X-Test" in encoding
        assert ("contains non-ASCII text" if locale == "en" else "含有非 ASCII 文本") in encoding
        assert "mcpServers.command.env.TOKEN" in await _card_text(cards["command"], pilot)
        assert not cards["variable"].row.disabled
        diagnostics = "\n".join(
            str(widget.render()) for card in cards.values() for widget in card.query(".mcp-import-warning")
        )
        assert "private-secret-" not in diagnostics


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (b"\xff", "UTF-8"),
        ('{\n "mcpServers": {"private-secret-invalid": }\n}', "line 2, column"),
    ],
)
async def test_pi_source_errors_locate_safe_repairs(tmp_path: Path, raw: str | bytes, expected: str) -> None:
    app = _Preview(_source(tmp_path, raw), "en")
    async with app.run_test(size=(120, 42)) as pilot:
        await wait_for(
            lambda: bool(app.dialog.query(".mcp-import-error")),
            pilot=pilot,
            description="pi source error was rendered",
        )
        text = "\n".join(str(widget.render()) for widget in app.dialog.query(".mcp-import-error"))
        assert expected in text
        assert "private-secret-" not in text


@pytest.mark.parametrize("locale", ["en", "zh-Hans"])
async def test_pi_mcp_servers_container_error_names_required_object(tmp_path: Path, locale: str) -> None:
    app = _Preview(_source(tmp_path, '{"mcpServers":"private-secret-invalid"}'), locale)
    async with app.run_test(size=(120, 42)) as pilot:
        await wait_for(
            lambda: bool(app.dialog.query(".mcp-import-error")),
            pilot=pilot,
            description="pi container error was rendered",
        )
        text = "\n".join(str(widget.render()) for widget in app.dialog.query(".mcp-import-error"))
        assert "mcpServers" in text
        assert ("must be a JSON object" if locale == "en" else "必须为 JSON 对象") in text
        assert "private-secret-" not in text


@pytest.mark.parametrize("locale", ["en", "zh-Hans"])
@pytest.mark.parametrize("width", [80, 120])
async def test_empty_source_shows_only_empty_state_at_real_width(tmp_path: Path, locale: str, width: int) -> None:
    app = _Preview(_source(tmp_path, '{"mcpServers":{}}'), locale)
    async with app.run_test(size=(width, 42)) as pilot:
        await wait_for(
            lambda: bool(app.dialog.query(HatchedEmptyState)) and screen_is_settled(app, app.dialog),
            pilot=pilot,
            description="empty source layout has settled",
        )
        assert not app.dialog.query(".mcp-import-message")
        expected = "No MCP servers found." if locale == "en" else "未找到 MCP 服务器。"
        svg = ElementTree.fromstring(app.export_screenshot())
        screen_text = "".join("".join(node.itertext()) for node in svg.iter("{http://www.w3.org/2000/svg}text"))
        assert re.sub(r"\s+", "", expected) in re.sub(r"[\s\u2500-\u259f]+", "", screen_text)
