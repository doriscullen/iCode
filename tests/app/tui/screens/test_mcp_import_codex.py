# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Codex import previews explain the actual imported settings and repairs."""

from __future__ import annotations

from pathlib import Path

import pytest
from textual.app import App
from textual.widgets import Button

from chrys.app.tui.i18n import LocaleController
from chrys.app.tui.screens.agents.import_mcp_dialog import ImportMcpServersDialog, _ImportCard
from chrys.app.tui.theme import CHRYS_THEME, TuiVariableDefaultsMixin
from chrys.foundation.config.settings import Settings
from chrys.service.mcp.importers import ImportContext
from chrys.service.mcp.importers.codex import CodexSource
from chrys.service.profiles.agents.loader import load_profile_from_yaml
from chrys.service.profiles.agents.schema import AgentProfile, MCPServerConfig, ToolsConfig
from chrys.service.profiles.agents.serializer import save_profile
from tests.app.tui.screens._mcp_import_support import _card_text, _cards_in
from tests.support.pilot_barrier import screen_is_settled
from tests.support.tui_helpers import click_when_settled
from tests.support.waiting import wait_for


class _Preview(TuiVariableDefaultsMixin, App):
    def __init__(self, context: ImportContext, locale: str) -> None:
        super().__init__()
        self.locale_controller = LocaleController(Settings(locale=locale))
        self.register_theme(CHRYS_THEME)
        self.theme = CHRYS_THEME.name
        self.result: list[MCPServerConfig] | None = None
        self.dialog = ImportMcpServersDialog(
            workspace_cwd=context.workspace_cwd,
            existing=[],
            locale_controller=self.locale_controller,
            sources=[CodexSource()],
            context=context,
        )

    def on_mount(self) -> None:
        self.push_screen(self.dialog, self.imported)

    def imported(self, result: list[MCPServerConfig] | None) -> None:
        self.result = result


def _source(tmp_path: Path, raw: str | bytes) -> ImportContext:
    codex = tmp_path / "codex"
    codex.mkdir()
    path = codex / "config.toml"
    path.write_bytes(raw.encode("utf-8") if isinstance(raw, str) else raw)
    return ImportContext(None, tmp_path / "home", {"CODEX_HOME": str(codex)})


@pytest.mark.parametrize("locale", ["en", "zh-Hans"])
async def test_omitted_auth_can_be_reviewed_imported_and_saved_without_its_secrets(tmp_path: Path, locale: str) -> None:
    context = _source(
        tmp_path,
        '[mcp_servers.remote]\nurl = "https://example.test/mcp"\n'
        'auth = { secret = "omitted-auth-secret" }\nstartup_readiness = "future-mode"\n'
        'http_headers_helper = { command = "omitted-helper-secret" }\n'
        'oauth = {callback_port = "future-port", client_secret = "omitted-oauth-secret"}\n',
    )
    app = _Preview(context, locale)
    async with app.run_test(size=(120, 42)) as pilot:
        await wait_for(
            lambda: len(app.dialog.query(_ImportCard)) == 1 and screen_is_settled(app, app.dialog),
            pilot=pilot,
            description="the server with omitted authentication is listed",
        )
        card = app.dialog.query_one(_ImportCard)
        assert card.collapsed
        assert not card.row.disabled
        text = await _card_text(card, pilot)
        assert "auth" in text and "startup_readiness" in text
        assert ("not imported" if locale == "en" else "不会导入") in text
        assert "omitted-" not in text
        await click_when_settled(pilot, card.row)
        await click_when_settled(pilot, app.dialog.query_one("#mcp-import-confirm", Button))
        await wait_for(lambda: app.result is not None, pilot=pilot, description="the reviewed server is imported")
        assert app.result is not None
        (config,) = app.result
        assert config.url == "https://example.test/mcp"
        assert "auth" in config.description and "omitted-" not in config.description
        profile = AgentProfile(name="Imported", tools=ToolsConfig(mcp=[config]))
        path = save_profile(profile, target_dir=tmp_path / "profiles")
        assert "omitted-" not in path.read_text(encoding="utf-8")
        restored = load_profile_from_yaml(path)
        assert restored.tools.mcp == [config]


@pytest.mark.parametrize("locale", ["en", "zh-Hans"])
async def test_codex_tool_scope_and_timeout_preview_survive_import_and_save(tmp_path: Path, locale: str) -> None:
    context = _source(
        tmp_path,
        '[mcp_servers.all]\ncommand = "all-server"\n'
        '[mcp_servers.selected]\ncommand = "selected-server"\n'
        'enabled_tools = ["inspect", "second"]\ndisabled_tools = ["second"]\ntool_timeout_sec = 1.501\n'
        '[mcp_servers.none]\ncommand = "no-tools-server"\nenabled_tools = []\n'
        '[mcp_servers.zero]\ncommand = "zero-server"\ntool_timeout_sec = 0\n',
    )
    app = _Preview(context, locale)
    async with app.run_test(size=(120, 42)) as pilot:
        await wait_for(
            lambda: len(app.dialog.query(_ImportCard)) == 4 and screen_is_settled(app, app.dialog),
            pilot=pilot,
            description="Codex previews have composed",
        )
        cards = {card.row.server_name: card for card in app.dialog.query(_ImportCard)}
        assert ("All server tools" if locale == "en" else "所有服务器工具") in await _card_text(cards["all"], pilot)
        assert ("Only these tools: inspect" if locale == "en" else "仅以下工具：inspect") in await _card_text(  # noqa: RUF001
            cards["selected"], pilot
        )
        assert ("No tools" if locale == "en" else "无工具") in await _card_text(cards["none"], pilot)
        assert ("2 s (source: 1.501 s)" if locale == "en" else "2 秒（源配置：1.501 秒）") in await _card_text(  # noqa: RUF001
            cards["selected"], pilot
        )
        assert ("Use iCode default" if locale == "en" else "使用 iCode 默认值") in await _card_text(
            cards["zero"], pilot
        )
        for card in cards.values():
            card.row.value = True
        await wait_for(
            lambda: not app.dialog.query_one("#mcp-import-confirm", Button).disabled,
            pilot=pilot,
            description="selected Codex servers can be imported",
        )
        app.dialog.query_one("#mcp-import-confirm", Button).press()
        await wait_for(lambda: app.result is not None, pilot=pilot, description="Codex settings were imported")
        configs = app.result
        assert configs is not None
        profile_path = save_profile(
            AgentProfile(name="Codex Import", tools=ToolsConfig(mcp=configs)), target_dir=tmp_path / "profiles"
        )
        loaded = {server.name: server for server in load_profile_from_yaml(profile_path).tools.mcp}
        assert loaded["all"].allowed_tools is None
        assert loaded["selected"].allowed_tools == ["inspect"]
        assert loaded["none"].allowed_tools == []
        assert loaded["selected"].request_timeout == 2
        assert loaded["zero"].request_timeout is None


@pytest.mark.parametrize("locale", ["en", "zh-Hans"])
async def test_codex_repair_notices_name_fields_without_secret_values(tmp_path: Path, locale: str) -> None:
    context = _source(
        tmp_path,
        '[mcp_servers.variable]\nurl = "https://example.test/mcp"\nbearer_token_env_var = "BAD-NAME"\n'
        '[mcp_servers.collision]\nurl = "https://example.test/mcp"\n'
        'http_headers = { "X-Test" = "secret-one", "x-test" = "secret-two" }\n'
        '[mcp_servers.literal]\ncommand = "literal-server"\nenv = { LITERAL = "{{value}}" }\n'
        '[mcp_servers.headers]\nurl = "https://example.test/mcp"\n'
        'http_headers = { "X-Test" = "secret-invalid\\nvalue" }\n'
        '[mcp_servers.budgets]\ncommand = "budget-server"\nstartup_timeout_sec = 120\n'
        'tools = { inspect = { approval_mode = "prompt", output_token_limit = 600 } }\n',
    )
    app = _Preview(context, locale)
    async with app.run_test(size=(120, 42)) as pilot:
        await wait_for(
            lambda: len(app.dialog.query(_ImportCard)) == 5 and screen_is_settled(app, app.dialog),
            pilot=pilot,
            description="Codex repair notices have composed",
        )
        cards = {card.row.server_name: card for card in app.dialog.query(_ImportCard)}
        for name in ("variable", "collision"):
            cards[name].set_collapsed(False, scroll_visible=False)
        await pilot.pause()
        assert "bearer_token_env_var" in await _card_text(cards["variable"], pilot)
        assert "BAD-NAME" in await _card_text(cards["variable"], pilot)
        assert "x-test" in await _card_text(cards["collision"], pilot)
        assert (
            "Braces look different in the preview" if locale == "en" else "预览中的花括号写法有所变化"
        ) in await _card_text(cards["literal"], pilot)
        assert "http_headers.X-Test" in await _card_text(cards["headers"], pilot)
        assert "tools.inspect.output_token_limit" in await _card_text(cards["budgets"], pilot)
        assert (
            "one output limit for the whole server" if locale == "en" else "为整个服务器设置统一的输出上限"
        ) in await _card_text(cards["budgets"], pilot)
        budget_text = await _card_text(cards["budgets"], pilot)
        assert "startup_timeout_sec" in budget_text
        assert ("no separate startup timeout" if locale == "en" else "没有独立的启动超时设置") in budget_text
        assert ("MCP Request Timeout (seconds)" if locale == "en" else "MCP 请求超时（秒）") in budget_text  # noqa: RUF001
        assert cards["variable"].row.disabled and cards["collision"].row.disabled
        diagnostics = "\n".join(
            str(widget.render()) for card in cards.values() for widget in card.query(".mcp-import-warning")
        )
        assert "secret-" not in diagnostics
        for name in ("literal", "headers", "budgets"):
            cards[name].row.value = True
        await pilot.pause()
        assert "secret-" not in "\n".join(config.description for config in app.dialog._selected())


@pytest.mark.parametrize("locale", ["en", "zh-Hans"])
@pytest.mark.parametrize("with_static_headers", [False, True])
async def test_unimportable_codex_card_shows_environment_header_references_separately(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, locale: str, with_static_headers: bool
) -> None:
    monkeypatch.setenv("MCP_IMPORT_TEST", "resolved-lower-secret")
    monkeypatch.setenv("CODEX_PROBE_TOKEN", "resolved-upper-secret")
    settings = (
        'http_headers = {"x-test" = "literal-lower", "X-Test" = "literal-upper"}\n' if with_static_headers else ""
    )
    context = _source(
        tmp_path,
        '[mcp_servers.case-collision-env]\nurl = "http://127.0.0.1:18767/mcp"\n'
        + settings
        + 'env_http_headers = {"x-test" = "MCP_IMPORT_TEST", "X-Test" = "CODEX_PROBE_TOKEN"}\n',
    )
    app = _Preview(context, locale)
    async with app.run_test(size=(120, 42)) as pilot:
        await wait_for(
            lambda: len(app.dialog.query(_ImportCard)) == 1 and screen_is_settled(app, app.dialog),
            pilot=pilot,
            description="the Codex card with conflicting environment headers is listed",
        )
        card = app.dialog.query_one(_ImportCard)
        text = await _card_text(card, pilot)
        assert card.row.disabled
        assert app.dialog.query_one("#mcp-import-confirm", Button).disabled
        assert ("differ only in case" if locale == "en" else "仅大小写不同") in text
        assert "resolved-" not in text
        [(name, fields, tickable)] = _cards_in(app.dialog)
        assert name == "case-collision-env" and not tickable
        values = dict(fields)
        assert values["Headers From Environment Variables" if locale == "en" else "环境变量请求头"] == [
            ("x-test", "MCP_IMPORT_TEST"),
            ("X-Test", "CODEX_PROBE_TOKEN"),
        ]
        if with_static_headers:
            assert values["Headers" if locale == "en" else "请求头"] == [
                ("x-test", "literal-lower"),
                ("X-Test", "literal-upper"),
            ]
        else:
            assert ("Headers" if locale == "en" else "请求头") not in values


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (b"\xff", "UTF-8"),
        ('mcp_servers = "secret-invalid-shape"', "mcp_servers"),
        ('[mcp_servers.server]\ncommand = "secret-invalid-syntax', "line 2, column"),
    ],
)
async def test_codex_file_errors_have_safe_repair_information(tmp_path: Path, raw: str | bytes, expected: str) -> None:
    app = _Preview(_source(tmp_path, raw), "en")
    async with app.run_test(size=(120, 42)) as pilot:
        await wait_for(
            lambda: bool(app.dialog.query(".mcp-import-error")),
            pilot=pilot,
            description="Codex source error was rendered",
        )
        text = "\n".join(str(widget.render()) for widget in app.dialog.query(".mcp-import-error"))
        assert expected in text
        assert "reopen import" not in text
        assert "secret-" not in text
