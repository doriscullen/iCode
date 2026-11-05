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
from urllib.parse import urlsplit

from rich.cells import cell_len, set_cell_size
from rich.text import Text
from textual import on
from textual.containers import Grid, Horizontal, Vertical, VerticalScroll
from textual.content import Content
from textual.events import Resize
from textual.message import Message
from textual.widgets import Button, Label, OptionList, Static
from textual.widgets.collapsible import CollapsibleTitle
from textual.widgets.option_list import Option

from chrys.app.tui.binding_display import CANCEL_BINDING, localized_binding
from chrys.app.tui.i18n import render_str, widget_localizer
from chrys.app.tui.screens.agents.mcp_picker import mcp_target_summary
from chrys.app.tui.screens.agents.panels.mcp import (
    _LOADING_STRATEGY,
    _NAMING_AND_LIMITS,
    _TOOL_ACCESS,
    _TOOL_LOADING,
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
from chrys.service.mcp.importers.base import MAX_SOURCE_BYTES, ConfigValueKind
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
_UNAVAILABLE = msg("tui.mcp.import.status.unavailable", fallback="Can't import")
_DUPLICATE = msg("tui.mcp.import.status.covered", fallback="Duplicate")
_IMPORTED = msg("tui.mcp.import.status.imported", fallback="Imported")
_NOT_ENABLED = msg("tui.mcp.import.field.enabled.no", fallback="No")
_DETECTING = msg("tui.mcp.import.detecting", fallback="Looking for agents...")
_DETECT_FAILED = msg(
    "tui.mcp.import.detect_failed",
    fallback="Couldn't search for MCP configurations. Reopen import to try again. If it still fails, see the User Guide (F8) to locate iCode's debug.log.",
)
_LOADING = msg("tui.mcp.import.loading", fallback="Reading MCP servers...")
_NO_AGENTS = msg(
    "tui.mcp.import.no_agents",
    fallback="No MCP configuration found for supported applications: {agents}.",
)
_EMPTY_SOURCE = msg("tui.mcp.import.empty_source", fallback="No MCP servers found.")
_READ_FAILED = msg(
    "tui.mcp.import.read_failed",
    fallback="Couldn't load MCP configurations from {agent}. Reopen import to try again. If it still fails, see the User Guide (F8) to locate iCode's debug.log.",
)
_DESCRIPTION = msg(
    "tui.mcp.import.description",
    fallback="Imported from {agent}. There it is a {scope} server, used in every project.",
)
_DESCRIPTION_SCOPED = msg(
    "tui.mcp.import.description.scoped",
    fallback="Imported from {agent}. There it is a {scope} server, used only in {path}.",
)
_SCOPE = msg("tui.mcp.import.field.scope", fallback="Scope in {agent}")
_SCOPE_EVERY_PROJECT = msg("tui.mcp.import.field.scope.every_project", fallback="{scope}, used in every project")
_SCOPE_ONLY_IN = msg("tui.mcp.import.field.scope.only_in", fallback="{scope}, used only in {path}")
_SCOPE_AFTER_IMPORT = msg("tui.mcp.import.field.scope_after_import", fallback="Scope After Import")
_SCOPE_IMPORTED_ALL = msg(
    "tui.mcp.import.field.scope_imported_all", fallback="Available to this agent in every project"
)
_CONFIGURATION_SOURCE = msg("tui.mcp.import.field.configuration_source", fallback="Configuration Source")
_TRANSPORT_OPTIONS = msg("tui.mcp.import.field.transport_options", fallback="{transport} Options")
_SOURCE_CONNECTION = msg("tui.mcp.import.field.source_connection", fallback="Connection Settings in Source")
_ENV_HTTP_HEADERS = msg("tui.mcp.import.field.env_http_headers", fallback="Headers From Environment Variables")
_REQUEST_TIMEOUT = msg("tui.mcp.import.field.request_timeout", fallback="MCP Request Timeout")
_TIMEOUT_SECONDS = msg("tui.mcp.import.timeout.seconds", fallback="{seconds} s")
_TIMEOUT_CONVERTED = msg(
    "tui.mcp.import.timeout.converted",
    fallback=" (source: {milliseconds} ms)",
)
_TIMEOUT_CONVERTED_SECONDS = msg(
    "tui.mcp.import.timeout.converted_seconds",
    fallback=" (source: {source_seconds} s)",
)
_TIMEOUT_UNLIMITED = msg("tui.mcp.import.timeout.unlimited", fallback=" (source: no timeout limit)")
_TIMEOUT_DEFAULT = msg("tui.mcp.import.timeout.default", fallback="Use {app} default")
_AVAILABLE_TOOL_SCOPE = msg("tui.mcp.import.field.available_tool_scope", fallback="Available Tool Scope")
_ALL_TOOLS = msg("tui.mcp.import.field.all_tools", fallback="All server tools")
_SELECTED_TOOLS = msg("tui.mcp.import.field.selected_tools", fallback="Only these tools: {tools}")

_SCOPE_USER = msg("tui.mcp.import.scope.user", fallback="user")
_SCOPE_LOCAL = msg("tui.mcp.import.scope.local", fallback="local")
_SCOPE_PROJECT = msg("tui.mcp.import.scope.project", fallback="project")
_SCOPE_GLOBAL = msg("tui.mcp.import.scope.global", fallback="global")
_TOGGLE_BINDING = msg("tui.binding.toggle_card", fallback="Expand or collapse")
_UNSUPPORTED_TRANSPORT = msg(
    "tui.mcp.import.unsupported.transport",
    fallback="{app} supports only STDIO and HTTP MCP servers. Add the server manually using a STDIO command or HTTP URL that the server provides.",
)
_TEMPLATE_IN_URL = msg(
    "tui.mcp.import.unsupported.template_in_url",
    fallback=(
        "{app} doesn't support environment variable references in URLs. "
        "Add the server manually and enter its full address in URL."
    ),
)
_TEMPLATE_IN_COMMAND = msg(
    "tui.mcp.import.unsupported.template_in_command",
    fallback=(
        "{app} doesn't support environment variable references in commands or arguments. "
        "Add the server manually and replace each reference with the required value in Command."
    ),
)
_TEMPLATE_IN_FIELD_NAME = msg(
    "tui.mcp.import.unsupported.template_in_field_name",
    fallback="{app} requires fixed server names and configuration field names. Names containing variable or file references can't be imported.",
)
_TEMPLATE_NEXT_TO_BRACE = msg(
    "tui.mcp.import.unsupported.template_next_to_brace",
    fallback=(
        "{app} can't import this value because a variable reference is adjacent to a brace. "
        "Add the server manually and enter the full value in Environment Variables or Headers."
    ),
)
_FILE_REFERENCE = msg(
    "tui.mcp.import.unsupported.file_reference",
    fallback=(
        "{app} can't import values read from files. Add the server manually and fill in the required value "
        "from the file. For variable or credential command references, see the User Guide (F8)."
    ),
)
_HEADER_VALUE_ENCODING = msg(
    "tui.mcp.import.unsupported.header_value_encoding",
    fallback="This HTTP header contains non-ASCII text and can't be imported.",
)
_VALUE_FROM_COMMAND = msg(
    "tui.mcp.import.unsupported.value_from_command",
    fallback=(
        "This value comes from a command that needs to be configured manually in {app}. "
        "Add the server and use {template} in Environment Variables or Headers. "
        "For credential command usage, see the User Guide (F8)."
    ),
)
_VARIABLE_NAME = msg(
    "tui.mcp.import.unsupported.variable_name",
    fallback="This environment variable name isn't supported by {app}. Names must begin with an ASCII letter or underscore and contain only ASCII letters, digits or underscores.",
)
_EMPTY_ENV_VALUE = msg(
    "tui.mcp.import.unsupported.empty_env_value",
    fallback="{app} doesn't support environment variables with empty values, so this server can't be imported.",
)
_EMPTY_HEADER_VALUE = msg(
    "tui.mcp.import.unsupported.empty_header_value",
    fallback="{app} doesn't support HTTP headers with empty values, so this server can't be imported.",
)
_ENV_VALUE_WHITESPACE = msg(
    "tui.mcp.import.unsupported.env_value_whitespace",
    fallback="{app} doesn't support environment variable values with leading or trailing whitespace, so this server can't be imported.",
)
_UNSUPPORTED_SETTING = msg(
    "tui.mcp.import.unsupported.setting", fallback="{reason}\nSource setting: {field}", multiline=True
)
_VARIABLE_NAME_DETAILS = msg(
    "tui.mcp.import.unsupported.variable_name_details",
    fallback="{field} references the unsupported environment variable name {variable}. Names must begin with an ASCII letter or underscore and contain only ASCII letters, digits or underscores.",
)
_HEADER_NAME_COLLISION = msg(
    "tui.mcp.import.unsupported.header_name_collision",
    fallback="These headers have conflicting entries whose names differ only in case: {fields}.",
)
_REMOTE_ENVIRONMENT = msg(
    "tui.mcp.import.unsupported.remote_environment",
    fallback=(
        "This server uses a remote execution environment or variables from that environment. "
        "To use it in {app}, add it manually with connection settings that work on this machine "
        "and provide the required environment variables."
    ),
)
_COVERED = msg(
    "tui.mcp.import.covered",
    fallback="Same configuration as the selected {name} ({agent}, {scope}). It will be imported once.",
)
# The value as written is left out on purpose: the warning is saved in the description, and the default may be a secret.
_DEFAULT_DROPPED = msg(
    "tui.mcp.import.warning.default_dropped",
    fallback=(
        "Defaults for {variable} in {field} are not imported. "
        "Set {variable} before starting {app}, or enter a fixed value after import."
    ),
)
_HEADERS_HELPER_IGNORED = msg(
    "tui.mcp.import.warning.headers_helper_ignored",
    fallback=(
        "The headers helper is not imported. After import, add the required headers in this server's settings. "
        "To obtain each header's value using {template}, see the User Guide (F8)."
    ),
)
_OAUTH_IGNORED = msg(
    "tui.mcp.import.warning.oauth_ignored",
    fallback=(
        "OAuth settings and sign-in state are not imported. If the server accepts a token or another "
        "authentication method, configure Headers after import, or see the User Guide (F8) to obtain credentials "
        "using {template}."
    ),
)
_CODEX_OAUTH_IGNORED = msg(
    "tui.mcp.import.warning.codex_oauth_ignored",
    fallback=(
        "OAuth settings and sign-in state from Codex are not imported. If the server accepts a token or another "
        "authentication method, configure Headers after import, or see the User Guide (F8) to obtain credentials "
        "using {template}."
    ),
)
_CREDENTIAL_VARIABLE = msg(
    "tui.mcp.import.warning.credential_variable",
    fallback=(
        "To avoid unintentionally sending local credentials to an MCP server, references to {variable} "
        "in headers {field} are removed, matching {agent}. "
        "Other text is kept; headers with no remaining value are omitted."
    ),
)
_CLAUDE_PROJECT_DIR = msg(
    "tui.mcp.import.warning.claude_project_dir",
    fallback=(
        "{field} references {variable}, which {app} does not set automatically. "
        "After import, fill in the required project folder path in this field, "
        "or set {variable} before starting {app}."
    ),
)
_CWD_IGNORED = msg(
    "tui.mcp.import.warning.cwd_ignored",
    fallback=(
        "The working folder {folder} from {agent} is not imported. "
        "Before testing, start {app} there or use absolute paths in the server's command and arguments."
    ),
)
_RELATIVE_CWD_IGNORED = msg(
    "tui.mcp.import.warning.relative_cwd_ignored",
    fallback=(
        "The working folder {folder} from {agent}, relative to {base}, is not imported. "
        "Before testing, start {app} in the resolved folder or use absolute paths in the server's command and arguments."
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
        "These settings also come from other {agent} configuration sources: {fields}. "
        "Review them before importing. Additional sources: {files}."
    ),
)
_AUTH_PROVIDER_IGNORED = msg(
    "tui.mcp.import.warning.auth_provider_ignored",
    fallback=(
        "The {provider} credentials from {agent} are not imported. After import, configure this server's Headers "
        "with a token it accepts, or see the User Guide (F8) to obtain credentials using {template}."
    ),
)
_TOOL_RULES_IGNORED = msg(
    "tui.mcp.import.warning.tool_rules_ignored",
    fallback=(
        "These tool rules from {agent} are not imported: {rules}. Before using the server in {app}, "
        "check Available Tool Scope in this server's settings and select the tools you need. "
        "For tool loading options, see the User Guide (F8)."
    ),
)
_TOOL_PERMISSIONS_IGNORED = msg(
    "tui.mcp.import.warning.tool_permissions_ignored",
    fallback=(
        "Tool permissions from {agent} are not imported ({fields}). "
        "After import, select which tools are available under Available Tool Scope. "
        "To change whether tool execution requires approval, see the User Guide (F8) to configure approval rules."
    ),
)
_TOOLS_LIMITED_NONE = msg(
    "tui.mcp.import.warning.tools_limited.none",
    fallback="{agent} hides all tools under {rule}. After import, Available Tool Scope is set to No tools. Change it in the server's settings if needed.",
)
_TOOLS_LIMITED_SOME = msg(
    "tui.mcp.import.warning.tools_limited.some",
    fallback="Under {rule}, {agent} exposes only: {tools}. Check Available Tool Scope after import to change this selection.",
)
_OPTIONAL_VARIABLE = msg(
    "tui.mcp.import.warning.optional_variable",
    fallback=(
        "{field} references {variable}, which is optional in {agent} but must be nonempty in {app}. "
        "Set {variable} before starting {app}, or remove this setting after import if it is not needed."
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
    fallback=(
        "These source settings are not imported: {fields}. "
        "Check whether your server relies on them before using it in {app}. "
        "For supported settings, see the User Guide (F8)."
    ),
)
_INVALID_HEADER_NAME_IGNORED = msg(
    "tui.mcp.import.warning.invalid_header_name_ignored",
    fallback="{field} has an invalid HTTP header name. {agent} skips it, and it is not imported.",
)
_INVALID_HEADER_VALUE_IGNORED = msg(
    "tui.mcp.import.warning.invalid_header_value_ignored",
    fallback="{field} has an invalid HTTP header value. {agent} skips it, and it is not imported.",
)
_ZERO_TIMEOUT_IGNORED = msg(
    "tui.mcp.import.warning.zero_timeout_ignored",
    fallback=(
        "A timeout of 0 is not imported. {app} will use its default request timeout. "
        "To change it, set MCP Request Timeout after import."
    ),
)
_STARTUP_TIMEOUT_IGNORED = msg(
    "tui.mcp.import.warning.startup_timeout_ignored",
    fallback=(
        "startup_timeout_sec is not imported: {app} has no separate startup timeout. "
        "Connection setup and tool requests use MCP Request Timeout (seconds). "
        "After import, adjust that field in this server's settings if startup needs more time."
    ),
)
_TOOL_OUTPUT_LIMIT_IGNORED = msg(
    "tui.mcp.import.warning.tool_output_limit_ignored",
    fallback=(
        "These per-tool output limits are not imported: {fields}. "
        "You can set one output limit for the whole server after import. For configuration, see the User Guide (F8)."
    ),
)
_LITERAL_BRACES_ESCAPED = msg(
    "tui.mcp.import.warning.literal_braces_escaped",
    fallback="Braces look different in the preview; the text sent to the server is unchanged.",
)
_WARNING_TITLE = msg("tui.mcp.import.warning.title", fallback="Import Warnings")
_WARNING_NOTE = msg("tui.mcp.import.warning.note", fallback="Note:")
_DEFAULT_NOTE = msg(
    "tui.mcp.import.warning.default.note", fallback="The variable default in these fields will not be kept: {fields}."
)
_INFORMATIONAL_WARNING_KINDS = frozenset({ImportWarningKind.LITERAL_BRACES_ESCAPED, ImportWarningKind.MERGED})
_UNREADABLE = msg(
    "tui.mcp.import.source_error.unreadable",
    fallback="Couldn't read {path}. Check that the file exists and your account has permission to read it.",
)
_TOO_LARGE = msg(
    "tui.mcp.import.source_error.too_large",
    fallback="{path} exceeds the {limit_mib} MiB file size limit.",
)
_INVALID_JSON = msg(
    "tui.mcp.import.source_error.invalid_json",
    fallback="{path} is not valid JSON.",
)
_INVALID_JSON_LOCATION = msg(
    "tui.mcp.import.source_error.invalid_json_location",
    fallback="Couldn't parse {path} as JSON at line {line}, column {column}.",
)
_INVALID_TOML = msg(
    "tui.mcp.import.source_error.invalid_toml",
    fallback="Couldn't parse {path} as TOML.",
)
_INVALID_TOML_LOCATION = msg(
    "tui.mcp.import.source_error.invalid_toml_location",
    fallback="Couldn't parse {path} as TOML at line {line}, column {column}.",
)
_INVALID_ENCODING = msg(
    "tui.mcp.import.source_error.invalid_encoding",
    fallback="Couldn't read {path} as UTF-8.",
)
_INVALID_CONFIG_FIELD = msg(
    "tui.mcp.import.source_error.invalid_config_field",
    fallback="{field} in {path} must be a TOML table, such as {example}.",
)
_INVALID_CONFIG_SETTING = msg(
    "tui.mcp.import.source_error.invalid_config_setting",
    fallback="The setting {field} in {path} is invalid.",
)
_EXPECTED_OBJECT = msg(
    "tui.mcp.import.source_error.expected.object",
    fallback="{field} in {path} must be a JSON object.",
)
_EXPECTED_STRING = msg(
    "tui.mcp.import.source_error.expected.string",
    fallback="{field} in {path} must be a string.",
)
_EXPECTED_STRING_ARRAY = msg(
    "tui.mcp.import.source_error.expected.string_array",
    fallback="The command at {field} in {path} must be an array of strings, such as {example}.",
)
_EXPECTED_STRING_MAP = msg(
    "tui.mcp.import.source_error.expected.string_map",
    fallback="{field} in {path} must be a JSON object whose values are strings.",
)
_EXPECTED_BOOLEAN = msg(
    "tui.mcp.import.source_error.expected.boolean",
    fallback="{field} in {path} must be true or false.",
)
_EXPECTED_POSITIVE_INTEGER = msg(
    "tui.mcp.import.source_error.expected.positive_integer",
    fallback="{field} in {path} must be a positive integer.",
)
_EXPECTED_SERVER_TYPE = msg(
    "tui.mcp.import.source_error.expected.server_type",
    fallback='{field} in {path} must be "local" or "remote".',
)
_EXPECTED_VALUES = {
    ConfigValueKind.OBJECT: _EXPECTED_OBJECT,
    ConfigValueKind.STRING: _EXPECTED_STRING,
    ConfigValueKind.STRING_ARRAY: _EXPECTED_STRING_ARRAY,
    ConfigValueKind.STRING_MAP: _EXPECTED_STRING_MAP,
    ConfigValueKind.BOOLEAN: _EXPECTED_BOOLEAN,
    ConfigValueKind.POSITIVE_INTEGER: _EXPECTED_POSITIVE_INTEGER,
    ConfigValueKind.SERVER_TYPE: _EXPECTED_SERVER_TYPE,
}
_INVALID_JSON_OBJECT_FIELD = msg(
    "tui.mcp.import.source_error.invalid_json_object_field",
    fallback="{field} in {path} must be a JSON object.",
)
_INVALID_SERVER_CONFIG = msg(
    "tui.mcp.import.source_error.invalid_server_config",
    fallback="The MCP server settings at {field} in {path} are invalid. Type local requires a command array; type remote requires a URL.",
)
_NOT_AN_OBJECT = msg(
    "tui.mcp.import.source_error.not_an_object",
    fallback="{path} must contain a JSON object at the top level.",
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
    UnsupportedReason.EMPTY_ENV_VALUE: _EMPTY_ENV_VALUE,
    UnsupportedReason.EMPTY_HEADER_VALUE: _EMPTY_HEADER_VALUE,
    UnsupportedReason.ENV_VALUE_WHITESPACE: _ENV_VALUE_WHITESPACE,
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
        if error.expected is not None:
            field = sanitize_legacy_scalar(error.field)
            if error.expected is ConfigValueKind.STRING_ARRAY:
                return _EXPECTED_STRING_ARRAY.bind(path=path, field=field, example='["server", "--flag"]')
            return _EXPECTED_VALUES[error.expected].bind(path=path, field=field)
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
    """One server's checkbox, outside its card and level with the card's title line.

    It carries no label: the card's title names the server, so ticking and
    folding stay two separate targets. It names its agent too, since its
    Changed may land after the pane switched agents.
    """

    def __init__(self, name: str, *, source_id: str, index: int, value: bool, disabled: bool) -> None:
        super().__init__(
            "",
            value=value,
            disabled=disabled,
            id=f"mcp-import-row-{index}",
            classes="mcp-import-row",
        )
        self.server_name = name
        self.source_id = source_id
        self.index = index

    def render(self) -> Content:
        """Draw the tick alone: Textual pads the empty label, and that pad paints a block of its own."""
        return self._button


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
    annotation: str = ""


class _AnnotatedFieldValue(Static):
    """A value with a quieter inline explanation, following the active theme."""

    COMPONENT_CLASSES: ClassVar[set[str]] = {"mcp-import-value--annotation"}

    def __init__(self, value: str, annotation: str, *, classes: str) -> None:
        super().__init__(Content.assemble(value, (annotation, ".mcp-import-value--annotation")), classes=classes)


@dataclass(frozen=True, slots=True)
class _WarningGroup:
    text: str
    attention: bool = True


def _field_widgets(field: _CardField) -> ComposeResult:
    yield Label(Content.from_text(field.label, markup=False), classes="mcp-import-field-label")
    if field.pairs:
        yield _PairGrid(field.pairs)
    else:
        classes = "mcp-import-field-value" + (" -single-line" if field.single_line else "")
        if field.annotation:
            yield _AnnotatedFieldValue(field.value, field.annotation, classes=classes)
        else:
            yield Static(Text(field.value), classes=classes)


# Textual puts the fold symbol and a space before the label; the gap follows the name before the summary.
_SYMBOL_WIDTH = 2
_SUMMARY_CLASS = "cardtitle--summary"
_NAME_STYLE = "bold"


def _elide_end(text: str, width: int) -> str:
    """*text* cut at *width* cells, with an ellipsis in the last cell."""
    if width <= 0:
        return ""
    if cell_len(text) <= width:
        return text
    return set_cell_size(text, width - 1) + "…"


class _CardTitle(CollapsibleTitle):
    """The card's one title line, and the fold's target: symbol, name, then a muted summary.

    The status reserves space on the right. The connection summary gives way
    before the name when space runs out. The line never wraps.
    """

    BINDINGS: ClassVar[list] = [
        localized_binding("enter,space", "toggle_collapsible", _TOGGLE_BINDING, show=False),
    ]

    # The summary takes its colour from the component class, so it follows the
    # line's own hover and focus colours instead of staying a fixed gray.
    COMPONENT_CLASSES: ClassVar[set[str]] = {_SUMMARY_CLASS}

    def __init__(self, name: str, *, transport: str, target: str, collapsed: bool) -> None:
        # ``_name`` is Textual's node name, so the server's name is kept apart from it.
        self._server_name = name
        self._summary_transport = transport
        self._summary_target = target
        self._status = ""
        self._status_widget = Static(classes="mcp-import-status")
        self._status_widget.display = False
        super().__init__(
            label=Content.assemble((name, _NAME_STYLE)),
            collapsed_symbol="▶",
            expanded_symbol="▼",
            collapsed=collapsed,
        )

    def compose(self) -> ComposeResult:
        yield self._status_widget

    def on_resize(self, event: Resize) -> None:
        # Resize carries the new outer width; fitting must exclude the title's padding.
        self._fit(max(0, event.size.width - self.styles.gutter.width))
        # A scrollbar can narrow the pane again within the same layout pass.
        self.call_after_refresh(self._fit)

    def set_status(self, status: str, *, attention: bool) -> None:
        self._status = status
        self._status_widget.update(Text(status))
        self._status_widget.display = bool(status)
        self.set_class(attention, "-attention")
        self._fit()

    def _fit(self, width: int | None = None) -> None:
        """Lay the name and the summary out again for the width the card has now."""
        if width is None:
            width = self.content_size.width
        if width <= 0:
            return
        room = max(0, width - _SYMBOL_WIDTH)
        room = max(0, room - (cell_len(self._status) + 2 if self._status else 0))
        name = self._server_name
        summary = self._summary(room - cell_len(name)) if self.collapsed else ""
        if summary:
            content = Content.assemble((name, _NAME_STYLE), (summary, f".{_SUMMARY_CLASS}"))
        else:
            content = Content.assemble((_elide_end(name, room), _NAME_STYLE))
        if content != self.label:
            self.label = content

    def _summary(self, room: int) -> str:
        """Fit the connection summary after its colon, dropping the target before the transport."""
        transport = self._summary_transport
        if not transport or room <= 0:
            return ""
        full = f"{transport} · {self._summary_target}" if self._summary_target else transport
        if cell_len(f": {full}") <= room:
            return f": {full}"
        prefix = f": {transport} · "
        # Keep at least one target character and the ellipsis, or show only the transport.
        if self._summary_target and cell_len(prefix) + 2 <= room:
            return prefix + _elide_end(self._summary_target, room - cell_len(prefix))
        return f": {transport}" if cell_len(f": {transport}") <= room else ""


@dataclass(frozen=True, slots=True)
class _CardSection:
    """A titled box of settings, as the agent's MCP card groups them."""

    title: str
    fields: tuple[_CardField, ...]


class _Section(Vertical):
    """One titled box; it composes its own fields, so a card can build one when it is unfolded."""

    def __init__(self, section: _CardSection) -> None:
        super().__init__(classes="mcp-import-section")
        self._section = section
        # Titled before it mounts, so a card built while unfolding needs no second layout for it.
        if section.title:
            self.border_title = Text(section.title)

    def compose(self) -> ComposeResult:
        for field in self._section.fields:
            yield from _field_widgets(field)


class _WarningBlock(Vertical):
    """One warning paragraph, with related fields named together."""

    def __init__(self, group: _WarningGroup) -> None:
        super().__init__(classes="mcp-import-warning-group")
        self._group = group

    def compose(self) -> ComposeResult:
        yield Static(Text(self._group.text), classes="mcp-import-warning")


class _ImportItem(Horizontal):
    """One server's row: its checkbox, outside the card, level with the card's title line."""

    def __init__(self, row: _ImportRow, card: _ImportCard) -> None:
        super().__init__(classes="mcp-import-item")
        self.row = row
        self.card = card

    def compose(self) -> ComposeResult:
        yield self.row
        yield self.card


class _ImportCard(CollapsibleConfigCard):
    """A server's settings, read-only, folded to one title line, as the agent's MCP card opens.

    The title toggles the fold and reserves room for a short status on the right.
    Details appear only when unfolded, with warnings grouped apart from settings.
    The body is built on the first unfold and kept afterwards.
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
        name: str,
        transport: str,
        target: str,
        source_fields: Sequence[_CardField],
        sections: Sequence[_CardSection],
        warnings: Sequence[_WarningGroup],
        hint: str | None = None,
        covered: str | None = None,
        status_labels: tuple[str, str, str, str] = ("⚠", "", "", ""),
        collapsed: bool = True,
    ) -> None:
        self._row = row
        self._importable = importable
        self._server_name = name
        self._transport = transport
        self._target = target
        self._source_fields = list(source_fields)
        self._sections = list(sections)
        self._warnings = list(warnings)
        self._status_labels = status_labels
        self._needs_attention = any(warning.attention for warning in warnings)
        # The hint this server has on its own; a cover note shows instead of it while it is locked.
        self._own_hint = hint
        self._hint = covered if covered is not None else hint
        # A covered card keeps its note under the title even unfolded: its body carries no other trace of the lock.
        self._covered = covered is not None
        # The body is built once, on the first unfold; a card rebuilt unfolded builds it at once.
        self._body_built = not collapsed
        self._hint_widget = Static(Text(self._hint or ""), classes="mcp-import-hint")
        self._hint_title = Static(classes="mcp-import-notes-title")
        self._hint_panel = Vertical(self._hint_title, self._hint_widget, classes="mcp-import-notes")
        classes = "mcp-import-card" + (" -unavailable" if row.disabled else "")
        super().__init__(index, collapsed=collapsed, classes=classes, read_only=True)
        self._sync_hint()

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
        hint = note if note is not None else self._own_hint
        if not self._importable or hint == self._hint:
            return
        self._hint = hint
        self._covered = note is not None
        lock = note is not None
        self._row.disabled = lock
        self.set_class(lock, "-unavailable")
        self._hint_widget.update(Text(hint or ""))
        self._sync_hint()

    def _sync_hint(self) -> None:
        """Keep only the short status folded; reasons and lock details appear on unfolding."""
        self._hint_widget.display = (
            self._hint is not None and not self.collapsed and (self._covered or not self._importable)
        )
        self._hint_panel.display = self._hint_widget.display
        self._hint_panel.set_class(not self._covered, "-error")
        attention, unavailable, covered, imported = self._status_labels
        self._hint_title.update(Text(covered if self._covered else f"✕ {unavailable}"))
        if isinstance(self._title_widget, _CardTitle):
            status = (
                covered
                if self._covered
                else unavailable
                if not self._importable and self._hint
                else imported
                if not self._importable
                else (attention if self._needs_attention else "")
            )
            self._title_widget.set_status(status, attention=bool(status == attention and status))

    def set_collapsed(self, collapsed: bool, *, scroll_visible: bool = True) -> None:
        changed = collapsed != self.collapsed
        super().set_collapsed(collapsed, scroll_visible=scroll_visible)
        if not collapsed:
            self._build_body()
        self._sync_hint()
        if changed:
            self.post_message(self.Folded(self._row.source_id, self._row.index, collapsed))

    def _build_body(self) -> None:
        """Build the body's fields the first time the card is unfolded."""
        if self._body_built or self._body is None:
            return
        self._body_built = True
        self._body.mount_all(self._body_widgets())

    def compose(self) -> ComposeResult:
        yield self._make_title(self._server_name, classes="mcp-import-title")
        yield self._hint_panel
        with self.card_body() as body:
            if self._body_built:
                yield from self._body_widgets()
            self._body = body

    def _make_title(self, title: str, *, classes: str) -> Widget:
        self._title_text = title
        title_widget = _CardTitle(
            title,
            transport=self._transport,
            target=self._target,
            collapsed=self.collapsed,
        )
        title_widget.add_class(*classes.split())
        self._title_widget = title_widget
        self._sync_hint()
        return title_widget

    def _body_widgets(self) -> list[Widget]:
        """Everything under the title: what import changes, where the server came from, then its boxes."""
        widgets: list[Widget] = []
        for attention in (True, False):
            warnings = [warning for warning in self._warnings if warning.attention is attention]
            if not warnings:
                continue
            notes: list[Widget] = []
            if attention:
                notes.append(
                    Static(
                        Text(f"⚠ {render_str(widget_localizer(self), _WARNING_TITLE.bind())}"),
                        classes="mcp-import-notes-title",
                    )
                )
            notes.extend(_WarningBlock(warning) for warning in warnings)
            widgets.append(Vertical(*notes, classes="mcp-import-notes" + (" -attention" if attention else "")))
        for field in self._source_fields:
            widgets.extend(_field_widgets(field))
        widgets.extend(_Section(section) for section in self._sections)
        return widgets


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
        self._load_locks: dict[str, asyncio.Lock] = {}
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
                # Pane rebuilds share one source snapshot, including its candidate indices.
                async with self._load_locks.setdefault(source_id, asyncio.Lock()):
                    if source_id not in self._results:
                        try:
                            result: McpImportResult | None = await asyncio.to_thread(
                                source.discover, self._import_context
                            )
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
            # A server the import can convert shows its transport and boxes; a refused one shows only what
            # its source wrote, under the one box its body has.
            converted = candidate.config is not None
            # Cards open folded, as on the agent's MCP card; a fold the user made wins over that default.
            collapsed = self._folded.get(source.source_id, {}).get(index, True)
            cards = _ImportCard(
                index=index,
                row=row,
                importable=state == "importable",
                name=sanitize_legacy_scalar(candidate.name),
                transport=sanitize_legacy_scalar(candidate.transport.upper()),
                target=self._summary_target(candidate),
                source_fields=self._source_fields(candidate, agent, converted=converted),
                sections=self._sections(candidate) if converted else self._source_sections(candidate),
                collapsed=collapsed,
                # What the conversion changed only matters for a server that will be imported.
                warnings=[] if state == "added" else self._warning_groups(candidate.warnings, agent),
                hint=note,
                covered=covered,
                status_labels=(
                    "⚠",
                    self._render_message(_UNAVAILABLE.bind()),
                    self._render_message(_DUPLICATE.bind()),
                    self._render_message(_IMPORTED.bind()),
                ),
            )
            widgets.append(_ImportItem(row, cards))
        # A file that couldn't be read already says why nothing is listed.
        if not result.candidates and not result.source_errors:
            # The highlighted agent in the sidebar names whose servers these are, as the agent's MCP tab says it.
            widgets.append(HatchedEmptyState(self._render_message(_EMPTY_SOURCE.bind()), classes="mcp-import-empty"))
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
            else _HEADER_VALUE_ENCODING.bind()
            if candidate.unsupported is UnsupportedReason.HEADER_VALUE_ENCODING
            else _UNSUPPORTED_REASONS[candidate.unsupported].bind(app=APP_DISPLAY_NAME)
        )
        if candidate.unsupported_field:
            return self._render_message(
                _UNSUPPORTED_SETTING.bind(field=sanitize_legacy_scalar(candidate.unsupported_field), reason=reason)
            )
        return reason

    def _summary_target(self, candidate: McpImportCandidate) -> str:
        """The program or URL host, never the arguments or credentials the card shows unfolded."""
        config = candidate.config
        if config is not None:
            return mcp_target_summary(config)
        entry = candidate.entry or {}
        command = entry.get("command")
        if isinstance(command, str):
            # Handwritten YAML may put arguments in command; cut them before looking for path separators.
            parts = command.split(maxsplit=1)
            return sanitize_legacy_scalar(parts[0].replace("\\", "/").rsplit("/", 1)[-1]) if parts else ""
        url = entry.get("url")
        if not isinstance(url, str):
            return ""
        try:
            return sanitize_legacy_scalar(urlsplit(url).hostname or "")
        except ValueError:
            return ""

    def _source_fields(self, candidate: McpImportCandidate, agent: str, *, converted: bool) -> list[_CardField]:
        """Where the server came from, then — for a server the import converts — what it is.

        The source area says where the settings were found: the file, the scope there
        and whether the source enables it. A converted server then opens with the
        transport and description as the agent's MCP card does, before its boxes. A
        refused one shows neither: its body has only what its source actually wrote.
        """
        fields = [
            _CardField(
                self._render_message(_CONFIGURATION_SOURCE.bind()),
                sanitize_legacy_scalar(candidate.source_path),
                single_line=True,
            ),
        ]
        # The source's own word for the scope, then where it applies; a long folder wraps.
        scope = self._scope_label(candidate.scope)
        where = (
            _SCOPE_ONLY_IN.bind(scope=scope, path=sanitize_legacy_scalar(candidate.scope_path))
            if candidate.scope_path
            else _SCOPE_EVERY_PROJECT.bind(scope=scope)
        )
        fields.append(_CardField(self._render_message(_SCOPE.bind(agent=agent)), self._render_message(where)))
        if converted and candidate.scope_path:
            fields.append(
                _CardField(
                    self._render_message(_SCOPE_AFTER_IMPORT.bind()), self._render_message(_SCOPE_IMPORTED_ALL.bind())
                )
            )
        if candidate.config is not None and not candidate.config.enabled:
            fields.append(
                _CardField(self._render_message(MCP_ENABLED.bind()), self._render_message(_NOT_ENABLED.bind()))
            )
        if not converted:
            return fields
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
        return fields

    def _connection_title(self, candidate: McpImportCandidate) -> str:
        """The connection box's title, as the agent's MCP card names it; none when the source names no transport."""
        if not candidate.transport:
            return ""
        transport = sanitize_legacy_scalar(candidate.transport.upper())
        return self._render_message(_TRANSPORT_OPTIONS.bind(transport=transport))

    def _connection_fields(self, candidate: McpImportCandidate) -> list[_CardField]:
        """The connection settings the import keeps, named as on the agent's MCP card."""
        config = candidate.config
        if config is None:
            return []
        if config.transport == "http":
            return self._fields(url=config.url, headers=config.headers)
        return self._fields(command=format_mcp_command_line(config.command, config.args), env=config.env)

    def _source_connection_fields(self, candidate: McpImportCandidate) -> list[_CardField]:
        """What the source wrote for a server that can't be imported: it has no converted settings to show."""
        entry = candidate.entry or {}
        fields: list[_CardField] = []
        # An unsupported transport is why the server can't be imported, so it leads.
        if candidate.transport:
            fields.append(
                _CardField(
                    self._render_message(MCP_TRANSPORT_LABEL.bind()),
                    sanitize_legacy_scalar(candidate.transport.upper()),
                    single_line=True,
                )
            )
        command = entry.get("command")
        args = entry.get("args")
        command_line = None
        if isinstance(command, str):
            arg_list = [arg for arg in args if isinstance(arg, str)] if isinstance(args, list) else []
            command_line = format_mcp_command_line(command, arg_list)
        url = entry.get("url")
        fields.extend(
            self._fields(
                command=command_line,
                url=url if isinstance(url, str) else None,
                env=entry.get("env"),
                headers=entry.get("headers"),
            )
        )
        env_headers = entry.get("env_http_headers")
        if isinstance(env_headers, Mapping) and env_headers:
            fields.append(
                _CardField(self._render_message(_ENV_HTTP_HEADERS.bind()), pairs=_key_value_pairs(env_headers))
            )
        return fields

    def _sections(self, candidate: McpImportCandidate) -> list[_CardSection]:
        """The boxes a converted server shows, in the agent's MCP card order."""
        sections = [
            _CardSection(self._connection_title(candidate), tuple(self._connection_fields(candidate))),
            _CardSection(self._render_message(_TOOL_ACCESS.bind()), tuple(self._tool_access_fields(candidate))),
        ]
        if candidate.config is not None and candidate.config.allowed_tools != []:
            sections.append(
                _CardSection(self._render_message(_TOOL_LOADING.bind()), tuple(self._tool_loading_fields(candidate)))
            )
        sections.append(
            _CardSection(
                self._render_message(_NAMING_AND_LIMITS.bind()),
                # Only a server that will be imported gets one.
                tuple(self._naming_fields(candidate)),
            )
        )
        return sections

    def _source_sections(self, candidate: McpImportCandidate) -> list[_CardSection]:
        """The one box a server that can't be imported shows: what the source wrote, never converted settings."""
        fields = tuple(self._source_connection_fields(candidate))
        return [_CardSection(self._render_message(_SOURCE_CONNECTION.bind()), fields)] if fields else []

    def _timeout_field(self, candidate: McpImportCandidate) -> _CardField | None:
        """The request timeout the import keeps, or None when the source kept the default."""
        config = candidate.config
        if config is None or config.request_timeout is None:
            return None
        timeout = _TIMEOUT_SECONDS.bind(seconds=config.request_timeout)
        annotation: MessageRef | None = None
        if candidate.timeout_source_ms is not None and candidate.timeout_source_ms != config.request_timeout * 1000:
            annotation = _TIMEOUT_CONVERTED.bind(milliseconds=candidate.timeout_source_ms)
        elif (
            candidate.timeout_source_seconds is not None and candidate.timeout_source_seconds != config.request_timeout
        ):
            annotation = (
                _TIMEOUT_UNLIMITED.bind()
                if isinstance(candidate.timeout_source_seconds, float)
                and not math.isfinite(candidate.timeout_source_seconds)
                else _TIMEOUT_CONVERTED_SECONDS.bind(source_seconds=str(candidate.timeout_source_seconds))
            )
        return _CardField(
            self._render_message(_REQUEST_TIMEOUT.bind()),
            self._render_message(timeout),
            annotation=self._render_message(annotation) if annotation is not None else "",
        )

    def _tool_access_fields(self, candidate: McpImportCandidate) -> list[_CardField]:
        """The available-tool scope alone, as the agent's MCP card boxes it under Tool Access."""
        config = candidate.config
        if config is None:
            return []
        if config.allowed_tools is None:
            tool_scope = _ALL_TOOLS.bind()
        elif not config.allowed_tools:
            tool_scope = MCP_TOOL_ACCESS_NONE.bind()
        else:
            tool_scope = _SELECTED_TOOLS.bind(
                tools=", ".join(sanitize_legacy_scalar(tool) for tool in config.allowed_tools)
            )
        return [_CardField(self._render_message(_AVAILABLE_TOOL_SCOPE.bind()), self._render_message(tool_scope))]

    def _tool_loading_fields(self, candidate: McpImportCandidate) -> list[_CardField]:
        """The loading strategy and the initially visible tools, as the agent's MCP card boxes them."""
        config = candidate.config
        if config is None:
            return []
        strategy = _TOOL_LOADING_PROGRESSIVE_LABEL if config.use_progressive_disclosure else _TOOL_LOADING_FULL_LABEL
        fields = [_CardField(self._render_message(_LOADING_STRATEGY.bind()), self._render_message(strategy.bind()))]
        if config.use_progressive_disclosure:
            initially_visible = (
                ", ".join(sanitize_legacy_scalar(tool) for tool in config.always_load)
                if config.always_load
                else self._render_message(MCP_TOOL_ACCESS_NONE.bind())
            )
            fields.append(_CardField(self._render_message(MCP_INITIALLY_VISIBLE_TOOLS.bind()), initially_visible))
        return fields

    def _naming_fields(self, candidate: McpImportCandidate) -> list[_CardField]:
        """The tool name prefix and request timeout the import keeps; none when there is nothing to say."""
        if candidate.config is None:
            return []
        fields: list[_CardField] = []
        prefix = with_tool_name_prefix(candidate.config).tool_name_prefix
        if prefix:
            label = self._render_message(MCP_TOOL_NAME_PREFIX.bind())
            fields.append(_CardField(label, sanitize_legacy_scalar(prefix), single_line=True))
        timeout = self._timeout_field(candidate)
        if timeout is not None:
            fields.append(timeout)
        elif any(warning.kind is ImportWarningKind.ZERO_TIMEOUT_IGNORED for warning in candidate.warnings):
            fields.append(
                _CardField(
                    self._render_message(_REQUEST_TIMEOUT.bind()),
                    self._render_message(_TIMEOUT_DEFAULT.bind(app=APP_DISPLAY_NAME)),
                )
            )
        return fields

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
                groups.append(
                    _WarningGroup(
                        key,
                        attention=kind not in _INFORMATIONAL_WARNING_KINDS,
                    )
                )
                continue
            variable = sanitize_legacy_scalar(key)
            fields = ", ".join(
                dict.fromkeys(
                    sanitize_legacy_scalar(item.field) for item in sorted(items, key=lambda item: item.kind is not kind)
                )
            )
            text = self._warning_text(replace(items[0], kind=kind, field=fields, variable=variable), agent)
            defaults = ", ".join(
                dict.fromkeys(
                    sanitize_legacy_scalar(item.field)
                    for item in items
                    if item.kind is ImportWarningKind.DEFAULT_DROPPED
                )
            )
            if kind is ImportWarningKind.CLAUDE_PROJECT_DIR and defaults:
                text += " " + self._render_message(_DEFAULT_NOTE.bind(fields=defaults))
            groups.append(_WarningGroup(text))
        return groups

    def _warning_text(self, warning: ImportWarning, agent: str) -> str:
        variable = sanitize_legacy_scalar(warning.variable)
        field = sanitize_legacy_scalar(warning.field)
        original = sanitize_legacy_scalar(warning.original)
        match warning.kind:
            case ImportWarningKind.DEFAULT_DROPPED:
                reference = _DEFAULT_DROPPED.bind(
                    field=field,
                    variable=variable,
                    app=APP_DISPLAY_NAME,
                )
            case ImportWarningKind.HEADERS_HELPER_IGNORED:
                reference = _HEADERS_HELPER_IGNORED.bind(template="{{!command}}")
            case ImportWarningKind.OAUTH_IGNORED:
                reference = (_CODEX_OAUTH_IGNORED if agent == "Codex" else _OAUTH_IGNORED).bind(template="{{!command}}")
            case ImportWarningKind.CREDENTIAL_VARIABLE:
                reference = _CREDENTIAL_VARIABLE.bind(field=field, variable=variable, agent=agent)
            case ImportWarningKind.CLAUDE_PROJECT_DIR:
                reference = _CLAUDE_PROJECT_DIR.bind(field=field, variable=variable, app=APP_DISPLAY_NAME)
            case ImportWarningKind.UNKNOWN_FIELDS_IGNORED:
                reference = _UNKNOWN_FIELDS_IGNORED.bind(fields=original, app=APP_DISPLAY_NAME)
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
                reference = _AUTH_PROVIDER_IGNORED.bind(agent=agent, provider=original, template="{{!command}}")
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
                reference = _LITERAL_BRACES_ESCAPED.bind()
            case ImportWarningKind.INVALID_HEADER_IGNORED:
                message = _INVALID_HEADER_NAME_IGNORED if original == "name" else _INVALID_HEADER_VALUE_IGNORED
                reference = message.bind(field=field, agent=agent)
            case ImportWarningKind.ZERO_TIMEOUT_IGNORED:
                reference = _ZERO_TIMEOUT_IGNORED.bind(app=APP_DISPLAY_NAME)
            case ImportWarningKind.STARTUP_TIMEOUT_IGNORED:
                reference = _STARTUP_TIMEOUT_IGNORED.bind(app=APP_DISPLAY_NAME)
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
                note_label = self._render_message(_WARNING_NOTE.bind())
                notes = list(
                    dict.fromkeys(
                        f"{note_label if warning.kind in _INFORMATIONAL_WARNING_KINDS else '⚠'} "
                        f"{self._warning_text(warning, source.display_name)}"
                        for warning in candidate.warnings
                    )
                )
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
