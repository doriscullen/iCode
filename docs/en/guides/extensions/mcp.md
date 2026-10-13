# Connect MCP servers

Model Context Protocol (MCP) servers can provide iCode agents with external tools, prompt templates, and usage instructions. This guide explains how to connect HTTP or STDIO MCP servers in the terminal user interface (TUI), configure which tools are available, and control whether prompt templates are loaded and server instructions are included in the model context.

Before connecting an MCP server, confirm that its source is trustworthy. The server may receive tool call arguments and access local files, network services, or account data. Its prompt templates and usage instructions may also affect the agent's behavior.

## Add an MCP server configuration

Enter `/agents mcp` in the input field to open the current agent's "MCP" tab. Added server configurations apply only to the current agent; they are not automatically applied to other agents.

Each server has its own card. Saved servers are collapsed and show only their name; click the name to expand or collapse a card. If saving finds a problem, the card with the problem expands. To delete a server, click "✕" on its card and confirm.

Click "+ Add", then fill in the following fields in the new card at the top of the list:

1. Enter a "Server Name". This name identifies the server in the current agent's MCP configuration. Server names must be unique within an agent's MCP configuration, and are case-insensitive.
2. Optionally enter a "Description" to note the server's purpose. This description is not provided to the agent as server instructions.

HTTP header values and STDIO environment variable values can both use `{{ENV_VAR}}` to reference environment variables set before starting iCode. Variable names can contain only letters, digits, and underscores, and cannot start with a digit.

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
2. In "Command", enter the program name or path used to start the server, along with any required arguments. The TUI splits this line and saves it into the `command` and `args` fields in the agent YAML. When editing YAML by hand, put the executable in `command` and each argument in `args` as a separate item.
3. Add any environment variables the process needs. Enter values directly or use `{{ENV_VAR}}` to reference environment variables set before starting iCode.

An STDIO connection starts the specified program on the local machine. If the startup command downloads or runs third-party packages or scripts, confirm that their sources are trustworthy and review the relevant content first.

## Import servers from other agents

If you already set up MCP servers in another agent, you can copy them into the current iCode agent instead of typing them again. Claude Code is supported.

1. On the "MCP" tab, click "Import from other agents" next to "+ Add".
2. The left side of the window lists the supported agents iCode found on this computer. Click one to see the MCP servers it has configured on the right. Each server is shown as a card with the settings it will bring: the command it runs or the URL it connects to, and its environment variables or headers with their values.
3. Tick the servers you want (click the checkbox or the server name), then click "Import" at the bottom right. You can tick servers from more than one agent before you import.

For Claude Code, iCode lists the servers Claude Code uses, labeled as Claude Code labels them:

- "user": servers you added for all projects, kept in `~/.claude.json` (or the file of that name in the folder `CLAUDE_CONFIG_DIR` names).
- "local": servers you added only for one folder, kept in the same file. If the working folder is inside a Git repository, these are the servers you added for the repository's top folder, which Claude Code uses in every folder of the repository. Otherwise they are the servers you added for the working folder itself.
- "project": servers in the `.mcp.json` file of the working folder or of any folder above it. Tools that install a server "just for this project" put it there.

A "local" or "project" card shows the folder Claude Code uses the server in, and a note at the top of the list reminds you that iCode uses imported servers in every project. iCode starts a STDIO server in the current working folder, so a server that works on the project it is started in keeps doing so in each project.

Entries Claude Code itself doesn't load, such as a URL with no `type` or an incomplete command, are not listed.

Imported servers appear as new, expanded cards at the top of the list. Their "Description" says which agent they came from, followed by any warnings from the import, so you can still read them after the import window closes. Delete those lines once you have dealt with them. Nothing is saved yet: check the cards, click "Test" if you like, then click "Save".

What to know about imported servers:

- Values are copied as they are, including tokens and passwords in environment variables and headers. iCode saves them in the agent file, which only your user account can read.
- An environment variable reference such as `${GITHUB_TOKEN}` is changed to iCode's form, `{{GITHUB_TOKEN}}`. iCode has no default values: in `${HOST:-localhost}`, the default `localhost` is dropped, and a warning names the setting and the variable. The warning doesn't repeat the default, so a token used as one isn't copied into the description. Set the variable before starting iCode, or the connection fails.
- Claude Code sets `CLAUDE_PROJECT_DIR` to the project folder for the STDIO servers it starts; iCode doesn't. A warning names each environment variable or header that references it: set the variable before starting iCode, or write the folder path instead.
- Claude Code never sends a few of its own credential variables, such as `ANTHROPIC_API_KEY`, to a remote server, even when a header names them. iCode has no such rule and would send the real value, so a warning points out each header that references one of them. Remove the header, or copy the credential into a variable with another name.
- The server's per-call timeout is kept: Claude Code's `timeout` in milliseconds becomes iCode's "MCP Request Timeout" in seconds, rounded up. Values below one second are ignored, as Claude Code ignores them. An HTTP server with no `timeout` but a `request_timeout_ms` uses that value instead, at most five minutes, as Claude Code does.
- Settings iCode has no equivalent for are not imported, and a warning names them. For Claude Code these are the headers helper command (iCode never runs it) and OAuth sign-in (iCode only sends the headers you enter); add the headers yourself. Any other setting iCode doesn't recognize is listed too.
- Some servers can't be imported: those using a connection type other than STDIO or HTTP (such as SSE or WebSocket), those whose URL or command uses an environment variable, because iCode reads those fields as written, and those with a brace right next to an environment variable in an environment variable or header value (such as `{${TOKEN}}`), which iCode can't write the same way. Their cards are greyed out with a dashed border and say why. The `streamable-http` type is read as HTTP.
- A server that the current agent already has, with the same command and arguments or the same URL, is listed last, already ticked and locked, even if its name differs. If an imported server has the same name as another server, "Save" reports it; rename one of them.
- The same server may be listed more than once, for example as both a "user" and a "project" server, or in two agents. Once you tick one, the others with the same command and arguments or the same URL are locked, and their cards say which one is ticked, so the server is imported only once. Untick it to choose another.

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
- **MCP Request Timeout (seconds)**: Limits how long a single MCP request can wait. Leave blank to use the default shown in the interface.

## Test and save

1. **Enable or disable the server**: To enable an MCP server, keep "Enabled" checked at the bottom left of its configuration card. To keep the configuration but temporarily disable the server, uncheck it. A disabled server does not provide tools, prompt templates, or usage instructions to the agent.
2. **Test the server connection**: Click "Test" at the bottom right of the server configuration card. iCode attempts to connect and displays the server's identity, advertised capabilities, and usage instructions in a connection report, along with the tools and prompt templates available under the current configuration.
3. **Save the configuration**: Click "Save". If you are editing the agent used by the current session, the saved MCP configuration applies to subsequent requests.

If the test fails:

- For HTTP servers, check the URL, authentication headers, environment variables, and proxy and TLS settings.
- For STDIO servers, first inspect the executable, working directory, process exit code, and tail of standard error shown in the connection error. Then check the command, arguments, and environment variables, and confirm that the command can start in the indicated working directory. iCode shows only a limited amount of standard error output. For complete logs, enable logging as described in the server's documentation. Remove sensitive information such as tokens and credentials before sharing diagnostics.

## Verify MCP tool calls

Close the agent configuration window and submit a low-risk task that needs one of the server's tools. For example, if the server provides a read-only search tool, ask the agent to search for a specific piece of data.

The conversation should show the corresponding MCP tool call card and return data from the server. If an "Approval Required" dialog appears, check the tool name and arguments before deciding whether to approve. For approval modes, see [Configure approval modes](../configuration/approval.md).

If a tool returns an image, audio clip, or file that iCode can't decode, the agent receives a short note in place of that item, along with the rest of the result. A link the tool returns reaches the agent as text (its name, address, and description).

If the connection test succeeds but tools are unavailable in the conversation, check that the server is enabled, that the tools appear in the connection report, and that they are included in "Available Tool Scope".
