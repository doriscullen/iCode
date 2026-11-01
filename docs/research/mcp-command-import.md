# MCP Import Command Migration

Checked 2026-11-01. This note records the execution contracts behind pi command
values and Claude Code/Codex header helpers. It supports import decisions; it is
not a proposal to run external commands during discovery.

## Sources

- Pi 1.4.0, immutable commit
  [`4a080252f06caf63a525d7873c5d94d3c23887e0`](https://github.com/earendil-works/pi/tree/4a080252f06caf63a525d7873c5d94d3c23887e0):
  [value resolver](https://github.com/earendil-works/pi/blob/4a080252f06caf63a525d7873c5d94d3c23887e0/packages/coding-agent/src/core/resolve-config-value.ts),
  [MCP runtime](https://github.com/earendil-works/pi/blob/4a080252f06caf63a525d7873c5d94d3c23887e0/packages/coding-agent/src/extensions/mcp/runtime.ts),
  [shell selection](https://github.com/earendil-works/pi/blob/4a080252f06caf63a525d7873c5d94d3c23887e0/packages/coding-agent/src/utils/shell.ts).
- Codex rust-v0.166.0, immutable commit
  [`6ff1e1ca3b30fc8b914720681fcf5fe62e95f856`](https://github.com/openai/codex/tree/6ff1e1ca3b30fc8b914720681fcf5fe62e95f856):
  [HTTP header helper](https://github.com/openai/codex/blob/6ff1e1ca3b30fc8b914720681fcf5fe62e95f856/codex-rs/rmcp-client/src/http_headers.rs),
  [MCP runtime context](https://github.com/openai/codex/blob/6ff1e1ca3b30fc8b914720681fcf5fe62e95f856/codex-rs/codex-mcp/src/runtime.rs),
  [subprocess environment](https://github.com/openai/codex/blob/6ff1e1ca3b30fc8b914720681fcf5fe62e95f856/codex-rs/rmcp-client/src/utils.rs).
- Claude Code official [dynamic headers reference](https://code.claude.com/docs/en/mcp#use-dynamic-headers-for-custom-authentication),
  including its "Where the helper runs", "Which variables a helper can read" and
  "Trust a folder before its headersHelper runs" sections. The locally cached
  official page identifies its modification time as 2026-10-28. The importer
  baseline is Claude Code 2.1.316, with the underlying rules initially checked on
  2.1.302. The documentation is a live reference, not an immutable source release;
  this note does not claim to have independently rerun the older binary.
- iCode at merge commit `e05c4ddc`: `service/command_credentials.py`,
  `service/prompt_shortcuts/shell.py`, `foundation/util/env_templates.py`, and
  `service/mcp/{_connection,_http_transport,_stdio_transport}.py` under
  `src/chrys/`.

## Pi Command Values

Only a value that starts with `!` is a command. The complete remainder becomes
the shell command; it is not passed through pi's `$NAME`/`${NAME}` configuration
template parser. That shell can expand its own environment references. A value
starting with `$!` becomes a literal exclamation mark, not a command. See
`parseConfigValueReference`, `parseConfigValueTemplate`, and
`executeCommandUncached` in the pinned resolver.

| Property | Pi 1.4.0 MCP behavior |
| --- | --- |
| Shell on POSIX | Node `execSync` default shell, normally `/bin/sh` |
| Shell on Windows | pi's Bash selection; fall back to Node's default shell if the configured shell cannot be started |
| Working directory | pi process working directory; resolver supplies no `cwd` option |
| Environment | pi process environment; resolver supplies no replacement `env` |
| Streams | stdin ignored except the legacy WSL shell command transport; stdout captured; stderr ignored |
| Time limit | 10 seconds |
| Output | UTF-8 stdout, trimmed, must be nonempty; no one-line ASCII restriction in the resolver |
| Failure | start, timeout, nonzero exit, or empty stdout gives no value; the MCP throwing API fails transport creation |
| Refresh | command runs uncached each time the MCP transport is created |

The last row matters: `resolveConfigValue` has a process-lifetime cache, but MCP
does not use that path. `createDefaultTransport` calls `resolveHeadersOrThrow`
and `resolveConfigValueOrThrow`; those call `resolveConfigValueUncached`.
Do not describe MCP credentials as using the generic lifetime cache.

The stdio server's own `config.cwd` is applied after resolving its environment.
It changes the server's working directory, not the credential command's. See
the pinned MCP runtime's `createDefaultTransport`.

## Header Helpers

Both helper interfaces return a JSON object of string header names and values.
That object is a collection of headers, not one credential string. Its keys
cannot be known without running the helper. See Claude's "Requirements" and
Codex's `RawHeaderEntries`/`parse_helper_output`.

| Property | Claude Code `headersHelper` | Codex `http_headers_helper` |
| --- | --- | --- |
| Shell | A shell, per official reference | `sh -c`; Windows `COMSPEC` or `cmd.exe` with `/Q /D /C` |
| Time limit | 10 seconds | 10 seconds |
| Output | JSON object of strings | Trimmed UTF-8 JSON object of strings, at most 64 KiB; duplicate and reserved headers rejected |
| Working directory | Depends on declaring scope | Local process cwd supplied by runtime context |
| Environment | Extra MCP name/URL variables; scope-dependent credential filtering | Cleared, then MCP subprocess allowlist and custom CA settings; arbitrary ambient credentials excluded |
| Initial fetch | Fresh on each connection, including reconnect | First same-origin request; result shared and cached for that HTTP client |
| Refresh | 401/403 tool failure reruns helper, reconnects and retries once | Same-origin POST 401/403 reruns helper; OAuth insufficient-scope exception; retries once only if effective headers changed |
| Header precedence | Helper overrides matching static headers | Helper overrides matching static headers except explicit Authorization wins |

Claude's cwd is the configuration directory for user-scope servers, declaring
project directory for project/local servers, plugin root for plugin servers,
or primary session cwd for SDK/`--mcp-config` and certain inline sources. The
official page also documents trust gating for project/local helpers and removal
of credential-like environment variables for repository/plugin-supplied helpers.
Neither policy is reproduced by merely wrapping the command in a scalar template.

Codex limits helper application to the configured server origin, stops redirects,
and preserves OAuth challenges if a refresh fails or changes nothing. Its helper
receives `create_env_for_mcp_server(None, &[])`, not per-server `env` or arbitrary
ambient environment. On Unix its base list is HOME, LOGNAME, PATH, SHELL, USER,
`__CF_USER_TEXT_ENCODING`, LANG, LC_ALL, TERM, TMPDIR, TZ; custom CA variables are
added and non-inheritable credentials are removed. See `run_helper`,
`HttpHeadersClient`, and `create_env_for_mcp_server` in the pinned sources.

## Comparison With iCode

iCode scalar `{{!command}}` runs in the user's shell (`SHELL`, with `/bin/sh`
fallback; Windows command shell), from the user's home directory, with
`build_shell_subprocess_env`. Its limit is 30 seconds and 64 KiB; output is
trimmed and must be one nonempty line of printable ASCII. Command handles are
registered synchronously, but execution happens when credentials are used.

HTTP values refresh after the configured TTL and after a 401; an unsuccessful
refresh can retain an earlier value. STDIO values refresh at process launch and
remain fixed for that child. These contracts come from the iCode source paths
listed above. They differ from pi and both helper interfaces.

Literal `{{!command}}` text from another application's ordinary string is not a
request to use iCode commands. Importers must retain brace escaping through YAML
save/load and MCP tool creation. They must not execute it or register a command
handle while interpreting an escaped literal.

## Migration Decision

General automatic conversion of pi `!command` to iCode `{{!command}}` cannot be
called semantics-preserving. Even when stdout happens to be a suitable token,
cwd, shell selection, environment, timeout, refresh timing, and failure behavior
change. Determining that a shell command is independent of those details is not
a reliable static import operation. A command whose delimiter text cannot be
represented by the iCode parser also needs explicit handling.

Keep these values unsupported by automatic import unless the product explicitly
offers a conversion that explains and accepts those changed semantics. Manual
conversion is suitable for a user-owned, repeatable credential command adapted
to iCode's runtime contract. It should use an absolute script path, handle its
own working directory and dependencies, print only a single ASCII token, and
keep secrets in stdout rather than command arguments.

A JSON header helper cannot be put directly in one `{{!command}}` value: it would
send the JSON object as that header's value and lose its other headers. Even
splitting a helper into independent per-header commands changes atomic refresh,
key additions/removals, execution count, header precedence and retry behavior.
Retain a specific helper-not-migrated warning for Claude and Codex. Guide users
to configure the required Headers manually or adapt the helper into explicit
single-value commands. Do not copy a helper's resolved secrets into saved YAML.

Full helper migration would need a dedicated header-map runtime contract,
source-aware cwd/environment policy, header validation/precedence, coordinated
refresh, redirect policy and persistence/cache-key support. It is separate from
scalar command syntax. The presence of `{{!command}}` alone does not provide it.

Import warnings should say "not automatically migrated" rather than "iCode does
not run commands". They should name settings without embedding command text or
output, and point to the scalar-command instructions where manual adaptation
fits. None of these findings requires execution during discovery or weakening
the existing literal escaping.
