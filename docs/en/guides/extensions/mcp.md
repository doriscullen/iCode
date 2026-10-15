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

## Import servers from other applications

If you already set up MCP servers in another application, you can copy them into the current iCode agent instead of typing them again. Claude Code, Codex, opencode and pi are supported.

1. On the "MCP" tab, click "Import from other applications" next to "+ Add".
2. The left side of the window lists the supported agents iCode found on this computer. Click one to see the MCP servers it has configured on the right. Each server is shown as a card with the settings it will bring, laid out as on the "MCP" tab: the command it runs or the URL it connects to, and its environment variables or headers, names on the left and values on the right. Click ▶/▼ before a server's checkbox to fold or unfold its card.
3. Tick the servers you want (click the checkbox or the server name), then click "Import" at the bottom right. You can tick servers from more than one agent before you import.

Each card is labeled with where the agent keeps the server, such as "user" or "project". A server the agent uses only in one folder shows that folder on its card, and a note at the top of the list reminds you that iCode uses imported servers in every project. iCode starts a STDIO server in the current working folder, so a server that works on the project it is started in keeps doing so in each project.

Imported servers appear as new, expanded cards at the top of the list. Their "Description" says which agent they came from, followed by any warnings from the import, so you can still read them after the import window closes. Delete those lines once you have dealt with them. Nothing is saved yet: check the cards, click "Test" if you like, then click "Save".

What to know about imported servers, whichever agent they come from:

- Values are copied as they are, including tokens and passwords in environment variables and headers. iCode saves them in the agent file, which only your user account can read.
- Environment variable references are changed to iCode's form, `{{NAME}}`. Set each variable before starting iCode: iCode fails the connection when one is not set.
- iCode never signs in to a server with OAuth: it only sends the headers you enter. If the other agent signed in to a server for you, add an `Authorization` header yourself.
- Settings iCode has no equivalent for are not imported, and a warning names them, such as a working folder for the server (iCode always starts it in the current working folder).
- Some servers can't be imported, for example those whose URL or command uses an environment variable, because iCode reads those fields as written, and those with a brace right next to an environment variable in an environment variable or header value (such as `{${TOKEN}}`), which iCode can't write the same way. Their cards are greyed out with a dashed border and folded to their name; click ▶ to unfold one and see its settings and why it can't be imported.
- A server that the current agent already has, with the same command and arguments or the same URL, is listed last, already ticked, locked and folded, even if its name differs. If an imported server has the same name as another server, "Save" reports it; rename one of them.
- The same server may be listed more than once, for example as both a "user" and a "project" server, or in two agents. Once you tick one, the others with the same command and arguments or the same URL are locked, and their cards say which one is ticked, so the server is imported only once. Untick it to choose another.
- If a configuration file can't be read, or the agent itself would reject its MCP settings, a message at the top of the list names the file, and its servers are not listed.

### Claude Code

iCode lists the servers Claude Code uses, labeled as Claude Code labels them:

- "user": servers you added for all projects, kept in `~/.claude.json` (or the file of that name in the folder `CLAUDE_CONFIG_DIR` names).
- "local": servers you added only for one folder, kept in the same file. If the working folder is inside a Git repository, these are the servers you added for the repository's top folder, which Claude Code uses in every folder of the repository. Otherwise they are the servers you added for the working folder itself.
- "project": servers in the `.mcp.json` file of the working folder or of any folder above it. Tools that install a server "just for this project" put it there.

Entries Claude Code itself doesn't load, such as a URL with no `type` or an incomplete command, are not listed. Also:

- `${GITHUB_TOKEN}` becomes `{{GITHUB_TOKEN}}`. iCode has no default values: in `${HOST:-localhost}`, the default `localhost` is dropped, and a warning names the setting and the variable. The warning doesn't repeat the default, so a token used as one isn't copied into the description.
- Claude Code sets `CLAUDE_PROJECT_DIR` to the project folder for the STDIO servers it starts; iCode doesn't. A warning names each environment variable or header that references it: set the variable before starting iCode, or write the folder path instead.
- Claude Code never sends a few of its own credential variables, such as `ANTHROPIC_API_KEY`, to a remote server, even when a header names them. iCode has no such rule and would send the real value, so a warning points out each header that references one of them. Remove the header, or copy the credential into a variable with another name.
- The server's per-call timeout is kept: Claude Code's `timeout` in milliseconds becomes iCode's "MCP Request Timeout" in seconds, rounded up. Values below one second are ignored, as Claude Code ignores them. An HTTP server with no `timeout` but a `request_timeout_ms` uses that value instead, at most five minutes, as Claude Code does.
- The headers helper command and OAuth settings are not imported, with a warning; add the headers yourself. Any other setting iCode doesn't recognize is listed too.
- SSE and WebSocket servers can't be imported: iCode supports only STDIO and HTTP servers. The `streamable-http` type is read as HTTP.

### Codex

The importer follows [Codex 0.162.1](https://github.com/openai/codex/tree/rust-v0.162.1), using its source code and tests as the reference.

- "user": servers under `[mcp_servers]` in `~/.codex/config.toml`, or `config.toml` in the folder `CODEX_HOME` names. A relative `CODEX_HOME` is resolved from the working folder.
- "project": `.codex/config.toml` from the project root down to the working folder, with the nearer file taking precedence. The root is the nearest folder containing a `project_root_markers` entry (default: `.git`); without one, only the working folder is searched. Tables merge by key, including environment variables and headers; arrays replace earlier arrays. A warning names the other files contributing to a server.

Project trust is read from the user file's `[projects]` entries. A folder's own decision takes precedence over the project root's, then the Git root's. Project servers without a trusted decision are shown but can't be imported. They do not override user servers, and a project file cannot grant itself trust.

Also:

- `command` selects STDIO and `url` selects HTTP. Both together are invalid, including when they come from different merged files. Invalid server entries are not listed; unreadable or malformed TOML files show an error.
- `env` and `http_headers` values remain literal. `env_vars`, `env_http_headers` and `bearer_token_env_var` become `{{VARIABLE}}` references; their current secret values are not copied. Explicit environment values override forwarded variables; environment headers override static headers, and bearer auth overrides an Authorization header. Codex can skip optional references when a variable is unavailable; iCode requires a nonempty value, so these references have a warning.
- Disabled servers stay disabled. `tool_timeout_sec` becomes "MCP Request Timeout", rounded up to seconds, defaulting to 300 seconds in this Codex version. A zero timeout cannot be preserved and is reported as omitted. iCode also uses its request timeout for connection setup; Codex's separate startup timeout is not carried over.
- `enabled_tools` becomes the allowed tools list, with `disabled_tools` removed. If there is only a deny list, the server is imported with **no tools**, with a warning: choose the allowed tools yourself under "Available Tool Scope".
- A custom `cwd`, OAuth credentials and settings, ChatGPT/enterprise sign-in, header helper commands, approval rules and other settings without an iCode equivalent are not carried over. Warnings name the omitted settings. No helper commands are run. Remote execution environments and remote-sourced variables cannot be imported as local servers.

The importer reads local user and project files only. It does not read system, managed or cloud settings, selected profile files, session overrides, plugin servers, stored credentials, or the main checkout's trust for linked Git worktrees. Explicit variables on Codex's non-inheritable list are omitted with a warning. iCode still uses its own subprocess environment inheritance rules.

### opencode

opencode combines the settings of all its configuration files, later files winning, and iCode lists the servers as opencode ends up with them:

- "global": `opencode.json`, `opencode.jsonc` and `config.json` in `~/.config/opencode` (or in `opencode` under `XDG_CONFIG_HOME`), the file `OPENCODE_CONFIG` names, the `~/.opencode` folder, the folder `OPENCODE_CONFIG_DIR` names, the settings in `OPENCODE_CONFIG_CONTENT`, and the settings your administrator installed for all users.
- "project": `opencode.json` and `opencode.jsonc` in the working folder and in each folder above it, and in their `.opencode` folders, up to the top folder of the Git repository (outside a repository, up to the top of the disk). They are skipped when `OPENCODE_DISABLE_PROJECT_CONFIG` is set, as opencode skips them.

When more than one file changes a server, for example a project file that turns off a server from the global file, the card shows the result and a warning names the other files. If those files add headers or environment variables to a server whose URL or command comes from another file, the warning names them too: they are sent to that server. Settings opencode gets from the internet, such as your organization's, are not read.

Also:

- `{env:GITHUB_TOKEN}` becomes `{{GITHUB_TOKEN}}`. opencode uses an empty value for a variable that is not set; iCode fails the connection instead, so a warning names each variable that is not set when you import.
- A command or URL containing an environment reference can't be imported. A server whose entire URL is `{env:MCP_URL}` or `{file:url.txt}` is still listed, with the reason it can't be imported.
- A server whose command, URL, environment variables or headers read a file with `{file:...}` can't be imported: iCode doesn't read files for values. Put the value in an environment variable and reference that in opencode instead, or add the server by hand.
- A server opencode has turned off (`"enabled": false`) is imported turned off. Tick "Enabled" in its card to use it.
- `timeout` in milliseconds becomes "MCP Request Timeout" in seconds, rounded up; without it, `experimental.mcp_timeout` is used. iCode also waits at most that long to connect.
- If opencode hides all of a server's tools with `tools` or `permission` (for example `"tools": {"github_*": false}`), the server is imported with no tools, or only with the tools a later rule turns back on by name, and a warning says so. Rules for single tools that iCode can't apply, and rules set for one opencode agent only, are named in a warning; set "Available Tool Scope" yourself.
- opencode also connects to a remote server over SSE when it doesn't support HTTP; iCode doesn't.

### pi

iCode lists the servers pi uses in the working folder:

- "global": `mcp.json` in `~/.pi/agent`, or in the folder `PI_CODING_AGENT_DIR` names.
- "project": `.pi/mcp.json` in the working folder itself. A project server replaces a global server of the same name. A project entry that only turns a global server on or off, or changes how its tools are offered, changes that global server, and a warning names the project file.

pi reads project servers only when it trusts the folder. If you told pi not to trust the working folder, or pi is set never to trust a new folder (`"defaultProjectTrust": "never"`), the project servers are listed but can't be imported. If pi hasn't asked yet, they are listed as usual: you decide here.

pi skips a configuration file with a comment, a trailing comma or a byte order mark at the start, and iCode reports such a file. Also:

- `${GITHUB_TOKEN}` and `$GITHUB_TOKEN` become `{{GITHUB_TOKEN}}`, and `$$` becomes `$`. A value starting with `!` is the output of a command pi runs; iCode doesn't run commands for values, so such a server can't be imported.
- Text pi treats literally, such as `${TOKEN:-default}` and literal braces in environment variables or headers, is preserved. pi doesn't support defaults in variable references.
- `~` at the start of the command or an argument becomes your home folder.
- A server pi has turned off is imported turned off. `timeout` in seconds is rounded up. The server's `description` comes first in the imported description.
- How pi offers the server's tools becomes iCode's "Loading Strategy" and "Available Tool Scope": `codemode` (pi's default) and `deferred` become "On demand — load tools as needed", `direct` becomes "Full — load all available tools", and `hidden` becomes "No tools". In `toolExposure`, tools named one by one are added to the tool lists where iCode can express it; patterns such as `"write_*"` are named in a warning.
- If pi is set not to turn on codemode (`"autoEnableCodemode": false`), pi can't call the tools of a `codemode` server unless you turn codemode on yourself; a warning says so. iCode loads them on demand.
- OAuth settings and `auth` (pi sending the token of a service you logged in to with `/login`) are not imported, with a warning; add an `Authorization` header yourself. Servers that pi extensions add are not listed.

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
