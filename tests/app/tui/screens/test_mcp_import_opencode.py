# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""opencode previews identify configuration sources and actionable repairs."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from textual.app import App

from chrys.app.tui.i18n import LocaleController
from chrys.app.tui.screens.agents.import_mcp_dialog import ImportMcpServersDialog, _ImportCard, _source_error
from chrys.app.tui.theme import CHRYS_THEME, TuiVariableDefaultsMixin
from chrys.foundation.config.settings import Settings
from chrys.foundation.i18n.formatting import format_message
from chrys.service.mcp.importers import ImportContext
from chrys.service.mcp.importers.base import McpSourceError, SourceErrorKind
from chrys.service.mcp.importers.opencode import OpenCodeSource
from tests.app.tui.screens._mcp_import_support import _card_text
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
            sources=[OpenCodeSource()],
            context=context,
        )

    def on_mount(self) -> None:
        self.push_screen(self.dialog)


def _source(tmp_path: Path, raw: str, *, inline: str = "") -> ImportContext:
    home = tmp_path / "home"
    config = home / ".config" / "opencode" / "opencode.jsonc"
    config.parent.mkdir(parents=True)
    config.write_text(raw, encoding="utf-8")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    return ImportContext(
        str(workspace),
        home,
        {
            "OPENCODE_DISABLE_PROJECT_CONFIG": "true",
            "OPENCODE_TEST_MANAGED_CONFIG_DIR": str(tmp_path / "managed"),
            "OPENCODE_CONFIG_CONTENT": inline,
        },
    )


@pytest.mark.parametrize("locale", ["en", "zh-Hans"])
@pytest.mark.parametrize("count", [1, 12])
async def test_opencode_warning_is_painted_before_any_card_interaction(tmp_path: Path, locale: str, count: int) -> None:
    context = _source(
        tmp_path,
        json.dumps(
            {
                "mcp": {
                    f"remote-{index}": {"type": "remote", "url": f"https://mcp.example.test/{index}", "oauth": {}}
                    for index in range(count)
                }
            }
        ),
    )
    app = _Preview(context, locale)
    async with app.run_test(size=(120, 42)) as pilot:
        await wait_for(
            lambda: len(app.dialog.query(_ImportCard)) == count and screen_is_settled(app, app.dialog),
            pilot=pilot,
            description="the initial opencode card is painted",
        )
        card = app.dialog.query_one(_ImportCard)
        assert card.collapsed
        title = card.query_one(".mcp-import-title")
        screen_line = app.screen._compositor.render_strips()[title.region.y].text
        assert "⚠" in screen_line, screen_line
        assert not card.query(".mcp-import-warning-group")


@pytest.mark.parametrize("locale", ["en", "zh-Hans"])
async def test_opencode_merged_sources_cwd_literal_and_timeout_are_visible(tmp_path: Path, locale: str) -> None:
    context = _source(
        tmp_path,
        json.dumps(
            {
                "mcp": {
                    "merged": {"type": "local", "command": ["old"], "environment": {"INHERITED": "value"}},
                    "preview": {
                        "type": "local",
                        "command": ["preview-server"],
                        "cwd": "server-folder",
                        "environment": {"LITERAL": "{{literal}}"},
                        "timeout": 1501,
                    },
                    "exact": {"type": "local", "command": ["exact-server"], "timeout": 1000},
                }
            }
        ),
        inline=json.dumps({"mcp": {"merged": {"type": "local", "command": ["merged-server"]}}}),
    )
    app = _Preview(context, locale)
    async with app.run_test(size=(120, 42)) as pilot:
        await wait_for(
            lambda: len(app.dialog.query(_ImportCard)) == 3 and screen_is_settled(app, app.dialog),
            pilot=pilot,
            description="opencode previews have composed",
        )
        cards = {card.row.server_name: card for card in app.dialog.query(_ImportCard)}
        merged = await _card_text(cards["merged"], pilot)
        assert ("Configuration Source" if locale == "en" else "配置来源") in merged
        assert "OPENCODE_CONFIG_CONTENT" in merged
        assert str(context.home / ".config" / "opencode" / "opencode.jsonc") in merged
        assert "INHERITED" in merged
        assert ("60 s" if locale == "en" else "60 秒") in merged
        preview = await _card_text(cards["preview"], pilot)
        assert "server-folder" in preview
        assert context.workspace_cwd in preview
        assert ("Braces look different in the preview" if locale == "en" else "预览中的花括号写法有所变化") in preview
        assert ("2 s (source: 1501 ms)" if locale == "en" else "2 秒（源配置：1501 毫秒）") in preview  # noqa: RUF001
        assert ("1 s" if locale == "en" else "1 秒") in await _card_text(cards["exact"], pilot)


@pytest.mark.parametrize("locale", ["en", "zh-Hans"])
async def test_opencode_unsupported_settings_offer_repairs_without_secret_values(tmp_path: Path, locale: str) -> None:
    context = _source(
        tmp_path,
        json.dumps(
            {
                "mcp": {
                    "command": {"type": "local", "command": ["{env:PROGRAM}"]},
                    "url": {"type": "remote", "url": "{env:MCP_URL}"},
                    "file": {
                        "type": "local",
                        "command": ["server"],
                        "environment": {"TOKEN": "{file:secret-file-path}"},
                    },
                    "variable": {
                        "type": "local",
                        "command": ["server"],
                        "environment": {"TOKEN": "prefix-secret-{env:BAD-NAME}"},
                    },
                }
            }
        ),
    )
    app = _Preview(context, locale)
    async with app.run_test(size=(120, 42)) as pilot:
        await wait_for(
            lambda: len(app.dialog.query(_ImportCard)) == 4 and screen_is_settled(app, app.dialog),
            pilot=pilot,
            description="opencode unsupported previews have composed",
        )
        cards = {card.row.server_name: card for card in app.dialog.query(_ImportCard)}
        for card in cards.values():
            card.set_collapsed(False, scroll_visible=False)
            assert card.row.disabled
        await pilot.pause()
        command = await _card_text(cards["command"], pilot)
        assert "command" in command
        assert ("replace each reference" if locale == "en" else "将变量引用替换为实际需要的值") in command
        url = str(cards["url"].query_one(".mcp-import-hint").render())
        assert url == (
            "iCode doesn't support environment variable references in URLs. "
            "Add the server manually and enter its full address in URL.\nSource setting: mcp.url.url"
            if locale == "en"
            else "iCode 不支持在 URL 中引用环境变量。请手动添加服务器，在“URL”中填写完整地址。\n"  # noqa: RUF001
            "源配置项：mcp.url.url"  # noqa: RUF001
        )
        file = await _card_text(cards["file"], pilot)
        assert "environment.TOKEN" in file
        assert ("Add the server manually" if locale == "en" else "手动添加服务器") in file
        assert "BAD-NAME" in await _card_text(cards["variable"], pilot)
        diagnostics = "\n".join(
            str(widget.render()) for card in cards.values() for widget in card.query(".mcp-import-warning")
        )
        assert "secret-" not in diagnostics


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ('{"mcp":{"broken":{"type":"local","command":"secret-invalid"}}}', "mcp.broken"),
        ('{\n // preserved location\n "mcp": { "secret-invalid": }\n}', "line 3, column"),
    ],
)
async def test_opencode_source_errors_locate_repairs_without_source_values(
    tmp_path: Path, raw: str, expected: str
) -> None:
    app = _Preview(_source(tmp_path, raw), "en")
    async with app.run_test(size=(120, 42)) as pilot:
        await wait_for(
            lambda: bool(app.dialog.query(".mcp-import-error")),
            pilot=pilot,
            description="opencode source error was rendered",
        )
        text = "\n".join(str(widget.render()) for widget in app.dialog.query(".mcp-import-error"))
        assert expected in text
        assert "reopen import" not in text
        assert "secret-" not in text


def test_opencode_source_error_without_location_explains_the_failure() -> None:
    text = format_message(
        _source_error(McpSourceError("/config/opencode.json", SourceErrorKind.INVALID_CONFIG), "opencode")
    )
    assert "/config/opencode.json" in text
    assert "has settings opencode rejects" in text
    assert "reopen import" not in text


@pytest.mark.parametrize("locale", ["en", "zh-Hans"])
async def test_opencode_invalid_mcp_timeout_offers_setting_repair(tmp_path: Path, locale: str) -> None:
    app = _Preview(_source(tmp_path, '{"mcp":{"timeout":{"catalog":0,"execution":0}}}'), locale)
    async with app.run_test(size=(120, 42)) as pilot:
        await wait_for(
            lambda: bool(app.dialog.query(".mcp-import-error")),
            pilot=pilot,
            description="opencode timeout error was rendered",
        )
        text = "\n".join(str(widget.render()) for widget in app.dialog.query(".mcp-import-error"))
        assert "mcp.timeout" in text
        assert ("must be a positive integer" if locale == "en" else "必须为正整数") in text
        assert "local" not in text and "remote" not in text


@pytest.mark.parametrize("locale", ["en", "zh-Hans"])
@pytest.mark.parametrize(
    ("raw", "field", "english", "chinese"),
    [
        ('{"mcp":{"s":{"type":"local","command":"secret-value"}}}', "mcp.s.command", "array of strings", "字符串数组"),
        ('{"mcp":{"s":{"type":"remote","url":1}}}', "mcp.s.url", "must be a string", "必须为字符串"),
        (
            '{"mcp":{"s":{"type":"remote","url":"https://x.test","headers":{"X-Test":1}}}}',
            "mcp.s.headers",
            "values are strings",
            "值均为字符串",
        ),
    ],
)
async def test_opencode_invalid_fields_show_expected_values_without_rejected_content(
    tmp_path: Path, locale: str, raw: str, field: str, english: str, chinese: str
) -> None:
    app = _Preview(_source(tmp_path, raw), locale)
    async with app.run_test(size=(120, 42)) as pilot:
        await wait_for(
            lambda: bool(app.dialog.query(".mcp-import-error")),
            pilot=pilot,
            description="the invalid setting's requirement is shown",
        )
        text = "\n".join(str(widget.render()) for widget in app.dialog.query(".mcp-import-error"))
        assert field in text and (english if locale == "en" else chinese) in text
        assert "secret-value" not in text


@pytest.mark.parametrize("locale", ["en", "zh-Hans"])
async def test_opencode_dynamic_names_offer_fixed_name_repairs_without_resolving_secrets(
    tmp_path: Path, locale: str
) -> None:
    name_file = tmp_path / "header-name.txt"
    name_file.write_text("private-secret-header-name", encoding="utf-8")
    context = _source(
        tmp_path,
        json.dumps(
            {
                "mcp": {
                    "{env:SERVER_NAME}": {"type": "local", "command": ["server"]},
                    "dynamic-env": {
                        "type": "local",
                        "command": ["server"],
                        "environment": {"{env:ENV_NAME}": "value"},
                    },
                    "dynamic-header": {
                        "type": "remote",
                        "url": "https://example.test/mcp",
                        "headers": {f"{{file:{name_file}}}": "value"},
                    },
                    "dynamic-command": {"type": "local", "{env:COMMAND_FIELD}": ["server"]},
                    "dynamic-type": {"{env:TYPE_FIELD}": "local", "command": ["server"]},
                }
            }
        ),
    )
    context = ImportContext(
        context.workspace_cwd,
        context.home,
        {
            **context.environ,
            "SERVER_NAME": "private-secret-server-name",
            "ENV_NAME": "private-secret-env-name",
            "COMMAND_FIELD": "command",
            "TYPE_FIELD": "type",
        },
    )
    app = _Preview(context, locale)
    async with app.run_test(size=(120, 42)) as pilot:
        await wait_for(
            lambda: len(app.dialog.query(_ImportCard)) == 5 and screen_is_settled(app, app.dialog),
            pilot=pilot,
            description="opencode dynamic name previews have composed",
        )
        for card in app.dialog.query(_ImportCard):
            card.set_collapsed(False, scroll_visible=False)
            assert card.row.disabled
        await pilot.pause()
        for card in app.dialog.query(_ImportCard):
            text = await _card_text(card, pilot)
            assert ("configuration field names" if locale == "en" else "配置字段名") in text
            assert ("requires fixed server names" if locale == "en" else "要求服务器名称") in text
            assert ("Source setting:" if locale == "en" else "源配置项") in text
            assert "private-secret-" not in text
        assert app.dialog._selected() == []
