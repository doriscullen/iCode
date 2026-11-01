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

Both can also use what a command prints, with `{{!command}}`, such as `Bearer {{!tokenprovider gettoken}}`. See [Get an API key from a command](../configuration/models.md#get-an-api-key-from-a-command) for what the command has to do. An HTTP server gets a new value while iCode runs. An STDIO server keeps the value it started with, and gets a new one only when iCode starts it again. If the command fails the first time, the server fails to connect.

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
3. Add any environment variables the process needs. Enter values directly or use `{{ENV_VAR}}` to reference environment variables set before starting iCode. Literal values retain leading and trailing whitespace. An empty value explicitly clears that variable for the server.

An STDIO connection starts the specified program on the local machine. If the startup command downloads or runs third-party packages or scripts, confirm that their sources are trustworthy and review the relevant content first.

## Import servers from other applications

If you already set up MCP servers in another application, you can copy them into the current iCode agent instead of typing them again. Claude Code, Codex, opencode and pi are supported.

Import keeps the server's connection settings, enabled state, and supported timeouts and tool restrictions. Agent-specific tool access rules and tool approval rules from the source application are not migrated. After import, iCode's approval mode and rules determine whether a tool call requires your confirmation; see [Configure approval mode](../configuration/approval.md).

Check the imported configuration and use "Test" before saving. Missing variables and connection failures are diagnosed when you test or use the server, rather than during import.

1. On the "MCP" tab, click "Import from other applications" next to "+ Add".
2. Select an application on the left to review its MCP servers on the right. Click ▶/▼ before a server's checkbox to fold or unfold its configuration.
3. Tick the servers you want (click the checkbox or the server name), then click "Import" at the bottom right. You can tick servers from more than one agent before you import.

Disabled servers stay disabled after import. Check "Scope in …" to see whether a server was configured for all projects or only one folder. Imported servers are available in every project that uses the current iCode agent. iCode starts STDIO servers in the current working folder; if a server needs a specific folder, adjust its command or arguments before use.

Each imported server gets a "Tool Name Prefix" based on its name to avoid tool name conflicts. For example, a server named `github` offers `search` as `github_search`. Names containing unsupported characters or exceeding the length limit are adjusted automatically. Check the generated prefix and change or clear it in the server's card if needed (see [Naming and limits](#naming-and-limits)).

After import, review the new server cards. Their "Description" includes the source application and any import warnings; resolve those warnings before use. Click "Test" to check the connection, then "Save" to keep the configuration.

What to know about imported servers, whichever agent they come from:

- Values are copied as they are, including tokens and passwords in environment variables and headers. iCode saves them in the agent file, which only your user account can read. Surrounding whitespace is removed from header values.
- Environment variable references are changed to iCode's form, `{{NAME}}`. Set each variable before starting iCode: iCode fails the connection when one is not set.
- OAuth settings and sign-in state are not imported. iCode does not manage the OAuth login or token refresh flow. If the server accepts a valid token you supply or supports another authentication method, configure authentication headers as it requires, for example `Authorization: Bearer <access-token>`. Replace an expired token if you entered it directly. You can also configure `Bearer {{!tokenprovider gettoken}}` if an external command obtains or refreshes the token; see [Move command values and headers helpers manually](#move-command-values-and-headers-helpers-manually).
- Settings iCode has no equivalent for are not imported, and a warning names them, such as a working folder for the server (iCode always starts it in the current working folder).
- Servers whose command or URL uses an environment variable cannot be imported. Some environment variable and header values also cannot be converted, such as `{${TOKEN}}`. Expand an unavailable server's card to read the reason, then adjust the source configuration or add the server manually.
- Servers already configured in the current agent cannot be selected again. Servers with different environment variables, headers, enabled states or tool scopes can be imported separately even if their command or URL is the same. If "Save" reports duplicate names, rename one of the servers.
- The same server may be listed more than once, for example as both a "user" and a "project" server, or in two agents. Once you tick one, other cards with the same settings are locked and say which one is ticked, so the server is imported only once. Untick it to choose another.
- If a configuration file can't be read, or the agent itself would reject its MCP settings, a message at the top of the list names the file, and its servers are not listed.

If no configuration is found, this means no supported application's MCP configuration was found, even if the application is installed. Files must not exceed 16 MiB; a JSON file must contain a JSON object at the top level. Check file permissions, size and format, fix the problem, then reopen import.

### Claude Code

iCode lists the servers Claude Code uses, labeled as Claude Code labels them:

- "user": servers you added for all projects, kept in `~/.claude.json` (or the file of that name in the folder `CLAUDE_CONFIG_DIR` names). If Claude Code's settings folder (`~/.claude`, or the folder `CLAUDE_CONFIG_DIR` names) holds a non-empty `.config.json` left from an older Claude Code, Claude Code reads that file instead, and so does iCode.
- "local": servers you added only for one folder, kept in the same file. If the working folder is inside a Git repository, these are the servers you added for the repository's top folder, which Claude Code uses in every folder of the repository. Otherwise they are the servers you added for the working folder itself.
- "project": servers in the `.mcp.json` file of the working folder or of any folder above it. Tools that install a server "just for this project" put it there.

Entries Claude Code itself doesn't load are not listed, such as a URL with no `type`, an incomplete command, a setting of the wrong type, or a name Claude Code keeps for its own servers, such as `workspace`. Also:

- `${GITHUB_TOKEN}` becomes `{{GITHUB_TOKEN}}`. Defaults such as `localhost` in `${HOST:-localhost}` are not imported. Set the variable before starting iCode, or replace the reference with the value you want.
- Variable references stay dynamic. Leading or trailing whitespace supplied by an HTTP header variable is not removed after expansion and may cause an `Illegal header value` connection error. Remove that whitespace from the variable before starting iCode.
- If a variable is not set, Claude Code passes the reference on as written, for example the text `${API_KEY}`, and warns about it in `claude mcp list`. iCode fails the connection instead, so set the variable before starting iCode.
- A server may depend on capabilities Claude Code provides beyond the imported configuration. iCode does not set `CLAUDE_PROJECT_DIR` or provide MCP roots. If a server needs that variable, even without referencing it in the configuration, set it before starting iCode. For a server that needs roots, check whether it can use the working folder or an explicitly configured folder instead.
- References to credential variables such as `ANTHROPIC_API_KEY` in remote server headers are replaced with an empty string, matching Claude Code's behavior. Other text is kept: `Bearer ${ANTHROPIC_API_KEY}` becomes `Bearer`, while a header containing only the removed reference is omitted. Check the authentication headers; if you need to send the credential, copy it into a variable with another name and reference that.
- Literal braces in environment variables and headers are escaped using iCode syntax. A note appears only when escaping occurs: `{{literal}}` appears as `{{{{literal}}}}` in the configuration, but the server still receives `{{literal}}`. This is literal text, not an environment reference.
- The server's per-call timeout is kept: Claude Code's `timeout` in milliseconds becomes iCode's "MCP Request Timeout" in seconds, rounded up. The preview shows the final value and the source milliseconds when the duration changes, for example 1501 milliseconds becomes 2 seconds. An entry without a configured timeout shows no explicit timeout. Values below one second are ignored, as Claude Code ignores them. An HTTP server with no `timeout` but a `request_timeout_ms` uses that value instead, at most five minutes, as Claude Code does.
- HTTP `toolPermissions` limits are not migrated. Check "Available Tool Scope" and approval rules before using the imported tools.
- Headers helpers, OAuth settings and sign-in state are not imported. If the server accepts a valid token you supply or supports another authentication method, configure the required authentication headers. iCode does not manage the OAuth login or token refresh flow. For command-based headers, see [Move command values and headers helpers manually](#move-command-values-and-headers-helpers-manually).
- SSE and WebSocket servers can't be imported: iCode supports only STDIO and HTTP servers. The `streamable-http` type is read as HTTP.
- iCode requires Streamable HTTP for remote servers. If "Test" fails, check whether the server offers a Streamable HTTP address; an SSE-only server cannot connect even if Claude Code lists it as HTTP.

### Codex

- "user": servers under `[mcp_servers]` in `~/.codex/config.toml`, or `config.toml` in the folder `CODEX_HOME` names. A relative `CODEX_HOME` is resolved from the working folder.
- "project": `.codex/config.toml` from the project root down to the working folder, with the nearer file taking precedence. The root is the nearest folder containing a `project_root_markers` entry (default: `.git`); without one, only the working folder is searched. When multiple files configure the same server, check the combined settings and the contributing files named on its card.

Codex's project trust decisions do not restrict import. You choose which configurations to copy into the current iCode agent; importing does not start a server or change Codex's trust settings.

Also:

- `command` selects STDIO and `url` selects HTTP. Both together are invalid, including when they come from different merged files. Invalid server entries are not listed; unreadable or malformed TOML files show an error.
- `env` and `http_headers` values remain literal. Most STDIO `env_vars` are supplied through iCode's inherited environment. Explicit `PYTHONPATH` and `PYTHONHOME` entries become `{{VARIABLE}}` references because iCode filters these variables from the inherited environment. A warning explains that these references require a nonempty value; set the variable before starting iCode, or remove the setting if it is unnecessary. `env_http_headers` and `bearer_token_env_var` also become variable references; their current secret values are not copied. Environment headers override static headers, and bearer auth overrides an Authorization header. Codex can skip an optional header when its variable is unavailable; iCode requires a nonempty value, so these headers have a warning.
- Disabled servers stay disabled. `tool_timeout_sec` becomes "MCP Request Timeout", rounded up to seconds, defaulting to 300 seconds in this Codex version. A zero timeout cannot be preserved and is reported as omitted. iCode also uses its request timeout for connection setup; Codex's separate startup timeout is not carried over.
- `enabled_tools` becomes the allowed tools list, with `disabled_tools` removed. If there is only a deny list, the server is imported with **no tools**, with a warning: choose the allowed tools yourself under "Available Tool Scope".
- A custom `cwd`, OAuth credentials and settings, ChatGPT/enterprise sign-in, approval rules and other settings without an iCode equivalent are not carried over. Warnings name the omitted settings. `http_headers_helper` has a separate warning with [manual migration instructions](#move-command-values-and-headers-helpers-manually). No helper commands are run during import. Remote execution environments and remote-sourced variables cannot be imported as local servers.

Only local user and project configuration files are imported. Servers or credentials supplied through system, managed or cloud settings, selected profiles, session overrides or plugins must be configured manually. Check any warnings about environment variables that were omitted.

### opencode

opencode combines the settings of all its configuration files, later files winning, and iCode lists the servers as opencode ends up with them:

- "global": `opencode.json`, `opencode.jsonc` and `config.json` in `~/.config/opencode` (or in `opencode` under `XDG_CONFIG_HOME`), the file `OPENCODE_CONFIG` names, the `~/.opencode` folder, the folder `OPENCODE_CONFIG_DIR` names, the settings in `OPENCODE_CONFIG_CONTENT`, and the settings your administrator installed for all users.
- "project": `opencode.json` and `opencode.jsonc` in the working folder and in each folder above it, and in their `.opencode` folders, up to the top folder of the Git repository (outside a repository, up to the top of the disk). They are skipped when `OPENCODE_DISABLE_PROJECT_CONFIG` is set, as opencode skips them.

When more than one file changes a server, for example a project file that turns off a server from the global file, the card shows the result and a warning names the other files. If those files add headers or environment variables to a server whose URL or command comes from another file, the warning names them too: they are sent to that server. Settings opencode gets from the internet, such as your organization's, are not read.

Also:

- `{env:GITHUB_TOKEN}` becomes `{{GITHUB_TOKEN}}`. References are preserved without checking current values; set the variables before testing or using the server.
- A command or URL containing an environment reference can't be imported. A server whose entire URL is `{env:MCP_URL}` or `{file:url.txt}` is still listed, with the reason it can't be imported.
- A server whose command, URL, environment variables or headers read a file with `{file:...}` can't be imported: iCode doesn't read files for values. Put the value in an environment variable and reference that in opencode instead, or add the server by hand.
- A server opencode has turned off (`"enabled": false`) is imported turned off. Tick "Enabled" in its card to use it.
- `timeout` in milliseconds becomes "MCP Request Timeout" in seconds, rounded up; without it, `experimental.mcp_timeout` is used. iCode also waits at most that long to connect.
- Global `tools`, `permission`, `permissions`, `OPENCODE_PERMISSION` and agent-specific permission rules are not migrated. Configure "Available Tool Scope" and approval rules in iCode.
- opencode enables OAuth by default unless `oauth` is `false`. The card warns that OAuth sign-in is not imported, even when the source configuration has no explicit `oauth` object. If the server accepts a valid token you supply or supports another authentication method, configure the required authentication headers. iCode does not manage the OAuth login or token refresh flow.
- opencode also connects to a remote server over SSE when it doesn't support HTTP; iCode doesn't.

### pi

iCode lists the servers pi uses in the working folder:

- "global": `mcp.json` in `~/.pi/agent`, or in the folder `PI_CODING_AGENT_DIR` names.
- "project": `.pi/mcp.json` in the working folder itself. A project server replaces a global server of the same name. A project entry that only turns a global server on or off, or changes how its tools are offered, changes that global server, and a warning names the project file.

pi's project trust decisions do not restrict import. You choose which configurations to copy into the current iCode agent; importing does not start a server or change pi's trust settings.

If a pi configuration file cannot be read, check that it is valid JSON without comments or trailing commas. Also:

- `${GITHUB_TOKEN}` and `$GITHUB_TOKEN` become `{{GITHUB_TOKEN}}`, and `$$` becomes `$`. A value starting with `!` is the output of a command pi runs. It is not automatically converted to iCode's `{{!command}}`, because the execution and refresh behavior differs; [add such a server manually](#move-command-values-and-headers-helpers-manually).
- Text pi treats literally, such as `${TOKEN:-default}` and literal braces in environment variables or headers, is preserved. pi doesn't support defaults in variable references.
- `~` at the start of the command or an argument becomes your home folder.
- A server pi has turned off is imported turned off. `timeout` in seconds is rounded up; a timeout so large that pi doesn't limit the wait at all becomes the longest timeout iCode allows. The server's `description` comes first in the imported description.
- How pi offers the server's tools becomes iCode's "Loading Strategy" and "Available Tool Scope": `codemode` (pi's default) and `deferred` become "On demand — load tools as needed", `direct` becomes "Full — load all available tools", and `hidden` becomes "No tools". In `toolExposure`, tools named one by one are added to the tool lists where iCode can express it; patterns such as `"write_*"` are named in a warning.
- Tool rules that cannot be imported follow the server's overall settings in iCode instead. Their availability or loading behavior may differ from pi; check "Available Tool Scope" and "Loading Strategy".
- OAuth sign-in is not imported, with a warning. iCode also does not support pi's `auth`, which uses the sign-in token of a service you logged in to with `/login` to authenticate with the MCP server. If authentication is required, configure other credentials supported by the server under "Headers". pi defaults to OAuth when neither an `Authorization` header nor `auth` is configured, so these servers also receive a warning. Servers that pi extensions add are not listed.

### Move command values and headers helpers manually

Import does not execute credential commands or headers helpers. Text the source treats as literal stays literal, including `{{!command}}` in an environment variable or header: importing and saving it does not turn it into an iCode credential command.

For a pi value such as `!tokenprovider gettoken`, add the server manually. Under Environment Variables, set the required variable to `{{!tokenprovider gettoken}}`. Under Headers, a token command can be used as `Authorization=Bearer {{!tokenprovider gettoken}}`. If the command prints the complete header value, use `{{!command}}` without adding another `Bearer` prefix.

Check these differences before clicking "Test":

- pi runs a value command from the folder pi started in, with a 10-second limit. iCode runs from your home folder with a 30-second limit. Shell selection and inherited environment can differ. Use absolute paths or a script that sets its own working folder when the command depends on a particular folder.
- iCode accepts one nonempty line of printable ASCII, trims surrounding whitespace, and limits captured output to 64 KiB. pi accepts trimmed UTF-8 output, including multiple lines. A command used by iCode must print only the required value and exit successfully. See [Get an API key from a command](../configuration/models.md#get-an-api-key-from-a-command).
- iCode refreshes HTTP command credentials after the configured interval or a 401 response. Restart an STDIO server to get a new value. If an HTTP refresh fails, the previous value may remain in use.

Claude Code's `headersHelper` and Codex's `http_headers_helper` return a JSON object containing multiple headers. Add each required header separately in iCode, using an environment variable or a command that prints only that header's value. Do not use a JSON-producing helper as `Authorization={{!command}}`: it would send the JSON as the header value. If the headers must be generated and refreshed together, continue using the helper in the source application.

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
