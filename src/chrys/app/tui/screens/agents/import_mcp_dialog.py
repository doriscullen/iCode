# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Dialog that imports MCP servers configured in other agents (Claude Code, opencode, pi).

The left pane lists the agents detected on this machine; the right pane lists
the MCP servers the highlighted agent has configured. Ticked servers are
returned as configs for the MCP tab to add as unsaved cards.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Mapping
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Any, ClassVar, Literal

from rich.text import Text
from textual import on
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.content import Content
from textual.widgets import Button, Label, OptionList, Static
from textual.widgets.option_list import Option

from chrys.app.tui.binding_display import CANCEL_BINDING, localized_binding
from chrys.app.tui.i18n import render_str
from chrys.app.tui.screens.agents.panels.config_card import ConfigCard
from chrys.app.tui.screens.agents.panels.mcp import format_mcp_command_line
from chrys.app.tui.screens.agents.validation_messages import (
    MCP_COMMAND,
    MCP_ENABLED,
    MCP_ENVIRONMENT_VARIABLES,
    MCP_HEADERS,
    MCP_URL,
)
from chrys.app.tui.screens.dialogs.base import BaseDialog
from chrys.app.tui.util.removal import remove_children_shielded
from chrys.app.tui.widgets import Checkbox, DialogButtonRow, DialogButtonSpec
from chrys.foundation.branding import APP_DISPLAY_NAME
from chrys.foundation.i18n import MessageRef, msg
from chrys.foundation.i18n.formatting import format_message, sanitize_legacy_scalar
from chrys.service.mcp.importers import (
    ImportContext,
    ImportWarning,
    ImportWarningKind,
    McpImportCandidate,
    McpImportResult,
    McpImportSource,
    McpSourceError,
    SourceErrorKind,
    UnsupportedReason,
    detected_sources,
)
from chrys.service.profiles.agents.schema import MCPServerConfig

if TYPE_CHECKING:
    from collections.abc import Sequence

    from textual.app import ComposeResult
    from textual.widget import Widget

    from chrys.app.tui.i18n import LocaleController

logger = logging.getLogger(__name__)

_TITLE = msg("tui.mcp.import.title", fallback="Import from Other Applications")
_IMPORT = msg("tui.mcp.import.button.import", fallback="Import")
_CANCEL = msg("tui.mcp.import.button.cancel", fallback="Cancel")
_DETECTING = msg("tui.mcp.import.detecting", fallback="Looking for agents...")
_DETECT_FAILED = msg(
    "tui.mcp.import.detect_failed",
    fallback="Couldn't look for other agents. See the log for details.",
)
_LOADING = msg("tui.mcp.import.loading", fallback="Reading MCP servers...")
_NO_AGENTS = msg(
    "tui.mcp.import.no_agents",
    fallback="No supported agents found on this machine. Supported: {agents}.",
)
_NO_SERVERS = msg("tui.mcp.import.no_servers", fallback="No MCP servers configured in {agent}.")
_READ_FAILED = msg("tui.mcp.import.read_failed", fallback="Couldn't read the MCP servers of {agent}.")
_DESCRIPTION = msg(
    "tui.mcp.import.description",
    fallback="Imported from {agent}. There it is a {scope} server, used in every project.",
)
_DESCRIPTION_SCOPED = msg(
    "tui.mcp.import.description.scoped",
    fallback="Imported from {agent}. There it is a {scope} server, used only in {path}.",
)
_SCOPED_NOTICE = msg(
    "tui.mcp.import.scoped_notice",
    fallback=(
        "{agent} uses the servers marked {local} or {project} only in the folder their card names. "
        "Once imported, {app} uses them in every project."
    ),
)
_SCOPED_NOTICE_PROJECT = msg(
    "tui.mcp.import.scoped_notice.project",
    fallback=(
        "{agent} uses the servers marked {project} only in the folder their card names. "
        "Once imported, {app} uses them in every project."
    ),
)
_SCOPE_FOLDER = msg("tui.mcp.import.field.folder", fallback="{agent} uses it only in")

_SCOPE_USER = msg("tui.mcp.import.scope.user", fallback="user")
_SCOPE_LOCAL = msg("tui.mcp.import.scope.local", fallback="local")
_SCOPE_PROJECT = msg("tui.mcp.import.scope.project", fallback="project")
_SCOPE_GLOBAL = msg("tui.mcp.import.scope.global", fallback="global")
_UNSUPPORTED_TRANSPORT = msg(
    "tui.mcp.import.unsupported.transport",
    fallback="Can't import: {app} supports only STDIO and HTTP MCP servers.",
)
_TEMPLATE_IN_URL = msg(
    "tui.mcp.import.unsupported.template_in_url",
    fallback="Can't import: the URL uses an environment variable, which {app} doesn't resolve in URLs.",
)
_TEMPLATE_IN_COMMAND = msg(
    "tui.mcp.import.unsupported.template_in_command",
    fallback="Can't import: the command uses an environment variable, which {app} doesn't resolve in commands.",
)
_TEMPLATE_NEXT_TO_BRACE = msg(
    "tui.mcp.import.unsupported.template_next_to_brace",
    fallback=(
        "Can't import: an environment variable or header value has a brace right next to a variable, "
        "which {app} can't write the same way."
    ),
)
_FILE_REFERENCE = msg(
    "tui.mcp.import.unsupported.file_reference",
    fallback="Can't import: a setting takes its value from a file, which {app} doesn't read.",
)
_VALUE_FROM_COMMAND = msg(
    "tui.mcp.import.unsupported.value_from_command",
    fallback=(
        "Can't import: an environment variable or header value is the output of a command, which {app} doesn't run."
    ),
)
_VARIABLE_NAME = msg(
    "tui.mcp.import.unsupported.variable_name",
    fallback="Can't import: a setting uses an environment variable whose name {app} can't write.",
)
_UNTRUSTED_PROJECT = msg(
    "tui.mcp.import.unsupported.untrusted_project",
    fallback="Can't import: {agent} doesn't trust this folder, so it doesn't run this server.",
)
_REMOTE_ENVIRONMENT = msg(
    "tui.mcp.import.unsupported.remote_environment",
    fallback="Can't import: this server uses a remote execution environment, which {app} can't reproduce.",
)
_COVERED = msg(
    "tui.mcp.import.covered",
    fallback="Same server as {name} ({agent}, {scope}), which is ticked. It is imported once.",
)
# The value as written is left out on purpose: the warning is saved in the description, and the default may be a secret.
_DEFAULT_DROPPED = msg(
    "tui.mcp.import.warning.default_dropped",
    fallback=(
        "{field} gave {variable} a default value. {app} variables can't have one, so it is now {converted}. "
        "Set {variable} before starting {app}, or the connection fails."
    ),
)
_HEADERS_HELPER_IGNORED = msg(
    "tui.mcp.import.warning.headers_helper_ignored",
    fallback=(
        "The headers helper command {command} is not imported: {app} doesn't run it. "
        "Add the headers it produces under Headers yourself."
    ),
)
_OAUTH_IGNORED = msg(
    "tui.mcp.import.warning.oauth_ignored",
    fallback=(
        "The OAuth sign-in settings are not imported: {app} doesn't sign in to MCP servers with OAuth. "
        "Add an Authorization header under Headers yourself, or the server may refuse the connection."
    ),
)
_CREDENTIAL_VARIABLE = msg(
    "tui.mcp.import.warning.credential_variable",
    fallback=(
        "{field} references {variable}, which {agent} never sends to a remote server. {app} does send "
        "its real value: remove the header, or copy the credential into a variable with another name "
        "and reference that."
    ),
)
_CLAUDE_PROJECT_DIR = msg(
    "tui.mcp.import.warning.claude_project_dir",
    fallback=(
        "{field} references {variable}, which {agent} sets to the project folder for the servers it starts. "
        "{app} doesn't set it: set it before starting {app}, or write the folder path instead."
    ),
)
_DISABLED = msg(
    "tui.mcp.import.warning.disabled",
    fallback="{agent} has this server turned off, so it is imported turned off. Tick {enabled} in its card to use it.",
)
_CWD_IGNORED = msg(
    "tui.mcp.import.warning.cwd_ignored",
    fallback=(
        "{agent} starts this server in {folder}. {app} starts it in the working folder, "
        "so a server that needs that folder may not work."
    ),
)
_MERGED = msg(
    "tui.mcp.import.warning.merged",
    fallback="{agent} combines this server's settings from more than one file. Also from: {files}.",
)
_MERGED_VALUES = msg(
    "tui.mcp.import.warning.merged.values",
    fallback=(
        "{agent} combines this server's settings from more than one file. Also from: {files}. "
        "{fields} come from a different file than the command or URL, and are sent to it too."
    ),
)
_AUTH_PROVIDER_IGNORED = msg(
    "tui.mcp.import.warning.auth_provider_ignored",
    fallback=(
        "{agent} sends this server the token of its {provider} sign-in; {app} doesn't. "
        "Add an Authorization header under Headers yourself, or the server may refuse the connection."
    ),
)
_TOOL_RULES_IGNORED = msg(
    "tui.mcp.import.warning.tool_rules_ignored",
    fallback=(
        "Not imported, {app} can't apply {agent}'s settings for single tools: {rules}. "
        "Set the available tools yourself if needed."
    ),
)
_TOOLS_LIMITED_NONE = msg(
    "tui.mcp.import.warning.tools_limited.none",
    fallback="{agent} hides this server's tools ({rule}), so it is imported with no tools.",
)
_TOOLS_LIMITED_SOME = msg(
    "tui.mcp.import.warning.tools_limited.some",
    fallback="{agent} hides this server's tools ({rule}) except some, so it is imported with only: {tools}.",
)
_CODEMODE_OFF = msg(
    "tui.mcp.import.warning.codemode_off",
    fallback=(
        "{agent} calls this server's tools only from codemode, which it doesn't turn on (autoEnableCodemode is "
        "false), so it can't call them unless codemode is on. {app} loads them on demand."
    ),
)
_VARIABLE_UNSET = msg(
    "tui.mcp.import.warning.variable_unset",
    fallback=(
        "{field} references {variable}, which is not set now. {agent} then uses an empty value, "
        "but {app} fails to connect. Set {variable} before starting {app}."
    ),
)
_OPTIONAL_VARIABLE = msg(
    "tui.mcp.import.warning.optional_variable",
    fallback=(
        "{field} references {variable}. {agent} can skip this reference when the variable is unavailable; "
        "{app} requires a nonempty value. Set {variable} before starting {app}, or remove this setting."
    ),
)
_DENY_LIST = msg(
    "tui.mcp.import.warning.deny_list",
    fallback=(
        "{agent} blocks these tools: {tools}. {app} needs a list of allowed tools instead, so this server is "
        "imported with no tools. Select the tools you want under Available Tool Scope."
    ),
)
_UNKNOWN_FIELDS_IGNORED = msg(
    "tui.mcp.import.warning.unknown_fields_ignored",
    fallback="Not imported, {app} has no matching setting: {fields}.",
)
_UNREADABLE = msg("tui.mcp.import.source_error.unreadable", fallback="Couldn't read {path}.")
_TOO_LARGE = msg("tui.mcp.import.source_error.too_large", fallback="{path} is too large to read.")
_INVALID_JSON = msg("tui.mcp.import.source_error.invalid_json", fallback="{path} is not valid JSON.")
_INVALID_TOML = msg("tui.mcp.import.source_error.invalid_toml", fallback="{path} is not valid TOML.")
_NOT_AN_OBJECT = msg(
    "tui.mcp.import.source_error.not_an_object",
    fallback="{path} does not contain a settings object.",
)

_SCOPE_LABELS = {"user": _SCOPE_USER, "local": _SCOPE_LOCAL, "project": _SCOPE_PROJECT, "global": _SCOPE_GLOBAL}
_UNSUPPORTED_REASONS = {
    UnsupportedReason.REMOTE_ENVIRONMENT: _REMOTE_ENVIRONMENT,
    UnsupportedReason.UNSUPPORTED_TRANSPORT: _UNSUPPORTED_TRANSPORT,
    UnsupportedReason.TEMPLATE_IN_URL: _TEMPLATE_IN_URL,
    UnsupportedReason.TEMPLATE_IN_COMMAND: _TEMPLATE_IN_COMMAND,
    UnsupportedReason.TEMPLATE_NEXT_TO_BRACE: _TEMPLATE_NEXT_TO_BRACE,
    UnsupportedReason.FILE_REFERENCE: _FILE_REFERENCE,
    UnsupportedReason.VALUE_FROM_COMMAND: _VALUE_FROM_COMMAND,
    UnsupportedReason.VARIABLE_NAME: _VARIABLE_NAME,
}
_INVALID_CONFIG = msg(
    "tui.mcp.import.source_error.invalid_config",
    fallback="{path} has settings {agent} rejects, so its servers are not listed.",
)
_SOURCE_ERRORS = {
    SourceErrorKind.INVALID_TOML: _INVALID_TOML,
    SourceErrorKind.UNREADABLE: _UNREADABLE,
    SourceErrorKind.TOO_LARGE: _TOO_LARGE,
    SourceErrorKind.INVALID_JSON: _INVALID_JSON,
    SourceErrorKind.NOT_AN_OBJECT: _NOT_AN_OBJECT,
}


type _CandidateState = Literal["importable", "unsupported", "added"]

_STATE_ORDER: dict[_CandidateState, int] = {"importable": 0, "unsupported": 1, "added": 2}


def _source_error(error: McpSourceError, agent: str) -> MessageRef:
    path = sanitize_legacy_scalar(error.path)
    if error.kind is SourceErrorKind.INVALID_CONFIG:
        return _INVALID_CONFIG.bind(path=path, agent=agent)
    return _SOURCE_ERRORS[error.kind].bind(path=path)


def is_already_added(config: MCPServerConfig, existing: Sequence[MCPServerConfig]) -> bool:
    """Whether *existing* already has a server with *config*'s connection.

    Names are not compared (two servers may share one); neither are env and
    headers, which hold credentials the user may have edited since importing.
    """
    return any(_connection_key(config) == _connection_key(server) for server in existing)


def _key_value_lines(values: Mapping[Any, Any]) -> tuple[str, ...]:
    return tuple(
        f"{sanitize_legacy_scalar(str(key))} = {sanitize_legacy_scalar(str(value))}" for key, value in values.items()
    )


def _connection_key(config: MCPServerConfig) -> tuple[str, ...]:
    if config.transport == "http":
        url = config.url.strip()
        return ("http", url.removesuffix("/"))
    return ("stdio", config.command.strip(), *config.args)


class _ImportRow(Checkbox):
    """One server's checkbox, labelled with its name.

    It names its agent too: its Changed may land after the pane switched agents.
    """

    def __init__(self, name: str, *, source_id: str, index: int, value: bool, disabled: bool) -> None:
        super().__init__(
            Content.from_text(name, markup=False),
            value=value,
            disabled=disabled,
            id=f"mcp-import-row-{index}",
            classes="mcp-import-row",
        )
        self.source_id = source_id
        self.index = index


@dataclass(frozen=True, slots=True)
class _CardField:
    label: str
    lines: tuple[str, ...]


class _ImportCard(ConfigCard):
    """A server's settings, read-only, under a header that ticks it: its name, then where it came from.

    An importable server is locked while a ticked one with the same connection
    covers it; the covered note says which.
    """

    def __init__(
        self,
        *,
        index: int,
        meta: str,
        row: _ImportRow,
        importable: bool,
        fields: Sequence[_CardField],
        notes: Sequence[str],
        warnings: Sequence[str],
        covered: str | None = None,
    ) -> None:
        self._meta = meta
        self._row = row
        self._importable = importable
        self._fields = list(fields)
        self._notes = list(notes)
        self._warnings = list(warnings)
        self._covered_note = Static(Text(covered or ""), classes="mcp-import-note")
        self._covered_note.display = covered is not None
        classes = "mcp-import-card" + (" -unavailable" if row.disabled else "")
        super().__init__(index, classes=classes, read_only=True)

    @property
    def row(self) -> _ImportRow:
        return self._row

    @property
    def importable(self) -> bool:
        """Whether the server can be imported at all, covered or not."""
        return self._importable

    def show_covered(self, note: str | None) -> None:
        """Lock the card with *note* while a ticked server covers it; unlock it when *note* is None."""
        if not self._importable:
            return
        covered = note is not None
        self._row.disabled = covered
        self.set_class(covered, "-unavailable")
        self._covered_note.update(Text(note or ""))
        self._covered_note.display = covered

    @property
    def meta(self) -> str:
        """Where the server came from and how it connects, shown dimmed beside its name."""
        return self._meta

    def compose(self) -> ComposeResult:
        with Horizontal(classes="mcp-import-card-header"):
            yield self._row
            yield Label(Content.from_text(self._meta, markup=False), classes="mcp-import-card-meta")
        with Vertical(classes="mcp-import-card-body"):
            for field in self._fields:
                yield Label(Content.from_text(field.label, markup=False), classes="mcp-import-field-label")
                yield Static(Text("\n".join(field.lines)), classes="mcp-import-field-value")
            for note in self._notes:
                yield Static(Text(note), classes="mcp-import-note")
            yield self._covered_note
            for warning in self._warnings:
                yield Static(Text(warning), classes="mcp-import-warning")


class ImportMcpServersDialog(BaseDialog[list[MCPServerConfig] | None]):
    """Pick MCP servers from other agents.

    Dismisses with the ticked servers' configs, each described as imported, or None.
    """

    BINDINGS: ClassVar[list] = [
        localized_binding("escape", "cancel", CANCEL_BINDING, show=False, priority=True),
    ]

    CSS_PATH = "import_mcp_dialog.tcss"

    def __init__(
        self,
        *,
        workspace_cwd: str | None,
        existing: Sequence[MCPServerConfig],
        locale_controller: LocaleController | None,
        sources: Sequence[McpImportSource] | None = None,
        context: ImportContext | None = None,
    ) -> None:
        self._locale_controller = locale_controller
        self._existing = list(existing)
        self._import_context = context if context is not None else ImportContext.current(workspace_cwd)
        self._all_sources = sources
        self._sources: list[McpImportSource] = []
        self._results: dict[str, McpImportResult | None] = {}
        self._ticked: dict[str, set[int]] = {}
        self._shown_source: str | None = None
        # The message the pane shows instead of servers, re-rendered when the locale changes.
        self._pane_message: MessageRef | None = None
        # Each pane change takes a generation; a pane read that finishes after a newer change was asked for is dropped.
        self._generation = 0
        self._pane_lock = asyncio.Lock()
        super().__init__()

    def _render_message(self, reference: MessageRef) -> str:
        controller = self._locale_controller
        return format_message(reference) if controller is None else render_str(controller.localizer, reference)

    def compose(self) -> ComposeResult:
        with Vertical(id="mcp-import-container") as container:
            container.border_title = Text(self._render_message(_TITLE.bind()))
            with Horizontal(id="mcp-import-body"):
                with Vertical(id="mcp-import-sidebar") as sidebar:
                    sidebar.display = False
                    yield OptionList(id="mcp-import-sources")
                # The buttons act on the servers listed, so they sit under that list.
                with Vertical(id="mcp-import-right"):
                    yield VerticalScroll(id="mcp-import-servers")
                    yield DialogButtonRow(
                        DialogButtonSpec(
                            Text(self._render_message(_IMPORT.bind())), id="mcp-import-confirm", disabled=True
                        ),
                        DialogButtonSpec(
                            Text(self._render_message(_CANCEL.bind())), id="mcp-import-cancel", variant="warning"
                        ),
                        id="mcp-import-buttons",
                    )

    async def on_mount(self) -> None:
        if self._locale_controller is not None:
            self._locale_controller.register_surface(self)
        await self._show_message(_DETECTING.bind(), self._generation)
        self.run_worker(self._detect(), group="mcp-import-detect", exclusive=True, exit_on_error=False)

    def on_unmount(self) -> None:
        if self._locale_controller is not None:
            self._locale_controller.unregister_surface(self)

    def refresh_localization(self) -> None:
        self.query_one("#mcp-import-container").border_title = Text(self._render_message(_TITLE.bind()))
        self.query_one("#mcp-import-confirm", Button).label = Text(self._render_message(_IMPORT.bind()))
        self.query_one("#mcp-import-cancel", Button).label = Text(self._render_message(_CANCEL.bind()))
        if self._shown_source is not None:
            self._request_source(self._shown_source)
        elif self._pane_message is not None:
            self.run_worker(
                self._show_message_safely(self._pane_message, self._generation),
                group="mcp-import-pane",
                exit_on_error=False,
            )

    async def _detect(self) -> None:
        # The worker doesn't exit the app on an error and nothing awaits it: say so here, or the pane spins forever.
        try:
            await self._list_sources()
        except Exception:
            logger.exception("Listing agents to import MCP servers from failed")
            await self._show_message_safely(_DETECT_FAILED.bind(), self._generation)

    async def _list_sources(self) -> None:
        try:
            sources = await asyncio.to_thread(detected_sources, self._import_context, self._all_sources)
        except Exception:
            logger.exception("Detecting agents to import MCP servers from failed")
            sources = []
        self._sources = sources
        if not sources:
            from chrys.service.mcp.importers import IMPORT_SOURCES

            supported = ", ".join(
                source.display_name for source in (IMPORT_SOURCES if self._all_sources is None else self._all_sources)
            )
            await self._show_message(_NO_AGENTS.bind(agents=supported), self._generation)
            return
        option_list = self.query_one("#mcp-import-sources", OptionList)
        option_list.add_options(
            Option(Content.styled(sanitize_legacy_scalar(source.display_name), "bold"), id=source.source_id)
            for source in sources
        )
        self.query_one("#mcp-import-sidebar").display = True
        option_list.highlighted = 0
        option_list.focus()

    @on(OptionList.OptionHighlighted, "#mcp-import-sources")
    def _on_source_highlighted(self, event: OptionList.OptionHighlighted) -> None:
        source_id = event.option.id
        if source_id is None or source_id == self._shown_source:
            return
        self._request_source(source_id)

    def _request_source(self, source_id: str) -> None:
        """Show *source_id*'s servers.

        The pane's source and generation change here, not in the worker: a
        highlight that lands before the worker starts must compare against them.
        """
        self._generation += 1
        self._shown_source = source_id
        self.run_worker(self._show_source(source_id, self._generation), group="mcp-import-pane", exit_on_error=False)

    async def _show_source(self, source_id: str, generation: int) -> None:
        source = next((item for item in self._sources if item.source_id == source_id), None)
        if source is None:
            return
        # The worker doesn't exit the app on an error and nothing awaits it: say so here, or the pane spins forever.
        try:
            if source_id not in self._results:
                await self._show_message(_LOADING.bind(), generation)
                try:
                    result: McpImportResult | None = await asyncio.to_thread(source.discover, self._import_context)
                except Exception:
                    logger.exception("Reading MCP servers from %s failed", source.display_name)
                    result = None
                self._results[source_id] = result
            async with self._pane_lock:
                if generation == self._generation:
                    await self._mount_pane(self._pane_widgets(source, self._results[source_id]))
                    self._pane_message = None
        except Exception:
            logger.exception("Showing the MCP servers of %s failed", source.display_name)
            await self._show_message_safely(_READ_FAILED.bind(agent=source.display_name), generation)

    async def _show_message(self, reference: MessageRef, generation: int) -> None:
        async with self._pane_lock:
            if generation == self._generation:
                await self._mount_pane([Static(Text(self._render_message(reference)), classes="mcp-import-message")])
                self._pane_message = reference

    async def _show_message_safely(self, reference: MessageRef, generation: int) -> None:
        """Show *reference*, logging a failure: nothing awaits these workers, and the dialog may be closing."""
        try:
            await self._show_message(reference, generation)
        except Exception:
            logger.exception("Showing a message in the MCP import dialog failed")

    async def _mount_pane(self, widgets: list[Widget]) -> None:
        """Replace the right pane; callers hold ``_pane_lock`` so two rebuilds never interleave."""
        pane = self.query_one("#mcp-import-servers", VerticalScroll)
        with self.app.batch_update():
            await remove_children_shielded(pane)
            await pane.mount(*widgets)

    def _pane_widgets(self, source: McpImportSource, result: McpImportResult | None) -> list[Widget]:
        agent = source.display_name
        if result is None:
            return [Static(Text(self._render_message(_READ_FAILED.bind(agent=agent))), classes="mcp-import-message")]
        ticked = self._ticked.setdefault(source.source_id, set())
        # Problems reading the agent's files come first, where they are seen without scrolling.
        widgets: list[Widget] = [
            Static(Text(self._render_message(_source_error(error, agent))), classes="mcp-import-error")
            for error in result.source_errors
        ]
        # Said once for the whole list: importing makes a folder-only server global.
        if any(candidate.scope_path for candidate in result.candidates):
            if any(candidate.scope == "local" for candidate in result.candidates):
                notice = _SCOPED_NOTICE.bind(
                    agent=agent,
                    local=self._scope_label("local"),
                    project=self._scope_label("project"),
                    app=APP_DISPLAY_NAME,
                )
            else:
                notice = _SCOPED_NOTICE_PROJECT.bind(
                    agent=agent, project=self._scope_label("project"), app=APP_DISPLAY_NAME
                )
            widgets.append(Static(Text(self._render_message(notice)), classes="mcp-import-notice"))
        # Importable servers first, then those that can't be imported, then those this agent already has.
        states = [
            (index, candidate, self._candidate_state(candidate)) for index, candidate in enumerate(result.candidates)
        ]
        for index, candidate, state in sorted(states, key=lambda item: _STATE_ORDER[item[2]]):
            if state != "importable":
                ticked.discard(index)
            # An already-added server shows ticked: it is in the agent, and it can't be ticked off from here.
            value = state == "added" or index in ticked
            covered = (
                self._covering(source.source_id, index, candidate.config)
                if state == "importable" and candidate.config is not None
                else None
            )
            row = _ImportRow(
                sanitize_legacy_scalar(candidate.name),
                source_id=source.source_id,
                index=index,
                value=value,
                disabled=state != "importable" or covered is not None,
            )
            note = self._unsupported_note(candidate, agent) if state == "unsupported" else None
            widgets.append(
                _ImportCard(
                    index=index,
                    meta=self._candidate_meta(candidate),
                    row=row,
                    importable=state == "importable",
                    covered=covered,
                    fields=self._card_fields(candidate, agent),
                    notes=[] if note is None else [note],
                    # What the conversion changed only matters for a server that will be imported.
                    warnings=[]
                    if state == "added"
                    else [self._warning_text(warning, agent) for warning in candidate.warnings],
                )
            )
        if not result.candidates:
            widgets.append(
                Static(Text(self._render_message(_NO_SERVERS.bind(agent=agent))), classes="mcp-import-message")
            )
        self._sync_import_button()
        return widgets

    def _candidate_state(self, candidate: McpImportCandidate) -> _CandidateState:
        if candidate.config is None:
            return "unsupported"
        return "added" if is_already_added(candidate.config, self._existing) else "importable"

    def _covering(self, source_id: str, index: int, config: MCPServerConfig) -> str | None:
        """The note locking a server that a ticked one with the same connection covers, in any agent.

        A ticked server is never covered, so ticking stays reversible; the
        import keeps one server per connection should two end up ticked.
        """
        if index in self._ticked.get(source_id, set()):
            return None
        key = _connection_key(config)
        for source in self._sources:
            result = self._results.get(source.source_id)
            if result is None:
                continue
            for other_index in sorted(self._ticked.get(source.source_id, set())):
                other = result.candidates[other_index]
                if other.config is not None and _connection_key(other.config) == key:
                    return self._render_message(
                        _COVERED.bind(
                            name=sanitize_legacy_scalar(other.name),
                            agent=source.display_name,
                            scope=self._scope_label(other.scope),
                        )
                    )
        return None

    def _sync_covered(self) -> None:
        """Lock or unlock the shown cards after a tick changed which connections are taken."""
        for card in self.query(_ImportCard):
            row = card.row
            result = self._results.get(row.source_id)
            if result is None or not card.importable:
                continue
            config = result.candidates[row.index].config
            if config is not None:
                card.show_covered(self._covering(row.source_id, row.index, config))

    def _unsupported_note(self, candidate: McpImportCandidate, agent: str) -> str | None:
        if candidate.unsupported is None:
            return None
        if candidate.unsupported is UnsupportedReason.UNTRUSTED_PROJECT:
            return self._render_message(_UNTRUSTED_PROJECT.bind(agent=agent))
        return self._render_message(_UNSUPPORTED_REASONS[candidate.unsupported].bind(app=APP_DISPLAY_NAME))

    def _card_fields(self, candidate: McpImportCandidate, agent: str) -> list[_CardField]:
        """Where the source uses the server, if only in one folder, then its settings."""
        fields: list[_CardField] = []
        if candidate.scope_path:
            label = self._render_message(_SCOPE_FOLDER.bind(agent=agent))
            fields.append(_CardField(label, (sanitize_legacy_scalar(candidate.scope_path),)))
        return fields + self._settings_fields(candidate)

    def _settings_fields(self, candidate: McpImportCandidate) -> list[_CardField]:
        """The settings the source gave: the converted config, or as written when it can't be imported."""
        config = candidate.config
        if config is not None:
            if config.transport == "http":
                return self._fields(url=config.url, headers=config.headers)
            return self._fields(command=format_mcp_command_line(config.command, config.args), env=config.env)
        entry = candidate.entry or {}
        command = entry.get("command")
        args = entry.get("args")
        command_line = None
        if isinstance(command, str):
            arg_list = [arg for arg in args if isinstance(arg, str)] if isinstance(args, list) else []
            command_line = format_mcp_command_line(command, arg_list)
        url = entry.get("url")
        return self._fields(
            command=command_line,
            url=url if isinstance(url, str) else None,
            env=entry.get("env"),
            headers=entry.get("headers"),
        )

    def _fields(
        self,
        *,
        command: str | None = None,
        url: str | None = None,
        env: object = None,
        headers: object = None,
    ) -> list[_CardField]:
        fields: list[_CardField] = []
        if command:
            fields.append(_CardField(self._render_message(MCP_COMMAND.bind()), (sanitize_legacy_scalar(command),)))
        if url:
            fields.append(_CardField(self._render_message(MCP_URL.bind()), (sanitize_legacy_scalar(url),)))
        for label, values in ((MCP_ENVIRONMENT_VARIABLES, env), (MCP_HEADERS, headers)):
            if isinstance(values, Mapping) and values:
                fields.append(_CardField(self._render_message(label.bind()), _key_value_lines(values)))
        return fields

    def _candidate_meta(self, candidate: McpImportCandidate) -> str:
        parts = [self._scope_label(candidate.scope)]
        if candidate.transport:
            parts.append(sanitize_legacy_scalar(candidate.transport.upper()))
        return " · ".join(parts)

    def _scope_label(self, scope: str) -> str:
        label = _SCOPE_LABELS.get(scope)
        return sanitize_legacy_scalar(scope) if label is None else self._render_message(label.bind())

    def _warning_text(self, warning: ImportWarning, agent: str) -> str:
        variable = sanitize_legacy_scalar(warning.variable)
        field = sanitize_legacy_scalar(warning.field)
        original = sanitize_legacy_scalar(warning.original)
        match warning.kind:
            case ImportWarningKind.DEFAULT_DROPPED:
                reference = _DEFAULT_DROPPED.bind(
                    field=field,
                    converted="{{" + variable + "}}",
                    variable=variable,
                    app=APP_DISPLAY_NAME,
                )
            case ImportWarningKind.HEADERS_HELPER_IGNORED:
                reference = _HEADERS_HELPER_IGNORED.bind(command=original, app=APP_DISPLAY_NAME)
            case ImportWarningKind.OAUTH_IGNORED:
                reference = _OAUTH_IGNORED.bind(app=APP_DISPLAY_NAME)
            case ImportWarningKind.CREDENTIAL_VARIABLE:
                reference = _CREDENTIAL_VARIABLE.bind(field=field, variable=variable, agent=agent, app=APP_DISPLAY_NAME)
            case ImportWarningKind.CLAUDE_PROJECT_DIR:
                reference = _CLAUDE_PROJECT_DIR.bind(field=field, variable=variable, agent=agent, app=APP_DISPLAY_NAME)
            case ImportWarningKind.UNKNOWN_FIELDS_IGNORED:
                reference = _UNKNOWN_FIELDS_IGNORED.bind(fields=original, app=APP_DISPLAY_NAME)
            case ImportWarningKind.DISABLED:
                reference = _DISABLED.bind(agent=agent, enabled=self._render_message(MCP_ENABLED.bind()))
            case ImportWarningKind.CWD_IGNORED:
                reference = _CWD_IGNORED.bind(agent=agent, folder=original, app=APP_DISPLAY_NAME)
            case ImportWarningKind.MERGED:
                reference = (
                    _MERGED_VALUES.bind(agent=agent, files=original, fields=field)
                    if field
                    else _MERGED.bind(agent=agent, files=original)
                )
            case ImportWarningKind.AUTH_PROVIDER_IGNORED:
                reference = _AUTH_PROVIDER_IGNORED.bind(agent=agent, provider=original, app=APP_DISPLAY_NAME)
            case ImportWarningKind.TOOL_RULES_IGNORED:
                reference = _TOOL_RULES_IGNORED.bind(agent=agent, rules=original, app=APP_DISPLAY_NAME)
            case ImportWarningKind.TOOLS_LIMITED:
                reference = (
                    _TOOLS_LIMITED_SOME.bind(agent=agent, rule=field, tools=original)
                    if original
                    else _TOOLS_LIMITED_NONE.bind(agent=agent, rule=field)
                )
            case ImportWarningKind.CODEMODE_OFF:
                reference = _CODEMODE_OFF.bind(agent=agent, app=APP_DISPLAY_NAME)
            case ImportWarningKind.VARIABLE_UNSET:
                reference = _VARIABLE_UNSET.bind(field=field, variable=variable, agent=agent, app=APP_DISPLAY_NAME)
            case ImportWarningKind.OPTIONAL_VARIABLE:
                reference = _OPTIONAL_VARIABLE.bind(field=field, variable=variable, agent=agent, app=APP_DISPLAY_NAME)
            case ImportWarningKind.DENY_LIST:
                reference = _DENY_LIST.bind(agent=agent, tools=original, app=APP_DISPLAY_NAME)
        return self._render_message(reference)

    def _description(self, agent: str, candidate: McpImportCandidate) -> MessageRef:
        scope = self._scope_label(candidate.scope)
        if candidate.scope_path:
            return _DESCRIPTION_SCOPED.bind(agent=agent, scope=scope, path=sanitize_legacy_scalar(candidate.scope_path))
        return _DESCRIPTION.bind(agent=agent, scope=scope)

    @on(Checkbox.Changed, ".mcp-import-row")
    def _on_row_changed(self, event: Checkbox.Changed) -> None:
        event.stop()
        row = event.checkbox
        if not isinstance(row, _ImportRow):
            return
        ticked = self._ticked.setdefault(row.source_id, set())
        if event.value:
            ticked.add(row.index)
        else:
            ticked.discard(row.index)
        self._sync_covered()
        self._sync_import_button()

    def _sync_import_button(self) -> None:
        self.query_one("#mcp-import-confirm", Button).disabled = not any(self._ticked.values())

    def _selected(self) -> list[MCPServerConfig]:
        selected: list[MCPServerConfig] = []
        # One server per connection, as the covered cards promise, even if two were ticked at once.
        taken: set[tuple[str, ...]] = set()
        for source in self._sources:
            result = self._results.get(source.source_id)
            ticked = self._ticked.get(source.source_id, set())
            if result is None:
                continue
            for index, candidate in enumerate(result.candidates):
                if index not in ticked or candidate.config is None:
                    continue
                key = _connection_key(candidate.config)
                if key in taken:
                    continue
                taken.add(key)
                origin = self._render_message(self._description(source.display_name, candidate))
                # The import's warnings stay with the server: the dialog that showed them is about to close.
                notes = [f"- {self._warning_text(warning, source.display_name)}" for warning in candidate.warnings]
                # The source's own description of the server comes first.
                own = [candidate.config.description] if candidate.config.description else []
                selected.append(replace(candidate.config, description="\n".join([*own, origin, *notes])))
        return selected

    @on(Button.Pressed, "#mcp-import-confirm")
    def _on_import(self, event: Button.Pressed) -> None:
        event.stop()
        selected = self._selected()
        if selected:
            self.dismiss(selected)

    @on(Button.Pressed, "#mcp-import-cancel")
    def _on_cancel(self, event: Button.Pressed) -> None:
        event.stop()
        self.dismiss(None)

    def action_cancel(self) -> None:
        self.dismiss(None)

    def _default_dismiss_result(self) -> list[MCPServerConfig] | None:  # type: ignore[override]
        return None
