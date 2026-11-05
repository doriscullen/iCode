# Connect MCP servers

Model Context Protocol (MCP) servers can provide iCode agents with external tools, prompt templates, and usage instructions. This guide explains how to connect HTTP or STDIO MCP servers in the terminal user interface (TUI), configure which tools are available, and control whether prompt templates are loaded and server instructions are included in the model context.

Before connecting an MCP server, confirm that its source is trustworthy. The server may receive tool call arguments and access local files, network services, or account data. Its prompt templates and usage instructions may also affect the agent's behavior.

To give agents tools written as a few Python functions, without running a server, see [Python extensions](./python-extensions.md).

## Add an MCP server configuration

Enter `/agents mcp` in the input field to open the current agent's "MCP" tab. Added server configurations apply only to the current agent; they are not automatically applied to other agents.

Each server has its own card. Saved servers are collapsed and show only their name; click the name to expand or collapse a card. If saving finds a problem, the card with the problem expands. To delete a server, click "✕" on its card and confirm.

Click "+ Add" to reuse an MCP configuration from another agent. When configurations are available, a picker shows their names, source agents, and the executable name or URL host. Select one to copy its complete settings into an expanded card at the top of the list. The copy is independent: later edits or deletion in either agent do not change the other. Disabled configurations stay disabled when copied. The picker uses the latest drafts in the open agent configuration window, including edits you have not saved yet.

Equivalent configurations from several agents appear once with all their sources. Configurations with the same name but different settings appear separately. Selectable configurations appear first, followed by grayed-out configurations; each group is sorted by name. Grayed-out items explain why they cannot be added: the current agent already has that name, or uses the same connection and tool name prefix with overlapping allowed tools. Disabled configurations are included in this check.

The picker does not connect to servers. If loading the agent reports a tool name conflict, follow the error guidance to adjust the affected servers’ tool name prefixes or exclude overlapping tools. Testing one server successfully does not rule out conflicts with other servers.

To configure a new server, select "+ New MCP configuration…". If no valid configurations are available from other agents, "+ Add" creates a blank card directly. Invalid source configurations are omitted. Canceling the picker leaves your configuration unchanged. Both copied and new configurations take effect through the configuration page's existing Save flow.

In a new card, fill in the following fields:

1. Enter a "Server Name". This name identifies the server in the current agent's MCP configuration. Server names must be unique within an agent's MCP configuration, and are case-insensitive.
2. Optionally enter a "Description" to note the server's purpose. This description is not provided to the agent as server instructions.

HTTP header values and STDIO environment variable values can both use `{{ENV_VAR}}` to reference environment variables set before starting iCode. Variable names can contain only letters, digits, and underscores, and cannot start with a digit.

Both can also use what a command prints, with `{{!command}}`, such as `Bearer {{!tokenprovider gettoken}}`. See [Get an API key from a command](../configuration/models.md#get-an-api-key-from-a-command) for what the command has to do. An HTTP server gets a new value while iCode runs. An STDIO server keeps the value it started with, and gets a new one only when iCode starts it again. If the command fails the first time, the server fails to connect.

When editing YAML by hand, numeric header and environment variable values are read as text. Write `timeout` as a positive integer without quotes; invalid values must be corrected before saving.

Next, complete the HTTP or STDIO settings according to the server's connection method.

### Connect over HTTP

1. Set "Transport" to `HTTP`.
2. Enter the server URL. The URL must begin with `http://` or `https://`.
3. If the server requires authentication, add names and values under "Headers". Use `{{ENV_VAR}}` to reference environment variables set before starting iCode, for example:

   ```text
   Name: Authorization
   Value: Bearer {{MCP_API_TOKEN}}
   ```

4. If the current network requires this server to connect without using the system proxy, enable "Bypass proxy".
5. Keeping TLS certificate verification enabled is recommended. To connect to a self-signed server you have confirmed is trustworthy, you can enable "Skip TLS verification". When enabled, iCode does not verify the HTTPS server certificate and cannot confirm that it is connecting to the intended server.

HTTP redirects must keep the request method and stay on the same origin (scheme, host, and port); an HTTP-to-HTTPS upgrade on the same host using default ports is also allowed. For POST requests, this permits `307` and `308` redirects, such as adding a trailing slash. If a redirect changes the host or port, downgrades HTTPS to HTTP, or changes a POST into a GET, the connection fails. Configure the intended final server URL directly in that case.

### Connect over STDIO

1. Set "Transport" to `STDIO`.
2. In "Command", enter the program name or path used to start the server, along with any required arguments. The TUI splits this line and saves it into the `command` and `args` fields in the agent YAML. When editing YAML by hand, put the executable in `command` and each argument in `args` as a separate item. On Windows, if `command` is `python` or `python3`, iCode skips the placeholder that only offers Python from the Microsoft Store and runs the next program of that name on `PATH`. python.org's traditional installer adds no `python3`, and puts `python` on `PATH` only if you choose that when installing: otherwise write the full path of its `python.exe`.
3. Add any environment variables the process needs. Enter values directly or use `{{ENV_VAR}}` to reference environment variables set before starting iCode. Values must be nonempty. The editor removes leading and trailing whitespace when saving.

An STDIO connection starts the specified program on the local machine. If the startup command downloads or runs third-party packages or scripts, confirm that their sources are trustworthy and review the relevant content first.

## Import servers from other applications

You can copy MCP servers from Claude Code, Codex, opencode and pi into the current iCode agent. This is a one-time copy: later changes in the source application do not update iCode.

1. Open `/agents mcp` and click "Import from other applications" next to "+ Add".
2. Select an application on the left. Expand a server card to review its connection settings, configuration source and import warnings.
3. Tick the servers you want, then click "Import". You can select servers from several applications at once.
4. Review the new cards, especially "Available Tool Scope", "Tool Name Prefix" and "MCP Request Timeout". Click "Test", then "Save". Importing alone does not connect to a server.

A ⚠ marks changes that need your attention. "Can't import" cards explain the unsupported setting and identify its source file. "Imported" means the current agent already has the configuration; "Duplicate" means the same configuration is already selected and will be imported once. If saving reports duplicate names, rename one of the servers.

### Check before use

- **Scope and working folder**: Imported servers are available in every project that uses the current agent, even if they were limited to one project in the source application. STDIO servers start in iCode's current working folder. If a server needs a specific folder, start iCode there or adjust the server's command and paths before testing.
- **Tools and timeouts**: Disabled servers stay disabled. Review the imported tool scope and timeout; settings without an iCode equivalent appear in the warnings. Source application approval rules do not transfer. Configure [iCode's approval mode and rules](../configuration/approval.md) as needed.
- **Credentials**: Literal tokens and passwords are copied into the agent file. OAuth sign-in state and authentication providers do not transfer, even when no warning appears. If authentication is required, supply the headers the server expects, such as `Authorization: Bearer <access-token>`. Replace expired tokens yourself or use an [external credential command](#move-command-values-and-headers-helpers-manually); iCode does not perform OAuth login.
- **Environment variables**: Set referenced variables before starting iCode. Imported references use `{{NAME}}` and require nonempty values. HTTP header values must be ASCII, with no line breaks, control characters or surrounding whitespace. Empty or non-ASCII literal headers cannot be imported. STDIO environment values cannot be empty or have surrounding whitespace.
- **Unsupported connections**: Remote servers must support Streamable HTTP; SSE-only and WebSocket servers are unsupported. Commands, arguments and URLs containing environment references cannot be imported. Add these servers manually using the actual program, arguments or full URL.

### Configuration sources and application-specific checks

"Configuration Source" on each card identifies the files to inspect or edit. The locations below help you find missing configurations; refer to each application's documentation for its full configuration rules.

| Application | Common configuration locations | Check before use |
| --- | --- | --- |
| [Claude Code](https://code.claude.com/docs/en/mcp) | `~/.claude.json` for user/local servers; `.mcp.json` in the working folder or its ancestors for project servers. Custom locations under `CLAUDE_CONFIG_DIR` are also supported. | Variable defaults are not carried over: set each referenced variable. If the server needs `CLAUDE_PROJECT_DIR`, set it yourself. iCode does not provide MCP roots; check whether the server accepts an explicit folder instead. |
| [Codex](https://developers.openai.com/codex/mcp) | `~/.codex/config.toml` (or under `CODEX_HOME`) and project `.codex/config.toml` files. | Set variables flagged as required, or remove unneeded headers. A tool deny list may import as "No tools"; select the tools you want after import. STDIO servers inherit iCode's environment: unset variables you do not want to pass on before starting iCode. |
| [opencode](https://opencode.ai/docs/mcp-servers/) | Global configuration under `~/.config/opencode`, project `opencode.json` / `opencode.jsonc` and `.opencode` folders; custom configuration locations are also supported. | `{file:...}` values cannot be imported. Add the server manually with the required content, or use an environment variable or credential command for header and environment values. |
| [pi](https://github.com/earendil-works/pi) | `~/.pi/agent/mcp.json` (or under `PI_CODING_AGENT_DIR`) and `.pi/mcp.json` in the working folder. | Review "Available Tool Scope", "Loading Strategy" and "Initially Visible Tools" when tool-rule warnings appear. Values starting with `!` need manual credential-command setup. Servers added by pi extensions are not listed. |

Configurations supplied only by cloud services, plugins or session overrides may need to be added manually. Review the listed sources before importing: source application trust decisions do not restrict which configurations you can select in iCode.

### When a server is missing or cannot be imported

If the list is empty, check that the source application's MCP configuration exists for the current working folder. Installing the application alone does not create a server configuration. Unreadable files show an error above the list; invalid server entries may be omitted. Follow the file or field named in the error, or add the server manually.

For "Can't import" cards, expand the card and use "Source setting" and "Configuration Source" to locate the problem. After correcting the source file, reopen import to load it again.

If loading still fails, check `~/.chrys/logs/debug.log` on macOS/Linux or `%APPDATA%/chrys/logs/debug.log` on Windows.

### Move command values and headers helpers manually

Import does not run credential commands or headers helpers. To use one in iCode:

1. Install and authenticate your credential provider on this machine.
2. Add the server manually if its command-based values prevent import; otherwise edit the imported card.
3. Set the required environment value to `{{!tokenprovider gettoken}}`, or add an `Authorization` header with `Bearer {{!tokenprovider gettoken}}`. Replace the example command with your provider's command. If it prints the complete header value, omit the extra `Bearer` prefix.
4. Use absolute paths if the command needs files: iCode runs it from your home folder. It must exit successfully and print one nonempty line of ASCII text. See [Get an API key from a command](../configuration/models.md#get-an-api-key-from-a-command) for command requirements and refresh behavior.
5. Click "Test" before saving.

For Claude Code's `headersHelper` or Codex's `http_headers_helper`, configure each header separately with an environment variable or a command that prints only that header's value. Do not use a helper's JSON output as a header value.

## Configure instructions, tools, and request options

The following settings apply to both HTTP and STDIO servers.

### Server instructions and prompt templates

- **Include server instructions in model context (if available)**: Enabled by default for new configurations. When enabled, server instructions are included in the model context and may affect the agent's behavior.
- **Load server prompt templates as tools (if available)**: Enabled by default for new configurations. When enabled, prompt templates provided by the server are added to the model's tool list as callable tools.

### Tool access

- **Available Tool Scope**: Controls which server tools the agent may use. Choose "All server tools", "Only selected tools", or "No tools".

When selecting "Only selected tools", enter a list of tool names separated by `,`. If you do not yet know which tool names the server provides, select "All server tools" first, then follow the steps in [Test and save](#test-and-save) to view the tool names in the connection report.

### Tool loading

- **Loading Strategy**: Controls when tools become available to the agent. With "Full — load all available tools", every tool within "Available Tool Scope" is available at the start of a task. With "On demand — load tools as needed", only the tools in "Initially Visible Tools (optional)" are available at the start, and the agent can load other tools within "Available Tool Scope" as needed. If a server provides many tools but only a few are used regularly, on-demand loading can reduce the tool information sent to the model at the start of a task.
- **Initially Visible Tools (optional)**: With "On demand — load tools as needed", enter an optional list of tool names separated by `,`. These tools must be included in "Available Tool Scope".

For example:

- If a server provides only `search` and `read_file`, and both are used frequently, choose "Full — load all available tools".
- If a server provides dozens of tools but you usually need only `search` and `read_file`, choose "On demand — load tools as needed" and enter these two tools in "Initially Visible Tools (optional)".

With Claude Opus 5.5, Fable 5.1 and Sonnet 5.5, on-demand loading has a cost. These models tie the thinking they return to the conversation before it, including the tools available to the agent at that point. When the agent loads or unloads a tool, or the next task starts again with only the initially visible tools, that changes, and the model service may leave the earlier thinking out or refuse to read it back. If the service refuses, iCode sends the request once more without the earlier thinking, which costs one extra request. Whether the service leaves the thinking out or iCode resends without it, the model no longer sees that earlier reasoning. With `thinking_block_binding: error`, iCode reports the refusal instead of resending. For the settings involved, see [Claude thinking settings](../configuration/models.md#claude-thinking-settings).

### Naming and limits

- **Tool Name Prefix**: Adds a prefix to this server's tool names, joining the prefix and original name with `_`. For example, the prefix `github` displays the tool `search` as `github_search`. Use a prefix to avoid conflicts when different servers or built-in tools share the same tool name.
- **MCP Request Timeout (seconds)**: Limits how long connection setup and each MCP request can wait. There is no separate startup timeout. Leave blank to use the default shown in the interface.

## Test and save

1. **Enable or disable the server**: To enable an MCP server, keep "Enabled" checked at the bottom left of its configuration card. To keep the configuration but temporarily disable the server, uncheck it. A disabled server does not provide tools, prompt templates, or usage instructions to the agent.
2. **Test the server connection**: Click "Test" at the bottom right of the server configuration card. iCode attempts to connect and displays the server's identity, advertised capabilities, and usage instructions in a connection report, along with the tools and prompt templates available under the current configuration.
3. **Save the configuration**: Click "Save". If you are editing the agent used by the current session, the saved MCP configuration applies to subsequent requests.

If the test fails:

- For HTTP servers, check the URL, authentication headers, environment variables, and proxy and TLS settings.
- For STDIO servers, first inspect the executable, working directory, process exit code, and tail of standard error shown in the connection error. Then check the command, arguments, and environment variables, and confirm that the command can start in the indicated working directory. iCode shows only a limited amount of standard error output. For complete logs, enable logging as described in the server's documentation. Remove sensitive information such as tokens and credentials before sharing diagnostics.

## Reconnect with /reload

`/reload` tries a new connection to each enabled MCP server of the current agent and its sub-agents, while the existing connections stay open. The next request uses the tools, prompt templates, and usage instructions each server now reports. Every server has its own result, so a failure on one doesn't affect the others:

| The new connection | What happens | In the summary |
| --- | --- | --- |
| Succeeds | iCode switches to it and closes the old connection | Nothing for this server |
| Fails, the server's configuration is unchanged, and the old connection still responds | iCode keeps using the old connection | `reconnect failed, using the existing connection`, with the reason |
| Fails, and the configuration changed, for example the address, a token, or "Available Tool Scope" | The server is unavailable, even if the old connection still works, so nothing keeps the old token's or scope's access | `is unavailable`, with the reason |
| Fails, and the old connection doesn't respond or there was none | The server is unavailable | `is unavailable`, with the reason |

The reason names only the kind of failure, such as a refused connection or rejected credentials. It never repeats what the server said, because that text can hold a credential; the server's own logs have the details.

An unavailable server's tools are removed from the agent; fix the problem and run `/reload` again. A server you removed or disabled loses its tools and its connection closes, with no reconnect attempt.

Two results are normal:

- A successful reconnect starts a new session on the server, so remote state such as an open browser page or a login can be reset.
- A STDIO server that holds a port or file exclusively may not be able to start beside its old instance, so every reconnect can fail. While the configuration is unchanged and the old connection responds, the summary keeps saying that iCode is using the existing connection.

Saving in the settings or agent dialogs doesn't force a reconnect: a server whose configuration didn't change keeps its connection.

## Verify MCP tool calls

Close the agent configuration window and submit a low-risk task that needs one of the server's tools. For example, if the server provides a read-only search tool, ask the agent to search for a specific piece of data.

The conversation should show the corresponding MCP tool call card and return data from the server. If an "Approval Required" dialog appears, check the tool name and arguments before deciding whether to approve. For approval modes, see [Configure approval modes](../configuration/approval.md).

If a tool returns an image, audio clip, or file that iCode can't decode, the agent receives a short note in place of that item, along with the rest of the result. A link the tool returns reaches the agent as text (its name, address, and description).

If the connection test succeeds but tools are unavailable in the conversation, check that the server is enabled, that the tools appear in the connection report, and that they are included in "Available Tool Scope".
