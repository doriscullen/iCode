# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Dialog that imports MCP servers configured in other agents.

The left pane lists the agents that have MCP server configuration on this
machine; the right pane lists the MCP servers the highlighted agent has
configured. Ticked servers are returned as configs for the MCP tab to add as
unsaved cards.
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
from collections.abc import Mapping
from dataclasses import asdict, dataclass, replace
from typing import TYPE_CHECKING, Any, ClassVar, Literal

from rich.text import Text
from textual import on
from textual.containers import Grid, Horizontal, Vertical, VerticalScroll
from textual.content import Content
from textual.message import Message
from textual.widgets import Button, Label, OptionList, Static
from textual.widgets.option_list import Option

from chrys.app.tui.binding_display import CANCEL_BINDING, localized_binding
from chrys.app.tui.i18n import render_str
from chrys.app.tui.screens.agents.panels.mcp import (
    _LOADING_STRATEGY,
    _TOOL_LOADING_FULL_LABEL,
    _TOOL_LOADING_PROGRESSIVE_LABEL,
    format_mcp_command_line,
)
from chrys.app.tui.screens.agents.validation_messages import (
    DESCRIPTION,
    MCP_COMMAND,
    MCP_ENABLED,
    MCP_ENVIRONMENT_VARIABLES,
    MCP_HEADERS,
    MCP_INITIALLY_VISIBLE_TOOLS,
    MCP_TOOL_ACCESS_NONE,
    MCP_TOOL_NAME_PREFIX,
    MCP_TRANSPORT_LABEL,
    MCP_URL,
)
from chrys.app.tui.screens.dialogs.base import BaseDialog
from chrys.app.tui.util.removal import remove_children_shielded
from chrys.app.tui.widgets import Checkbox, DialogButtonRow, DialogButtonSpec, HatchedEmptyState
from chrys.app.tui.widgets.config_card import CollapsibleConfigCard
from chrys.foundation.branding import APP_DISPLAY_NAME
from chrys.foundation.i18n import MessageRef, msg
from chrys.foundation.i18n.formatting import format_message, sanitize_legacy_block, sanitize_legacy_scalar
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
    with_tool_name_prefix,
)
from chrys.service.mcp.importers.base import MAX_SOURCE_BYTES
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
_NOT_ENABLED = msg("tui.mcp.import.field.enabled.no", fallback="No")
_DETECTING = msg("tui.mcp.import.detecting", fallback="Looking for agents...")
_DETECT_FAILED = msg(
    "tui.mcp.import.detect_failed",
    fallback="Couldn't look for other agents. See the log for details.",
)
_LOADING = msg("tui.mcp.import.loading", fallback="Reading MCP servers...")
_NO_AGENTS = msg(
    "tui.mcp.import.no_agents",
    fallback=(
        "No MCP configuration found for supported applications: {agents}. "
        "Check the source application's configuration folder and the current working folder, then reopen import."
    ),
)
_EMPTY_SOURCE = msg("tui.mcp.import.empty_source", fallback="No MCP servers found.")
_EMPTY_SOURCE_ACTION = msg(
    "tui.mcp.import.empty_source.action",
    fallback="Check the source application's configuration folder and the current working folder, then reopen import.",
)
_READ_FAILED = msg(
    "tui.mcp.import.read_failed",
    fallback="Couldn't read the MCP servers of {agent}. Check file permissions and the log, then reopen import.",
)
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
        "In {agent}, servers with {local} or {project} scope are used only in the folder shown on each card. "
        "After import, {app} uses them in every project."
    ),
)
_SCOPED_NOTICE_PROJECT = msg(
    "tui.mcp.import.scoped_notice.project",
    fallback=(
        "In {agent}, servers with {project} scope are used only in the folder shown on each card. "
        "After import, {app} uses them in every project."
    ),
)
_SCOPE = msg("tui.mcp.import.field.scope", fallback="Scope in {agent}")
_SCOPE_EVERY_PROJECT = msg("tui.mcp.import.field.scope.every_project", fallback="{scope}, used in every project")
_SCOPE_ONLY_IN = msg("tui.mcp.import.field.scope.only_in", fallback="{scope}, used only in {path}")
_CONFIGURATION_SOURCE = msg("tui.mcp.import.field.configuration_source", fallback="Configuration Source")
_TRANSPORT_OPTIONS = msg("tui.mcp.import.field.transport_options", fallback="{transport} Options")
_REQUEST_TIMEOUT = msg("tui.mcp.import.field.request_timeout", fallback="MCP Request Timeout")
_TIMEOUT_SECONDS = msg("tui.mcp.import.timeout.seconds", fallback="{seconds} s")
_TIMEOUT_CONVERTED = msg(
    "tui.mcp.import.timeout.converted",
    fallback="{seconds} s (source: {milliseconds} ms)",
)
_TIMEOUT_CONVERTED_SECONDS = msg(
    "tui.mcp.import.timeout.converted_seconds",
    fallback="{seconds} s (source: {source_seconds} s)",
)
_TIMEOUT_UNLIMITED = msg("tui.mcp.import.timeout.unlimited", fallback="{seconds} s (source: no timeout limit)")
_TIMEOUT_DEFAULT = msg("tui.mcp.import.timeout.default", fallback="Use {app} default")
_AVAILABLE_TOOL_SCOPE = msg("tui.mcp.import.field.available_tool_scope", fallback="Available Tool Scope")
_ALL_TOOLS = msg("tui.mcp.import.field.all_tools", fallback="All server tools")
_SELECTED_TOOLS = msg("tui.mcp.import.field.selected_tools", fallback="Only these tools: {tools}")

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
    fallback=(
        "Can't import: the URL uses an environment variable, which {app} doesn't resolve in URLs. "
        "Add the server manually using an explicit URL."
    ),
)
_TEMPLATE_IN_COMMAND = msg(
    "tui.mcp.import.unsupported.template_in_command",
    fallback=(
        "Can't import: the command uses an environment variable, which {app} doesn't resolve in commands. "
        "Add the server manually using an explicit command."
    ),
)
_TEMPLATE_IN_FIELD_NAME = msg(
    "tui.mcp.import.unsupported.template_in_field_name",
    fallback=(
        "Can't import: {app} doesn't support dynamic references in server names or configuration field names. "
        "Use fixed names in the source configuration, then reopen import, or add the server manually with fixed names."
    ),
)
_TEMPLATE_NEXT_TO_BRACE = msg(
    "tui.mcp.import.unsupported.template_next_to_brace",
    fallback=(
        "Can't import: an environment variable or header value has a brace right next to a variable, "
        "which {app} can't write the same way. Add the server manually and enter this value using "
        "supported variable syntax or a literal value."
    ),
)
_FILE_REFERENCE = msg(
    "tui.mcp.import.unsupported.file_reference",
    fallback=(
        "Can't import: a setting takes its value from a file, which {app} doesn't read. "
        "Add the server manually. Enter command and URL values explicitly; for environment variables and headers, "
        "supply the value directly or use a supported variable or credential command."
    ),
)
_HEADER_VALUE_ENCODING = msg(
    "tui.mcp.import.unsupported.header_value_encoding",
    fallback=(
        "Can't import: {app} can't preserve the transmission of non-ASCII text in this HTTP header value. "
        "Use an ASCII representation accepted by the server in the source configuration and reopen import, "
        "or continue using the source application."
    ),
)
_VALUE_FROM_COMMAND = msg(
    "tui.mcp.import.unsupported.value_from_command",
    fallback=(
        "Can't import: a command supplies an environment variable or header value, and {app} runs credential "
        "commands differently. Add the server manually and configure {template} for the value. "
        "Review the command's shell, working folder, output and refresh behavior before testing."
    ),
)
_VARIABLE_NAME = msg(
    "tui.mcp.import.unsupported.variable_name",
    fallback=(
        "Can't import: a setting uses an environment variable whose name {app} can't write. "
        "Add the server manually using supported environment variable names."
    ),
)
_UNSUPPORTED_SETTING = msg("tui.mcp.import.unsupported.setting", fallback="Affected setting: {field}. {reason}")
_VARIABLE_NAME_DETAILS = msg(
    "tui.mcp.import.unsupported.variable_name_details",
    fallback=(
        "Can't import: {field} references the unsupported environment variable name {variable}. "
        "Use a name beginning with an ASCII letter or underscore, followed by ASCII letters, digits or underscores. "
        "Update the name in the source configuration and environment, then reopen import."
    ),
)
_HEADER_NAME_COLLISION = msg(
    "tui.mcp.import.unsupported.header_name_collision",
    fallback=(
        "Can't import: these headers have conflicting entries whose names differ only in case: {fields}. "
        "Keep one entry for each header in the source configuration, then reopen import."
    ),
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
        "The headers helper is not imported: it returns a set of headers, while {app} credential commands "
        "supply one value. After import, open this server's settings and add the required Headers. "
        "For a value supplied by a command, configure {template} manually and review its working folder "
        "and refresh behavior."
    ),
)
_OAUTH_IGNORED = msg(
    "tui.mcp.import.warning.oauth_ignored",
    fallback=(
        "OAuth settings and sign-in state are not imported. {app} does not manage the OAuth login or token "
        "refresh flow. If the server accepts a manually supplied valid token or supports another "
        "authentication method, configure authentication headers as it requires."
    ),
)
_CODEX_OAUTH_IGNORED = msg(
    "tui.mcp.import.warning.codex_oauth_ignored",
    fallback=(
        "If this server relies on OAuth sign-in in Codex, its sign-in state and OAuth settings will not transfer. "
        "{app} does not manage OAuth login or token refresh. After import, open the server's Headers and "
        "provide a valid token or other authentication headers as the server requires."
    ),
)
_CREDENTIAL_VARIABLE = msg(
    "tui.mcp.import.warning.credential_variable",
    fallback=(
        "{field} references {variable}, which {agent} replaces with an empty string for remote servers. "
        "{app} replaces that variable reference with an empty string too; other text in the value is kept. "
        "A header is omitted only if this leaves its entire value empty after trimming. To send this credential, copy it "
        "into a variable with another name and reference that."
    ),
)
_CLAUDE_PROJECT_DIR = msg(
    "tui.mcp.import.warning.claude_project_dir",
    fallback=(
        "{field} references {variable}, which {agent} sets for the servers it starts but doesn't fill in here: "
        "the server gets the reference as written, or its default. {app} doesn't set {variable} and fails the "
        "connection unless you set it before starting {app}. Write the folder path instead."
    ),
)
_CWD_IGNORED = msg(
    "tui.mcp.import.warning.cwd_ignored",
    fallback=(
        "{agent} starts this server in {folder}. {app} starts it in the working folder, "
        "so a server that needs the original folder may not work. Start {app} from that folder, "
        "or adjust the server's relative paths before testing."
    ),
)
_RELATIVE_CWD_IGNORED = msg(
    "tui.mcp.import.warning.relative_cwd_ignored",
    fallback=(
        "{agent} resolves this server's working folder {folder} relative to {base}. "
        "{app} does not import this working-folder setting. Before testing, start {app} from the resolved "
        "folder or adjust the server's relative paths."
    ),
)
_MERGED = msg(
    "tui.mcp.import.warning.merged",
    fallback=(
        "{agent} combines this server's settings from multiple configuration sources. Additional sources: {files}."
    ),
)
_MERGED_VALUES = msg(
    "tui.mcp.import.warning.merged.values",
    fallback=(
        "{agent} combines this server's settings from multiple configuration sources. Additional sources: {files}. "
        "Settings inherited from another source: {fields}. These settings are also passed to this server."
    ),
)
_AUTH_PROVIDER_IGNORED = msg(
    "tui.mcp.import.warning.auth_provider_ignored",
    fallback=(
        "The {provider} authentication settings from {agent} are not imported. {app} will not obtain or refresh "
        "these credentials automatically. After import, open this server's Headers and provide a valid token "
        "or other authentication headers as the server requires. An external credential command can supply "
        "a token using {template}."
    ),
)
_TOOL_RULES_IGNORED = msg(
    "tui.mcp.import.warning.tool_rules_ignored",
    fallback=(
        "These tool rules from {agent} were not imported: {rules}. These tools will follow "
        "the server's overall settings in {app}, so their availability or loading behavior may differ. "
        "Check Available Tool Scope and, if shown, Loading Strategy in the preview. After import, "
        "you can adjust them in this server's settings."
    ),
)
_TOOL_PERMISSIONS_IGNORED = msg(
    "tui.mcp.import.warning.tool_permissions_ignored",
    fallback=(
        "Tool permissions from {agent} are not imported ({fields}). "
        "After import, check Available Tool Scope in the server's settings and the agent profile's approval rules."
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
        "imported with no tools. After import, open this server's settings and select the tools you want "
        "under Available Tool Scope."
    ),
)
_UNKNOWN_FIELDS_IGNORED = msg(
    "tui.mcp.import.warning.unknown_fields_ignored",
    fallback="These settings are not imported: {fields}.",
)
_INVALID_HEADER_NAME_IGNORED = msg(
    "tui.mcp.import.warning.invalid_header_name_ignored",
    fallback=(
        "{field} has an invalid HTTP header name. {agent} skips it, and it is not imported. "
        "If this header is needed, correct its name in the source configuration and reopen import."
    ),
)
_INVALID_HEADER_VALUE_IGNORED = msg(
    "tui.mcp.import.warning.invalid_header_value_ignored",
    fallback=(
        "{field} has an invalid HTTP header value. {agent} skips it, and it is not imported. "
        "If this header is needed, remove line breaks or control characters from its source value and reopen import."
    ),
)
_ZERO_TIMEOUT_IGNORED = msg(
    "tui.mcp.import.warning.zero_timeout_ignored",
    fallback=(
        "The source timeout of 0 cannot be preserved. No explicit timeout is imported; "
        "{app} will use its default request timeout. After import, set MCP Request Timeout if needed."
    ),
)
_TOOL_OUTPUT_LIMIT_IGNORED = msg(
    "tui.mcp.import.warning.tool_output_limit_ignored",
    fallback=(
        "These per-tool output limits are not imported: {fields}. "
        "After import, review the server-wide max_tool_result_tokens setting in the agent profile if needed."
    ),
)
_LITERAL_BRACES_ESCAPED = msg(
    "tui.mcp.import.warning.literal_braces_escaped",
    fallback="Literal braces are escaped using {app} syntax; the text received by the server is unchanged.",
)
_WARNING_FIELDS = msg("tui.mcp.import.warning.fields", fallback="Affected fields:")
_WARNING_RESULT = msg("tui.mcp.import.warning.result", fallback="Imported value:")
_WARNING_EFFECT = msg("tui.mcp.import.warning.effect", fallback="Impact:")
_WARNING_ACTION = msg("tui.mcp.import.warning.action", fallback="Action:")
_WARNING_NOTE = msg("tui.mcp.import.warning.note", fallback="Note:")
_PROJECT_DIR_TITLE = msg("tui.mcp.import.warning.project_dir.title", fallback="Configure the project folder manually")
_PROJECT_DIR_EFFECT = msg(
    "tui.mcp.import.warning.project_dir.effect", fallback="{app} does not set {variable} automatically."
)
_PROJECT_DIR_ACTION = msg(
    "tui.mcp.import.warning.project_dir.action",
    fallback=("Enter the required folder path in these fields, or set {variable} before starting {app}."),
)
_DEFAULT_TITLE = msg("tui.mcp.import.warning.default.title", fallback="Variable defaults will not be kept")
_DEFAULT_ACTION = msg(
    "tui.mcp.import.warning.default.action",
    fallback=("Set {variable} before starting {app}, or enter a fixed value in these fields."),
)
_DEFAULT_NOTE = msg(
    "tui.mcp.import.warning.default.note", fallback="The variable default in these fields will not be kept: {fields}."
)
_CREDENTIAL_TITLE = msg(
    "tui.mcp.import.warning.credential.title", fallback="Credential variable references will be removed"
)
_CREDENTIAL_ACTION = msg(
    "tui.mcp.import.warning.credential.action",
    fallback="To send this credential, reference another variable containing it.",
)
_CREDENTIAL_EFFECT = msg(
    "tui.mcp.import.warning.credential.effect",
    fallback=(
        "References to {variable} are replaced with an empty string; other text is kept. "
        "A header left entirely empty after trimming is omitted."
    ),
)
_UNREADABLE = msg(
    "tui.mcp.import.source_error.unreadable",
    fallback="Couldn't read {path}. Check file permissions, fix the problem, then reopen import.",
)
_TOO_LARGE = msg(
    "tui.mcp.import.source_error.too_large",
    fallback="{path} exceeds the {limit_mib} MiB file size limit. Reduce its size, then reopen import.",
)
_INVALID_JSON = msg(
    "tui.mcp.import.source_error.invalid_json", fallback="{path} is not valid JSON. Fix the file, then reopen import."
)
_INVALID_JSON_LOCATION = msg(
    "tui.mcp.import.source_error.invalid_json_location",
    fallback="Couldn't parse {path} as JSON at line {line}, column {column}. Fix the file, then reopen import.",
)
_INVALID_TOML = msg(
    "tui.mcp.import.source_error.invalid_toml",
    fallback="Couldn't parse {path} as TOML. Check its syntax and file encoding, then reopen import.",
)
_INVALID_TOML_LOCATION = msg(
    "tui.mcp.import.source_error.invalid_toml_location",
    fallback="Couldn't parse {path} as TOML at line {line}, column {column}. Fix the file, then reopen import.",
)
_INVALID_ENCODING = msg(
    "tui.mcp.import.source_error.invalid_encoding",
    fallback="Couldn't read {path} as UTF-8. Save the file as UTF-8, then reopen import.",
)
_INVALID_CONFIG_FIELD = msg(
    "tui.mcp.import.source_error.invalid_config_field",
    fallback="{field} in {path} must be a TOML table, such as {example}. Fix the file, then reopen import.",
)
_INVALID_CONFIG_SETTING = msg(
    "tui.mcp.import.source_error.invalid_config_setting",
    fallback="The setting {field} in {path} is invalid. Check its type and value, then reopen import.",
)
_INVALID_JSON_OBJECT_FIELD = msg(
    "tui.mcp.import.source_error.invalid_json_object_field",
    fallback="{field} in {path} must be a JSON object. Fix the file, then reopen import.",
)
_INVALID_SERVER_CONFIG = msg(
    "tui.mcp.import.source_error.invalid_server_config",
    fallback=(
        "The MCP server settings at {field} in {path} are invalid. Check that the type is local or remote "
        "and that its connection settings match, then reopen import."
    ),
)
_NOT_AN_OBJECT = msg(
    "tui.mcp.import.source_error.not_an_object",
    fallback="{path} must contain a JSON object at the top level. Fix the file, then reopen import.",
)

_SCOPE_LABELS = {"user": _SCOPE_USER, "local": _SCOPE_LOCAL, "project": _SCOPE_PROJECT, "global": _SCOPE_GLOBAL}
_UNSUPPORTED_REASONS = {
    UnsupportedReason.REMOTE_ENVIRONMENT: _REMOTE_ENVIRONMENT,
    UnsupportedReason.UNSUPPORTED_TRANSPORT: _UNSUPPORTED_TRANSPORT,
    UnsupportedReason.TEMPLATE_IN_URL: _TEMPLATE_IN_URL,
    UnsupportedReason.TEMPLATE_IN_COMMAND: _TEMPLATE_IN_COMMAND,
    UnsupportedReason.TEMPLATE_IN_FIELD_NAME: _TEMPLATE_IN_FIELD_NAME,
    UnsupportedReason.TEMPLATE_NEXT_TO_BRACE: _TEMPLATE_NEXT_TO_BRACE,
    UnsupportedReason.FILE_REFERENCE: _FILE_REFERENCE,
    UnsupportedReason.HEADER_VALUE_ENCODING: _HEADER_VALUE_ENCODING,
    UnsupportedReason.VALUE_FROM_COMMAND: _VALUE_FROM_COMMAND,
    UnsupportedReason.VARIABLE_NAME: _VARIABLE_NAME,
}
_INVALID_CONFIG = msg(
    "tui.mcp.import.source_error.invalid_config",
    fallback="{path} has settings {agent} rejects, so its servers are not listed. Fix the file, then reopen import.",
)
_SOURCE_ERRORS = {
    SourceErrorKind.INVALID_TOML: _INVALID_TOML,
    SourceErrorKind.UNREADABLE: _UNREADABLE,
    SourceErrorKind.TOO_LARGE: _TOO_LARGE,
    SourceErrorKind.INVALID_JSON: _INVALID_JSON,
    SourceErrorKind.NOT_AN_OBJECT: _NOT_AN_OBJECT,
    SourceErrorKind.INVALID_ENCODING: _INVALID_ENCODING,
}


type _CandidateState = Literal["importable", "unsupported", "added"]

_STATE_ORDER: dict[_CandidateState, int] = {"importable": 0, "unsupported": 1, "added": 2}


def _source_error(error: McpSourceError, agent: str) -> MessageRef:
    path = sanitize_legacy_scalar(error.path)
    if error.kind is SourceErrorKind.INVALID_JSON and error.line is not None and error.column is not None:
        return _INVALID_JSON_LOCATION.bind(path=path, line=error.line, column=error.column)
    if error.kind is SourceErrorKind.INVALID_TOML and error.line is not None and error.column is not None:
        return _INVALID_TOML_LOCATION.bind(path=path, line=error.line, column=error.column)
    if error.kind is SourceErrorKind.INVALID_CONFIG:
        if agent == "pi" and error.field == "mcpServers":
            return _INVALID_JSON_OBJECT_FIELD.bind(path=path, field=error.field)
        if agent == "Codex" and error.field == "mcp_servers":
            return _INVALID_CONFIG_FIELD.bind(
                path=path, field=sanitize_legacy_scalar(error.field), example="[mcp_servers.server_name]"
            )
        if error.field:
            message = (
                _INVALID_SERVER_CONFIG
                if agent == "opencode" and error.field.startswith("mcp.") and error.field != "mcp.timeout"
                else _INVALID_CONFIG_SETTING
            )
            return message.bind(path=path, field=sanitize_legacy_scalar(error.field))
        return _INVALID_CONFIG.bind(path=path, agent=agent)
    if error.kind is SourceErrorKind.TOO_LARGE:
        return _TOO_LARGE.bind(path=path, limit_mib=MAX_SOURCE_BYTES // (1024 * 1024))
    return _SOURCE_ERRORS[error.kind].bind(path=path)


def is_already_added(config: MCPServerConfig, existing: Sequence[MCPServerConfig]) -> bool:
    """Whether *existing* already has the same server settings, apart from its label and tool prefix."""
    return any(_connection_key(config) == _connection_key(server) for server in existing)


def _key_value_pairs(values: Mapping[Any, Any]) -> tuple[tuple[str, str], ...]:
    return tuple(
        (sanitize_legacy_scalar(str(key)), sanitize_legacy_scalar(str(value))) for key, value in values.items()
    )


def _connection_key(config: MCPServerConfig) -> str:
    # Credentials, environment and tool scope can distinguish servers sharing one endpoint.
    values = asdict(config)
    for key in ("name", "description", "tool_name_prefix"):
        del values[key]
    return json.dumps(values, sort_keys=True)


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


class _PairGrid(Grid):
    """Names and values in two equal columns, one line per pair, as the agent's MCP card shows them."""

    def __init__(self, pairs: Sequence[tuple[str, str]]) -> None:
        super().__init__(classes="mcp-import-pairs")
        self._pairs = pairs

    def compose(self) -> ComposeResult:
        for key, value in self._pairs:
            yield Static(Text(key), classes="mcp-import-pair-key")
            yield Static(Text(value), classes="mcp-import-pair-value")


@dataclass(frozen=True, slots=True)
class _CardField:
    """One setting: a single value, or (name, value) pairs such as env or headers.

    A ``single_line`` value is cut to one line, as the agent's MCP card shows it in a one-line input.
    """

    label: str
    value: str = ""
    pairs: tuple[tuple[str, str], ...] = ()
    single_line: bool = False


@dataclass(frozen=True, slots=True)
class _WarningGroup:
    text: str
    fields: tuple[_CardField, ...] = ()


def _field_widgets(field: _CardField) -> ComposeResult:
    yield Label(Content.from_text(field.label, markup=False), classes="mcp-import-field-label")
    if field.pairs:
        yield _PairGrid(field.pairs)
    else:
        classes = "mcp-import-field-value" + (" -single-line" if field.single_line else "")
        yield Static(Text(field.value), classes=classes)


class _ImportCard(CollapsibleConfigCard):
    """A server's settings, read-only, under a header that folds and ticks it, as on the agent's MCP card.

    The header holds only the name. The transport, the source description and
    the server's scope in the source come first, then the connection settings
    in a titled section and, below them as on the agent's MCP card, the tool
    name prefix the import gives the server. An importable server is locked
    while a ticked one with the same connection covers it; the covered note
    says which.
    """

    class Folded(Message):
        """Posted when the user folds or unfolds a card, so the pane can rebuild it the same way."""

        def __init__(self, source_id: str, index: int, collapsed: bool) -> None:
            self.source_id = source_id
            self.index = index
            self.collapsed = collapsed
            super().__init__()

    def __init__(
        self,
        *,
        index: int,
        row: _ImportRow,
        importable: bool,
        summary_fields: Sequence[_CardField],
        settings_title: str,
        settings_fields: Sequence[_CardField],
        naming_fields: Sequence[_CardField],
        notes: Sequence[str],
        warnings: Sequence[_WarningGroup],
        covered: str | None = None,
        collapsed: bool = False,
    ) -> None:
        self._row = row
        self._importable = importable
        self._summary_fields = list(summary_fields)
        self._settings_title = settings_title
        self._settings_fields = list(settings_fields)
        self._naming_fields = list(naming_fields)
        self._notes = list(notes)
        self._warnings = list(warnings)
        self._covered = covered
        self._covered_note = Static(Text(covered or ""), classes="mcp-import-note")
        self._covered_note.display = covered is not None
        classes = "mcp-import-card" + (" -unavailable" if row.disabled else "")
        super().__init__(index, collapsed=collapsed, classes=classes, read_only=True)

    @property
    def row(self) -> _ImportRow:
        return self._row

    @property
    def importable(self) -> bool:
        """Whether the server can be imported at all, covered or not."""
        return self._importable

    def show_covered(self, note: str | None) -> None:
        """Lock the card with *note* while a ticked server covers it; unlock it when *note* is None."""
        # Every tick re-checks every card: one whose note is unchanged keeps its layout.
        if not self._importable or note == self._covered:
            return
        self._covered = note
        covered = note is not None
        self._row.disabled = covered
        self.set_class(covered, "-unavailable")
        self._covered_note.update(Text(note or ""))
        self._covered_note.display = covered

    def set_collapsed(self, collapsed: bool, *, scroll_visible: bool = True) -> None:
        changed = collapsed != self.collapsed
        super().set_collapsed(collapsed, scroll_visible=scroll_visible)
        if changed:
            self.post_message(self.Folded(self._row.source_id, self._row.index, collapsed))

    def compose(self) -> ComposeResult:
        with Horizontal(classes="mcp-import-card-header"):
            # The fold toggle alone: the checkbox beside it carries the name, and a click on the name ticks.
            yield self._make_title("", classes="mcp-import-fold")
            # The rest of the header, so a long name is cut where the card ends, not past it.
            with Horizontal(classes="mcp-import-card-name"):
                yield self._row
        with self.card_body():
            for field in self._summary_fields:
                yield from _field_widgets(field)
            if self._settings_fields:
                with Vertical(classes="mcp-import-settings") as section:
                    if self._settings_title:
                        section.border_title = Text(self._settings_title)
                    for field in self._settings_fields:
                        yield from _field_widgets(field)
            for field in self._naming_fields:
                yield from _field_widgets(field)
            for note in self._notes:
                yield Static(Text(note), classes="mcp-import-note")
            yield self._covered_note
            for warning in self._warnings:
                with Vertical(classes="mcp-import-warning-group"):
                    title_classes = "mcp-import-warning" + (" mcp-import-warning-title" if warning.fields else "")
                    yield Static(Text(f"⚠ {warning.text}"), classes=title_classes)
                    if warning.fields:
                        with Grid(classes="mcp-import-warning-fields"):
                            for field in warning.fields:
                                yield Static(Text(field.label), classes="mcp-import-warning-label")
                                yield Static(Text(field.value), classes="mcp-import-warning-value")


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
        # Cards the user folded or unfolded, so a pane rebuild (agent or locale switch) keeps them that way.
        self._folded: dict[str, dict[int, bool]] = {}
        self._shown_source: str | None = None
        # The message the pane shows instead of servers, and whether it is an error, re-rendered when the locale changes.
        self._pane_message: MessageRef | None = None
        self._pane_message_is_error = False
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
                self._show_message_safely(self._pane_message, self._generation, error=self._pane_message_is_error),
                group="mcp-import-pane",
                exit_on_error=False,
            )

    async def _detect(self) -> None:
        # The worker doesn't exit the app on an error and nothing awaits it: say so here, or the pane spins forever.
        try:
            await self._list_sources()
        except Exception:
            logger.exception("Listing agents to import MCP servers from failed")
            await self._show_message_safely(_DETECT_FAILED.bind(), self._generation, error=True)

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
        options: list[Option | None] = []
        for position, source in enumerate(sources):
            # A rule between agents, as between the models of the model list; the cursor skips it.
            if position:
                options.append(None)
            options.append(
                Option(Content.styled(sanitize_legacy_scalar(source.display_name), "bold"), id=source.source_id)
            )
        option_list.add_options(options)
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
            await self._show_message_safely(_READ_FAILED.bind(agent=source.display_name), generation, error=True)

    async def _show_message(self, reference: MessageRef, generation: int, *, error: bool = False) -> None:
        """Show *reference* instead of servers; an *error* is red, as a file that can't be read."""
        classes = "mcp-import-error" if error else "mcp-import-message"
        async with self._pane_lock:
            if generation == self._generation:
                await self._mount_pane([Static(Text(self._render_message(reference)), classes=classes)])
                self._pane_message = reference
                self._pane_message_is_error = error

    async def _show_message_safely(self, reference: MessageRef, generation: int, *, error: bool = False) -> None:
        """Show *reference*, logging a failure: nothing awaits these workers, and the dialog may be closing."""
        try:
            await self._show_message(reference, generation, error=error)
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
            return [Static(Text(self._render_message(_READ_FAILED.bind(agent=agent))), classes="mcp-import-error")]
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
            # A server that can't be ticked here opens folded to its header; one covered by a tick stays open,
            # as its note says why it was just locked.
            collapsed = self._folded.get(source.source_id, {}).get(index, state != "importable")
            widgets.append(
                _ImportCard(
                    index=index,
                    row=row,
                    importable=state == "importable",
                    covered=covered,
                    summary_fields=self._summary_fields(candidate, agent),
                    settings_title=self._settings_title(candidate),
                    settings_fields=self._settings_fields(candidate, show_loading=source.source_id == "pi"),
                    # Only a server that will be imported gets one.
                    naming_fields=self._naming_fields(candidate) if state == "importable" else [],
                    collapsed=collapsed,
                    notes=[] if note is None else [note],
                    # What the conversion changed only matters for a server that will be imported.
                    warnings=[] if state == "added" else self._warning_groups(candidate.warnings, agent),
                )
            )
        # A file that couldn't be read already says why nothing is listed.
        if not result.candidates and not result.source_errors:
            # The highlighted agent in the sidebar names whose servers these are, as the agent's MCP tab says it.
            widgets.append(HatchedEmptyState(self._render_message(_EMPTY_SOURCE.bind()), classes="mcp-import-empty"))
            widgets.append(
                Static(Text(self._render_message(_EMPTY_SOURCE_ACTION.bind())), classes="mcp-import-message")
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
        if candidate.unsupported is UnsupportedReason.VARIABLE_NAME and candidate.unsupported_field:
            return self._render_message(
                _VARIABLE_NAME_DETAILS.bind(
                    field=sanitize_legacy_scalar(candidate.unsupported_field),
                    variable=sanitize_legacy_scalar(candidate.unsupported_variable),
                )
            )
        if candidate.unsupported is UnsupportedReason.HEADER_NAME_COLLISION:
            return self._render_message(
                _HEADER_NAME_COLLISION.bind(fields=sanitize_legacy_scalar(candidate.unsupported_field))
            )
        reason = self._render_message(
            _VALUE_FROM_COMMAND.bind(app=APP_DISPLAY_NAME, template="{{!command}}")
            if candidate.unsupported is UnsupportedReason.VALUE_FROM_COMMAND
            else _UNSUPPORTED_REASONS[candidate.unsupported].bind(app=APP_DISPLAY_NAME)
        )
        if candidate.unsupported_field:
            return self._render_message(
                _UNSUPPORTED_SETTING.bind(field=sanitize_legacy_scalar(candidate.unsupported_field), reason=reason)
            )
        return reason

    def _summary_fields(self, candidate: McpImportCandidate, agent: str) -> list[_CardField]:
        """The transport and description, in the agent's MCP card order, then the server's scope in the source."""
        fields: list[_CardField] = []
        # An unsupported transport shows too: it is why the server can't be imported.
        if candidate.transport:
            fields.append(
                _CardField(
                    self._render_message(MCP_TRANSPORT_LABEL.bind()),
                    sanitize_legacy_scalar(candidate.transport.upper()),
                    single_line=True,
                )
            )
        if candidate.config is not None and candidate.config.description.strip():
            fields.append(
                _CardField(
                    self._render_message(DESCRIPTION.bind()), sanitize_legacy_block(candidate.config.description)
                )
            )
        # The source's own word for the scope, then where it applies; a long folder wraps.
        scope = self._scope_label(candidate.scope)
        where = (
            _SCOPE_ONLY_IN.bind(scope=scope, path=sanitize_legacy_scalar(candidate.scope_path))
            if candidate.scope_path
            else _SCOPE_EVERY_PROJECT.bind(scope=scope)
        )
        fields.append(_CardField(self._render_message(_SCOPE.bind(agent=agent)), self._render_message(where)))
        if any(warning.kind is ImportWarningKind.MERGED for warning in candidate.warnings):
            fields.append(
                _CardField(
                    self._render_message(_CONFIGURATION_SOURCE.bind()), sanitize_legacy_scalar(candidate.source_path)
                )
            )
        if candidate.config is not None and not candidate.config.enabled:
            fields.append(
                _CardField(self._render_message(MCP_ENABLED.bind()), self._render_message(_NOT_ENABLED.bind()))
            )
        return fields

    def _settings_title(self, candidate: McpImportCandidate) -> str:
        """The settings section's title, as the agent's MCP card names it; none when the source names no transport."""
        if not candidate.transport:
            return ""
        transport = sanitize_legacy_scalar(candidate.transport.upper())
        return self._render_message(_TRANSPORT_OPTIONS.bind(transport=transport))

    def _settings_fields(self, candidate: McpImportCandidate, *, show_loading: bool = False) -> list[_CardField]:
        """The settings the source gave: the converted config, or as written when it can't be imported."""
        config = candidate.config
        if config is not None:
            if config.transport == "http":
                fields = self._fields(url=config.url, headers=config.headers)
            else:
                fields = self._fields(command=format_mcp_command_line(config.command, config.args), env=config.env)
            if config.request_timeout is not None:
                timeout = _TIMEOUT_SECONDS.bind(seconds=config.request_timeout)
                if (
                    candidate.timeout_source_ms is not None
                    and candidate.timeout_source_ms != config.request_timeout * 1000
                ):
                    timeout = _TIMEOUT_CONVERTED.bind(
                        seconds=config.request_timeout, milliseconds=candidate.timeout_source_ms
                    )
                elif (
                    candidate.timeout_source_seconds is not None
                    and candidate.timeout_source_seconds != config.request_timeout
                ):
                    timeout = (
                        _TIMEOUT_UNLIMITED.bind(seconds=config.request_timeout)
                        if isinstance(candidate.timeout_source_seconds, float)
                        and not math.isfinite(candidate.timeout_source_seconds)
                        else _TIMEOUT_CONVERTED_SECONDS.bind(
                            seconds=config.request_timeout, source_seconds=str(candidate.timeout_source_seconds)
                        )
                    )
                fields.append(_CardField(self._render_message(_REQUEST_TIMEOUT.bind()), self._render_message(timeout)))
            elif any(warning.kind is ImportWarningKind.ZERO_TIMEOUT_IGNORED for warning in candidate.warnings):
                fields.append(
                    _CardField(
                        self._render_message(_REQUEST_TIMEOUT.bind()),
                        self._render_message(_TIMEOUT_DEFAULT.bind(app=APP_DISPLAY_NAME)),
                    )
                )
            if config.allowed_tools is None:
                tool_scope = _ALL_TOOLS.bind()
            elif not config.allowed_tools:
                tool_scope = MCP_TOOL_ACCESS_NONE.bind()
            else:
                tool_scope = _SELECTED_TOOLS.bind(
                    tools=", ".join(sanitize_legacy_scalar(tool) for tool in config.allowed_tools)
                )
            fields.append(
                _CardField(self._render_message(_AVAILABLE_TOOL_SCOPE.bind()), self._render_message(tool_scope))
            )
            if show_loading and config.allowed_tools != []:
                strategy = (
                    _TOOL_LOADING_PROGRESSIVE_LABEL if config.use_progressive_disclosure else _TOOL_LOADING_FULL_LABEL
                )
                fields.append(
                    _CardField(self._render_message(_LOADING_STRATEGY.bind()), self._render_message(strategy.bind()))
                )
                if config.use_progressive_disclosure:
                    initially_visible = (
                        ", ".join(sanitize_legacy_scalar(tool) for tool in config.always_load)
                        if config.always_load
                        else self._render_message(MCP_TOOL_ACCESS_NONE.bind())
                    )
                    fields.append(
                        _CardField(self._render_message(MCP_INITIALLY_VISIBLE_TOOLS.bind()), initially_visible)
                    )
            return fields
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

    def _naming_fields(self, candidate: McpImportCandidate) -> list[_CardField]:
        """The tool name prefix the import gives the server; none when it gets none."""
        if candidate.config is None:
            return []
        prefix = with_tool_name_prefix(candidate.config).tool_name_prefix
        if not prefix:
            return []
        label = self._render_message(MCP_TOOL_NAME_PREFIX.bind())
        return [_CardField(label, sanitize_legacy_scalar(prefix), single_line=True)]

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
            fields.append(_CardField(self._render_message(MCP_COMMAND.bind()), sanitize_legacy_scalar(command)))
        if url:
            fields.append(
                _CardField(self._render_message(MCP_URL.bind()), sanitize_legacy_scalar(url), single_line=True)
            )
        for label, values in ((MCP_ENVIRONMENT_VARIABLES, env), (MCP_HEADERS, headers)):
            if isinstance(values, Mapping) and values:
                fields.append(_CardField(self._render_message(label.bind()), pairs=_key_value_pairs(values)))
        return fields

    def _scope_label(self, scope: str) -> str:
        label = _SCOPE_LABELS.get(scope)
        return sanitize_legacy_scalar(scope) if label is None else self._render_message(label.bind())

    def _warning_groups(self, warnings: Sequence[ImportWarning], agent: str) -> list[_WarningGroup]:
        """Group display notices without changing the full warnings saved in descriptions."""
        project_variables = {
            warning.variable for warning in warnings if warning.kind is ImportWarningKind.CLAUDE_PROJECT_DIR
        }
        grouped: dict[tuple[ImportWarningKind, str], list[ImportWarning]] = {}
        variable_kinds = {
            ImportWarningKind.CLAUDE_PROJECT_DIR,
            ImportWarningKind.DEFAULT_DROPPED,
            ImportWarningKind.CREDENTIAL_VARIABLE,
        }
        for warning in warnings:
            kind = warning.kind
            if kind is ImportWarningKind.DEFAULT_DROPPED and warning.variable in project_variables:
                kind = ImportWarningKind.CLAUDE_PROJECT_DIR
            key = warning.variable if kind in variable_kinds else self._warning_text(warning, agent)
            grouped.setdefault((kind, key), []).append(warning)

        groups: list[_WarningGroup] = []
        for (kind, key), items in grouped.items():
            if kind not in variable_kinds:
                groups.append(_WarningGroup(key))
                continue
            variable = sanitize_legacy_scalar(key)
            fields = ", ".join(
                dict.fromkeys(
                    sanitize_legacy_scalar(item.field) for item in sorted(items, key=lambda item: item.kind is not kind)
                )
            )
            rows = [_CardField(self._render_message(_WARNING_FIELDS.bind()), fields)]
            match kind:
                case ImportWarningKind.CLAUDE_PROJECT_DIR:
                    title = _PROJECT_DIR_TITLE.bind()
                    action = _PROJECT_DIR_ACTION.bind(app=APP_DISPLAY_NAME, variable=variable)
                    rows.append(
                        _CardField(
                            self._render_message(_WARNING_EFFECT.bind()),
                            self._render_message(_PROJECT_DIR_EFFECT.bind(app=APP_DISPLAY_NAME, variable=variable)),
                        )
                    )
                case ImportWarningKind.DEFAULT_DROPPED:
                    title = _DEFAULT_TITLE.bind()
                    action = _DEFAULT_ACTION.bind(app=APP_DISPLAY_NAME, variable=variable)
                    rows.append(_CardField(self._render_message(_WARNING_RESULT.bind()), "{{" + variable + "}}"))
                case ImportWarningKind.CREDENTIAL_VARIABLE:
                    title = _CREDENTIAL_TITLE.bind()
                    action = _CREDENTIAL_ACTION.bind()
                    rows.append(
                        _CardField(
                            self._render_message(_WARNING_EFFECT.bind()),
                            self._render_message(_CREDENTIAL_EFFECT.bind(variable=variable)),
                        )
                    )
                case _:
                    raise ValueError(f"Unsupported variable warning group: {kind}")
            rows.append(_CardField(self._render_message(_WARNING_ACTION.bind()), self._render_message(action)))
            defaults = ", ".join(
                dict.fromkeys(
                    sanitize_legacy_scalar(item.field)
                    for item in items
                    if item.kind is ImportWarningKind.DEFAULT_DROPPED
                )
            )
            if kind is ImportWarningKind.CLAUDE_PROJECT_DIR and defaults:
                rows.append(
                    _CardField(
                        self._render_message(_WARNING_NOTE.bind()),
                        self._render_message(_DEFAULT_NOTE.bind(fields=defaults)),
                    )
                )
            groups.append(_WarningGroup(self._render_message(title), tuple(rows)))
        return groups

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
                reference = _HEADERS_HELPER_IGNORED.bind(app=APP_DISPLAY_NAME, template="{{!command}}")
            case ImportWarningKind.OAUTH_IGNORED:
                reference = (_CODEX_OAUTH_IGNORED if agent == "Codex" else _OAUTH_IGNORED).bind(app=APP_DISPLAY_NAME)
            case ImportWarningKind.CREDENTIAL_VARIABLE:
                reference = _CREDENTIAL_VARIABLE.bind(field=field, variable=variable, agent=agent, app=APP_DISPLAY_NAME)
            case ImportWarningKind.CLAUDE_PROJECT_DIR:
                reference = _CLAUDE_PROJECT_DIR.bind(field=field, variable=variable, agent=agent, app=APP_DISPLAY_NAME)
            case ImportWarningKind.UNKNOWN_FIELDS_IGNORED:
                reference = _UNKNOWN_FIELDS_IGNORED.bind(fields=original)
            case ImportWarningKind.CWD_IGNORED:
                reference = (
                    _RELATIVE_CWD_IGNORED.bind(agent=agent, folder=original, base=field, app=APP_DISPLAY_NAME)
                    if field
                    else _CWD_IGNORED.bind(agent=agent, folder=original, app=APP_DISPLAY_NAME)
                )
            case ImportWarningKind.MERGED:
                reference = (
                    _MERGED_VALUES.bind(agent=agent, files=original, fields=field)
                    if field
                    else _MERGED.bind(agent=agent, files=original)
                )
            case ImportWarningKind.AUTH_PROVIDER_IGNORED:
                reference = _AUTH_PROVIDER_IGNORED.bind(
                    agent=agent, provider=original, app=APP_DISPLAY_NAME, template="{{!command}}"
                )
            case ImportWarningKind.TOOL_RULES_IGNORED:
                reference = _TOOL_RULES_IGNORED.bind(agent=agent, rules=original, app=APP_DISPLAY_NAME)
            case ImportWarningKind.TOOL_PERMISSIONS_IGNORED:
                reference = _TOOL_PERMISSIONS_IGNORED.bind(agent=agent, fields=original)
            case ImportWarningKind.TOOLS_LIMITED:
                reference = (
                    _TOOLS_LIMITED_SOME.bind(agent=agent, rule=field, tools=original)
                    if original
                    else _TOOLS_LIMITED_NONE.bind(agent=agent, rule=field)
                )
            case ImportWarningKind.OPTIONAL_VARIABLE:
                reference = _OPTIONAL_VARIABLE.bind(field=field, variable=variable, agent=agent, app=APP_DISPLAY_NAME)
            case ImportWarningKind.DENY_LIST:
                reference = _DENY_LIST.bind(agent=agent, tools=original, app=APP_DISPLAY_NAME)
            case ImportWarningKind.LITERAL_BRACES_ESCAPED:
                reference = _LITERAL_BRACES_ESCAPED.bind(app=APP_DISPLAY_NAME)
            case ImportWarningKind.INVALID_HEADER_IGNORED:
                message = _INVALID_HEADER_NAME_IGNORED if original == "name" else _INVALID_HEADER_VALUE_IGNORED
                reference = message.bind(field=field, agent=agent)
            case ImportWarningKind.ZERO_TIMEOUT_IGNORED:
                reference = _ZERO_TIMEOUT_IGNORED.bind(app=APP_DISPLAY_NAME)
            case ImportWarningKind.TOOL_OUTPUT_LIMIT_IGNORED:
                reference = _TOOL_OUTPUT_LIMIT_IGNORED.bind(fields=original)
        return self._render_message(reference)

    def _description(self, agent: str, candidate: McpImportCandidate) -> MessageRef:
        scope = self._scope_label(candidate.scope)
        if candidate.scope_path:
            return _DESCRIPTION_SCOPED.bind(agent=agent, scope=scope, path=sanitize_legacy_scalar(candidate.scope_path))
        return _DESCRIPTION.bind(agent=agent, scope=scope)

    @on(_ImportCard.Folded)
    def _on_card_folded(self, event: _ImportCard.Folded) -> None:
        event.stop()
        self._folded.setdefault(event.source_id, {})[event.index] = event.collapsed

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
        taken: set[str] = set()
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
                # Named after its server, as the source names its tools, so two servers' tools don't clash.
                config = with_tool_name_prefix(candidate.config)
                origin = self._render_message(self._description(source.display_name, candidate))
                # The import's warnings stay with the server: the dialog that showed them is about to close.
                notes = [f"- {self._warning_text(warning, source.display_name)}" for warning in candidate.warnings]
                # The source's own description of the server comes first.
                own = [config.description] if config.description else []
                selected.append(replace(config, description="\n".join([*own, origin, *notes])))
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
