# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Tests for importing MCP servers configured in Claude Code."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any

import pytest

from chrys.service.mcp.importers import (
    IMPORT_SOURCES,
    ImportContext,
    ImportWarning,
    ImportWarningKind,
    McpImportCandidate,
    McpImportResult,
    McpSourceError,
    SourceErrorKind,
    UnsupportedReason,
)
from chrys.service.mcp.importers.claude_code import ClaudeCodeSource, _project_files
from chrys.service.profiles.agents.loader import load_profile_from_yaml
from chrys.service.profiles.agents.schema import AgentProfile, MCPServerConfig, ToolsConfig
from chrys.service.profiles.agents.serializer import save_profile
from tests.support.symlinks import symlink_or_skip


@pytest.fixture
def home(tmp_path: Path) -> Path:
    path = tmp_path / "home"
    path.mkdir()
    return path


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    path = tmp_path / "workspace"
    path.mkdir()
    return path


def _write_json(path: Path, document: Any) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(document), encoding="utf-8")
    return path


def _context(home: Path, workspace: Path | None = None, **environ: str) -> ImportContext:
    return ImportContext(workspace_cwd=None if workspace is None else str(workspace), home=home, environ=environ)


def _by_name(candidates: tuple[McpImportCandidate, ...]) -> dict[str, McpImportCandidate]:
    return {candidate.name: candidate for candidate in candidates}


def test_every_importer_is_registered_in_display_order() -> None:
    assert [source.source_id for source in IMPORT_SOURCES] == ["claude_code", "codex", "opencode", "pi"]


def test_detect_needs_a_claude_config_or_project_file(home: Path, tmp_path: Path) -> None:
    source = ClaudeCodeSource()
    workspace = tmp_path / "repo" / "app"
    workspace.mkdir(parents=True)

    # A .claude.json inside a project is not Claude Code's config: it never reads one.
    _write_json(workspace / ".claude.json", {"mcpServers": {"stray": {"command": "cmd"}}})
    assert source.detect(_context(home, workspace)) is False

    # A tool that installs a server "just for this project" leaves only a .mcp.json, maybe above the workspace.
    _write_json(tmp_path / "repo" / ".mcp.json", {"mcpServers": {}})
    assert source.detect(_context(home, workspace)) is True

    (tmp_path / "repo" / ".mcp.json").unlink()
    _write_json(home / ".claude.json", {})
    assert source.detect(_context(home, workspace)) is True


def test_discover_lists_local_then_project_then_user_servers(home: Path, tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    workspace = repo / "app"
    workspace.mkdir(parents=True)
    (repo / ".git").mkdir()
    # Inside a repository Claude Code keys local servers by its root, here spelled another way.
    local_key = str(workspace / "..")
    config = _write_json(
        home / ".claude.json",
        {
            "mcpServers": {"user-server": {"command": "user-cmd"}},
            "projects": {
                local_key: {"mcpServers": {"local-server": {"command": "local-cmd"}}},
                "/somewhere/else": {"mcpServers": {"other-project": {"command": "other"}}},
            },
        },
    )
    near = _write_json(workspace / ".mcp.json", {"mcpServers": {"near": {"command": "near-cmd"}}})
    # Claude Code reads every .mcp.json above the workspace, past the repository root [observed 2.1.302].
    far = _write_json(tmp_path / ".mcp.json", {"mcpServers": {"far": {"command": "far-cmd"}}})
    _write_json(workspace / ".claude.json", {"mcpServers": {"stray": {"command": "cmd"}}})

    result = ClaudeCodeSource().discover(_context(home, workspace))

    # Local and project servers name the one folder Claude Code uses them in.
    assert [(c.name, c.scope, c.source_path, c.scope_path) for c in result.candidates] == [
        ("local-server", "local", str(config), local_key),
        ("near", "project", str(near), str(workspace)),
        ("far", "project", str(far), str(tmp_path)),
        ("user-server", "user", str(config), ""),
    ]
    assert result.source_errors == ()


def test_same_named_servers_are_all_listed(home: Path, workspace: Path) -> None:
    # Claude Code would load one of them; the user picks.
    _write_json(home / ".claude.json", {"mcpServers": {"shared": {"command": "user-cmd"}}})
    _write_json(workspace / ".mcp.json", {"mcpServers": {"shared": {"command": "project-cmd"}}})

    result = ClaudeCodeSource().discover(_context(home, workspace))

    assert [(c.name, c.scope) for c in result.candidates] == [("shared", "project"), ("shared", "user")]


def _local_server_names(home: Path, workspace: Path, project_keys: list[str]) -> list[str]:
    """Local-scope servers found for *workspace* when ``projects`` holds *project_keys* in order."""
    _write_json(
        home / ".claude.json",
        {
            "projects": {
                key: {"mcpServers": {f"server-{index}": {"command": "cmd"}}} for index, key in enumerate(project_keys)
            }
        },
    )
    result = ClaudeCodeSource().discover(_context(home, workspace))
    return [candidate.name for candidate in result.candidates if candidate.scope == "local"]


def _case_swapped(path: Path) -> Path:
    return path.with_name(path.name.swapcase())


def _filesystem_ignores_case(path: Path) -> bool:
    """Probe the volume holding *path* (macOS and Windows usually ignore case, Linux doesn't)."""
    swapped = _case_swapped(path)
    return swapped != path and swapped.exists() and swapped.samefile(path)


@pytest.fixture
def mixed_case_workspace(tmp_path: Path) -> Path:
    path = tmp_path / "MyProject"
    path.mkdir()
    return path


def test_local_scope_matches_a_differently_cased_key_where_the_filesystem_ignores_case(
    home: Path, mixed_case_workspace: Path
) -> None:
    if not _filesystem_ignores_case(mixed_case_workspace):
        pytest.skip("this volume is case-sensitive")

    names = _local_server_names(home, mixed_case_workspace, [str(_case_swapped(mixed_case_workspace))])

    assert names == ["server-0"]


def test_local_scope_ignores_a_differently_cased_key_where_the_filesystem_keeps_case(
    home: Path, mixed_case_workspace: Path
) -> None:
    if _filesystem_ignores_case(mixed_case_workspace):
        pytest.skip("this volume ignores case")
    _case_swapped(mixed_case_workspace).mkdir()  # a different project that differs only in case

    names = _local_server_names(home, mixed_case_workspace, [str(_case_swapped(mixed_case_workspace))])

    assert names == []


def test_local_scope_prefers_the_exactly_spelled_key(home: Path, mixed_case_workspace: Path) -> None:
    names = _local_server_names(
        home,
        mixed_case_workspace,
        [str(_case_swapped(mixed_case_workspace)), str(mixed_case_workspace)],
    )

    assert names == ["server-1"]


@pytest.mark.skipif(sys.platform != "win32", reason="Windows paths can be written with forward slashes")
def test_local_scope_matches_a_forward_slash_key_on_windows(home: Path, mixed_case_workspace: Path) -> None:
    key = mixed_case_workspace.as_posix()
    assert key != str(mixed_case_workspace)

    assert _local_server_names(home, mixed_case_workspace, [key.upper()]) == ["server-0"]


def test_local_scope_matches_a_workspace_opened_through_a_link(home: Path, tmp_path: Path) -> None:
    real = tmp_path / "real-project"
    real.mkdir()
    link = tmp_path / "linked-project"
    symlink_or_skip(link, real, target_is_directory=True)

    assert _local_server_names(home, link, [str(real)]) == ["server-0"]


def test_local_scope_skips_keys_that_are_not_usable_paths(home: Path, workspace: Path) -> None:
    keys = [str(workspace.parent / "deleted-project"), "bad\0key", "", str(workspace)]

    assert _local_server_names(home, workspace, keys) == ["server-3"]


@pytest.fixture(params=["directory", "file"])
def repository(request: pytest.FixtureRequest, tmp_path: Path) -> Path:
    """A folder marked as a Git repository root: by a .git directory, or a .git file as in a worktree or submodule."""
    root = tmp_path / "repo"
    root.mkdir()
    if request.param == "directory":
        (root / ".git").mkdir()
    else:
        (root / ".git").write_text("gitdir: /elsewhere/.git/worktrees/repo\n", encoding="utf-8")
    return root


def _local_candidates(home: Path, workspace: Path, projects: dict[str, Any]) -> list[tuple[str, str]]:
    _write_json(home / ".claude.json", {"projects": projects})
    result = ClaudeCodeSource().discover(_context(home, workspace))
    return [(candidate.name, candidate.scope_path) for candidate in result.candidates]


def test_local_scope_reads_the_repository_root_inside_a_repository(home: Path, repository: Path) -> None:
    # Inside a Git repository Claude Code keys local servers by the repository root [observed 2.1.302].
    workspace = repository / "packages" / "app"
    workspace.mkdir(parents=True)

    found = _local_candidates(home, workspace, {str(repository): {"mcpServers": {"repo-server": {"command": "cmd"}}}})

    assert found == [("repo-server", str(repository))]


def test_local_scope_never_reads_a_folder_key_inside_a_repository(home: Path, repository: Path) -> None:
    # In a repository's subfolder Claude Code uses the root's servers and never the subfolder's own,
    # with or without a root entry [observed 2.1.302].
    workspace = repository / "app"
    workspace.mkdir()
    workspace_only = {str(workspace): {"mcpServers": {"workspace-server": {"command": "cmd"}}}}
    both = {**workspace_only, str(repository): {"mcpServers": {"repo-server": {"command": "cmd"}}}}

    assert _local_candidates(home, workspace, workspace_only) == []
    assert _local_candidates(home, workspace, both) == [("repo-server", str(repository))]


def test_local_scope_reads_the_workspace_key_outside_a_repository(home: Path, tmp_path: Path) -> None:
    workspace = tmp_path / "plain" / "app"
    workspace.mkdir(parents=True)
    projects = {
        str(tmp_path / "plain"): {"mcpServers": {"parent-server": {"command": "cmd"}}},
        str(workspace): {"mcpServers": {"own-server": {"command": "cmd"}}},
    }

    assert _local_candidates(home, workspace, projects) == [("own-server", str(workspace))]


def test_local_scope_looks_no_higher_than_the_workspace_outside_a_repository(home: Path, tmp_path: Path) -> None:
    workspace = tmp_path / "plain" / "app"
    workspace.mkdir(parents=True)

    found = _local_candidates(
        home, workspace, {str(tmp_path / "plain"): {"mcpServers": {"parent-server": {"command": "cmd"}}}}
    )

    assert found == []


def test_discover_without_a_workspace_reads_only_user_servers(home: Path) -> None:
    _write_json(home / ".claude.json", {"mcpServers": {"user-server": {"command": "cmd"}}})

    result = ClaudeCodeSource().discover(_context(home))

    assert [candidate.name for candidate in result.candidates] == ["user-server"]


def test_claude_config_dir_replaces_home(home: Path, tmp_path: Path) -> None:
    _write_json(home / ".claude.json", {"mcpServers": {"home-server": {"command": "cmd"}}})
    custom = tmp_path / "custom"
    _write_json(custom / ".claude.json", {"mcpServers": {"custom-server": {"command": "cmd"}}})

    ctx = _context(home, CLAUDE_CONFIG_DIR=str(custom))

    assert ClaudeCodeSource().detect(ctx) is True
    assert [c.name for c in ClaudeCodeSource().discover(ctx).candidates] == ["custom-server"]


@pytest.mark.parametrize(
    ("config_dir", "found"),
    [
        ("", "home-server"),  # empty keeps .claude.json in the home folder
        ("   ", "blank-server"),  # blank is a folder named by the spaces, not unset [observed 2.1.302]
        ("profile", "relative-server"),  # relative to where Claude Code starts, the workspace here
    ],
)
def test_claude_config_dir_is_read_as_claude_code_reads_it(
    home: Path, workspace: Path, config_dir: str, found: str
) -> None:
    _write_json(home / ".claude.json", {"mcpServers": {"home-server": {"command": "cmd"}}})
    _write_json(workspace / "   " / ".claude.json", {"mcpServers": {"blank-server": {"command": "cmd"}}})
    _write_json(workspace / "profile" / ".claude.json", {"mcpServers": {"relative-server": {"command": "cmd"}}})

    result = ClaudeCodeSource().discover(_context(home, workspace, CLAUDE_CONFIG_DIR=config_dir))

    assert [c.name for c in result.candidates] == [found]


def _global_servers(home: Path, workspace: Path | None = None, **environ: str) -> list[str]:
    return [
        candidate.name for candidate in ClaudeCodeSource().discover(_context(home, workspace, **environ)).candidates
    ]


def test_a_legacy_config_json_in_the_config_folder_replaces_claude_json(home: Path) -> None:
    # Claude Code reads ~/.claude/.config.json instead of ~/.claude.json, but never ~/.config.json
    # [observed 2.1.302].
    _write_json(home / ".claude.json", {"mcpServers": {"claude-json": {"command": "cmd"}}})
    _write_json(home / ".config.json", {"mcpServers": {"home-config-json": {"command": "cmd"}}})
    legacy = _write_json(home / ".claude" / ".config.json", {"mcpServers": {"legacy": {"command": "cmd"}}})

    result = ClaudeCodeSource().discover(_context(home))

    assert [(candidate.name, candidate.source_path) for candidate in result.candidates] == [("legacy", str(legacy))]


def test_claude_config_dir_names_the_folder_of_the_legacy_config_json(home: Path, workspace: Path) -> None:
    custom = home / "custom"
    _write_json(custom / ".claude.json", {"mcpServers": {"custom-claude-json": {"command": "cmd"}}})
    _write_json(home / ".claude" / ".config.json", {"mcpServers": {"home-legacy": {"command": "cmd"}}})
    assert _global_servers(home, CLAUDE_CONFIG_DIR=str(custom)) == ["custom-claude-json"]

    _write_json(custom / ".config.json", {"mcpServers": {"custom-legacy": {"command": "cmd"}}})
    assert _global_servers(home, CLAUDE_CONFIG_DIR=str(custom)) == ["custom-legacy"]

    # Set but empty, it still names a folder: the one Claude Code starts in, while .claude.json
    # stays in the home directory [observed 2.1.302].
    _write_json(home / ".claude.json", {"mcpServers": {"home-claude-json": {"command": "cmd"}}})
    assert _global_servers(home, workspace, CLAUDE_CONFIG_DIR="") == ["home-claude-json"]
    _write_json(workspace / ".config.json", {"mcpServers": {"workspace-legacy": {"command": "cmd"}}})
    assert _global_servers(home, workspace, CLAUDE_CONFIG_DIR="") == ["workspace-legacy"]
    # Without a workspace that folder is unknown, so only .claude.json is read.
    assert _global_servers(home, CLAUDE_CONFIG_DIR="") == ["home-claude-json"]


def test_an_empty_legacy_config_json_hides_claude_json_only_when_writable_and_alone(home: Path) -> None:
    legacy = home / ".claude" / ".config.json"
    legacy.parent.mkdir()
    legacy.write_bytes(b"")
    claude_json = _write_json(home / ".claude.json", {"mcpServers": {"claude-json": {"command": "cmd"}}})
    # One a sandboxed command left behind [binary 2.1.302; observed 2.1.302].
    assert _global_servers(home) == ["claude-json"]

    claude_json.unlink()
    result = ClaudeCodeSource().discover(_context(home))
    assert result.source_errors == (McpSourceError(str(legacy), SourceErrorKind.INVALID_JSON),)

    legacy.chmod(0o444)
    try:
        result = ClaudeCodeSource().discover(_context(home))
    finally:
        legacy.chmod(0o644)
    assert result == McpImportResult()


def test_detect_finds_a_legacy_config_json_alone(home: Path) -> None:
    _write_json(home / ".claude" / ".config.json", {"mcpServers": {"legacy": {"command": "cmd"}}})

    assert ClaudeCodeSource().detect(_context(home)) is True


def test_a_tilde_in_claude_config_dir_is_a_folder_named_tilde(home: Path, workspace: Path) -> None:
    # Claude Code doesn't expand it [observed 2.1.302].
    _write_json(home / "cfg" / ".claude.json", {"mcpServers": {"expanded": {"command": "cmd"}}})
    _write_json(workspace / "~" / "cfg" / ".claude.json", {"mcpServers": {"literal": {"command": "cmd"}}})

    assert _global_servers(home, workspace, CLAUDE_CONFIG_DIR="~/cfg") == ["literal"]


def test_a_folder_named_config_json_is_not_a_legacy_config(home: Path) -> None:
    (home / ".claude" / ".config.json").mkdir(parents=True)
    _write_json(home / ".claude.json", {"mcpServers": {"claude-json": {"command": "cmd"}}})

    assert _global_servers(home) == ["claude-json"]


def test_a_relative_claude_config_dir_without_a_workspace_reads_nothing(home: Path) -> None:
    # The folder Claude Code would resolve it against is unknown, and the process's own is not a stand-in.
    _write_json(home / ".claude.json", {"mcpServers": {"home-server": {"command": "cmd"}}})
    ctx = _context(home, CLAUDE_CONFIG_DIR="profile")

    assert ClaudeCodeSource().detect(ctx) is False
    assert ClaudeCodeSource().discover(ctx) == McpImportResult()


def test_stdio_server_converts_env_placeholders(home: Path) -> None:
    _write_json(
        home / ".claude.json",
        {
            "mcpServers": {
                "github": {
                    "type": "stdio",
                    "command": "npx",
                    "args": ["-y", "@modelcontextprotocol/server-github"],
                    "env": {"TOKEN": "${GITHUB_TOKEN}", "LITERAL": "sk-123", "HOST": "${HOST:-localhost}"},
                }
            }
        },
    )

    (candidate,) = ClaudeCodeSource().discover(_context(home)).candidates

    assert candidate.config == MCPServerConfig(
        name="github",
        transport="stdio",
        command="npx",
        args=["-y", "@modelcontextprotocol/server-github"],
        env={"TOKEN": "{{GITHUB_TOKEN}}", "LITERAL": "sk-123", "HOST": "{{HOST}}"},
    )
    assert candidate.unsupported is None
    assert candidate.warnings == (ImportWarning(ImportWarningKind.DEFAULT_DROPPED, "HOST", field="HOST"),)


def test_http_server_converts_header_placeholders(home: Path) -> None:
    _write_json(
        home / ".claude.json",
        {
            "mcpServers": {
                "remote": {
                    "type": "http",
                    "url": "https://example.com/mcp",
                    "headers": {"Authorization": "Bearer ${API_TOKEN}"},
                },
                # The MCP specification's name for the transport, accepted as an alias
                # [doc: mcp, "Option 1: Add a remote HTTP server"].
                "spec-named": {"type": "streamable-http", "url": "https://example.com/other"},
            }
        },
    )

    candidates = _by_name(ClaudeCodeSource().discover(_context(home)).candidates)

    assert candidates["remote"].config == MCPServerConfig(
        name="remote",
        transport="http",
        url="https://example.com/mcp",
        headers={"Authorization": "Bearer {{API_TOKEN}}"},
    )
    assert candidates["remote"].warnings == ()
    assert candidates["spec-named"].config == MCPServerConfig(
        name="spec-named", transport="http", url="https://example.com/other"
    )
    assert candidates["spec-named"].transport == "http"


@pytest.mark.parametrize(
    "entry",
    [
        # Read as stdio, so a configuration error [doc: mcp, "Option 1: Add a remote HTTP server"].
        {"url": "https://x.test/mcp"},
        {"url": "https://x.test/mcp", "command": "cmd"},
        {"type": "sdk"},  # only an SDK host application registers one
        {"type": "carrier-pigeon", "url": "https://x.test"},
        {"type": 5, "url": "https://x.test/mcp"},
        {"type": "stdio"},
        {"command": "  "},
        {"command": "server", "args": "--flag"},
        {"command": "server", "env": {"PORT": 8080}},
        {"type": "http"},
        {"type": "http", "url": "  "},
        {"type": "http", "url": "https://x.test", "headers": ["a"]},
        {"type": "sse"},
        {"type": "ws", "url": "wss://x.test", "headers": {"A": 1}},
        "not an object",
        # A setting of the wrong type skips the whole entry, null included [observed 2.1.302].
        {"type": None, "command": "cmd"},
        {"command": "cmd", "args": None},
        {"command": "cmd", "env": None},
        {"type": "http", "url": "https://x.test", "headers": None},
        {"command": "cmd", "timeout": 0},
        {"command": "cmd", "timeout": -5000},
        {"command": "cmd", "timeout": 1500.5},
        {"command": "cmd", "timeout": "600000"},
        {"command": "cmd", "timeout": True},
        {"command": "cmd", "timeout": None},
        {"command": "cmd", "timeout": 2**53},  # beyond what a JavaScript number holds exactly
        {"command": "cmd", "timeout": 10**400},  # Infinity to JavaScript; too long for a Python float
        {"command": "cmd", "alwaysLoad": "yes"},
        {"command": "cmd", "bareElicitationCapability": "yes"},
        {"type": "http", "url": "https://x.test", "timeout": 0},
        {"type": "http", "url": "https://x.test", "headersHelper": 5},
        {"type": "http", "url": "https://x.test", "headersHelper": None},
        {"type": "http", "url": "https://x.test", "discoveryCache": "yes"},
        {"type": "http", "url": "https://x.test", "tools": "search"},
        {"type": "http", "url": "https://x.test", "tools": [{"permission_policy": "always_allow"}]},
        {"type": "http", "url": "https://x.test", "tools": [{"name": "search", "permission_policy": "sometimes"}]},
        {"type": "http", "url": "https://x.test", "tools": [{"name": "search", "permission_policy": []}]},
        {"type": "http", "url": "https://x.test", "toolPermissions": {"search": {}}},
        {"type": "http", "url": "https://x.test", "toolPermissions": "allow"},
        {"type": "http", "url": "https://x.test", "toolPermissions": {"search": "maybe"}},
        {"type": "http", "url": "https://x.test", "oauth": "client"},
        {"type": "http", "url": "https://x.test", "oauth": None},
        {"type": "http", "url": "https://x.test", "oauth": {"clientId": 5}},
        {"type": "http", "url": "https://x.test", "oauth": {"callbackPort": "8080"}},
        {"type": "http", "url": "https://x.test", "oauth": {"callbackPort": 0}},
        {"type": "http", "url": "https://x.test", "oauth": {"callbackPort": 10**400}},
        {"type": "http", "url": "https://x.test", "oauth": {"authServerMetadataUrl": "http://auth.test"}},
        {"type": "http", "url": "https://x.test", "oauth": {"authServerMetadataUrl": "HTTPS://auth.test"}},
        {"type": "http", "url": "https://x.test", "oauth": {"authServerMetadataUrl": "https://"}},
        {"type": "http", "url": "https://x.test", "oauth": {"authServerMetadataUrl": "https://au th.test"}},
        {"type": "http", "url": "https://x.test", "oauth": {"scopes": ""}},
        {"type": "http", "url": "https://x.test", "oauth": {"xaa": "yes"}},
        {"type": "sse", "url": "https://x.test/sse", "timeout": "600000"},
        {"type": "ws", "url": "wss://x.test", "timeout": "600000"},
        {"type": "ws", "url": "wss://x.test", "headersHelper": 5},
    ],
)
def test_entries_claude_code_skips_are_not_listed(home: Path, entry: object) -> None:
    _write_json(home / ".claude.json", {"mcpServers": {"skipped": entry, "kept": {"command": "cmd"}}})

    result = ClaudeCodeSource().discover(_context(home))

    assert [candidate.name for candidate in result.candidates] == ["kept"]


@pytest.mark.parametrize(
    "entry",
    [
        {"command": "cmd", "timeout": 9_007_199_254_740_991},
        {"command": "cmd", "role": "unknown"},  # Claude Code drops a role it doesn't know
        {"type": "http", "url": "https://x.test", "tools": [{"name": "search", "permission_policy": "always_ask"}]},
        {"type": "http", "url": "https://x.test", "toolPermissions": {"search": "blocked"}},
        {"type": "http", "url": "https://x.test", "oauth": {"authServerMetadataUrl": "https://auth.test:8443/meta"}},
        {"type": "http", "url": "https://x.test", "oauth": {"authServerMetadataUrl": "https://[::1]/meta"}},
        # Claude Code trims the URL, reads a backslash as a slash and decodes the host [observed 2.1.302].
        {"type": "http", "url": "https://x.test", "oauth": {"authServerMetadataUrl": " https://auth.test"}},
        {"type": "http", "url": "https://x.test", "oauth": {"authServerMetadataUrl": "https://auth.test\\meta"}},
        {"type": "http", "url": "https://x.test", "oauth": {"authServerMetadataUrl": "https://%61uth.test"}},
        {"type": "http", "url": "https://x.test", "oauth": {"future": 1}},
        {"type": "ws", "url": "wss://x.test", "oauth": "client"},  # a WebSocket server has no oauth to check
    ],
)
def test_well_typed_settings_keep_the_entry(home: Path, entry: dict[str, Any]) -> None:
    _write_json(home / ".claude.json", {"mcpServers": {"server": entry}})

    assert [candidate.name for candidate in ClaudeCodeSource().discover(_context(home)).candidates] == ["server"]


def test_a_blank_server_name_is_not_listed(home: Path) -> None:
    _write_json(home / ".claude.json", {"mcpServers": {"  ": {"command": "cmd"}}})

    assert ClaudeCodeSource().discover(_context(home)).candidates == ()


@pytest.mark.parametrize(
    "name",
    [
        # Claude Code's own servers, compared as written in tool names [doc: mcp, "Configuration
        # warnings"; observed 2.1.302].
        "workspace",
        "computer-use",
        "claude-in-chrome",
        "Claude in Chrome",
        "Claude Preview",
        "Claude Browser",
        "claude-device",
        "hearthbot",
        "remote-devices",
        "claude.ai",
        "webagent",
        "claude-code-remote",
        "workspace ",
        "Claude Preview!",
        "workspace__extra",
        "workspace\U0001f600x",  # one character beyond U+FFFF is two UTF-16 units, so "__"
        "claude.ai ",
        "Slack sign-in (Claude Code tag)",
        # Object.prototype members Claude Code can't hold as a name, the first four new in 2.1.302.
        "__defineGetter__",
        "__defineSetter__",
        "__lookupGetter__",
        "__lookupSetter__",
        "__proto__",
    ],
)
def test_names_claude_code_reserves_are_not_listed(home: Path, workspace: Path, name: str) -> None:
    server = {"mcpServers": {name: {"command": "cmd"}, "kept": {"command": "cmd"}}}
    _write_json(home / ".claude.json", server)
    _write_json(workspace / ".mcp.json", server)

    result = ClaudeCodeSource().discover(_context(home, workspace))

    assert [candidate.name for candidate in result.candidates] == ["kept", "kept"]


@pytest.mark.parametrize(
    "name",
    [
        "Workspace",
        "work space",
        "computer use",
        "computer_use",
        "Claude-Preview",
        "claude in chrome",
        "claude-ai",
        "_workspace",
        "__workspace",
        "x__workspace",
        "claude.ai  x",  # a connector's name is cut at no "__"
        "Claude_Pages",  # reserved in Claude Code's hosted sessions only
        "widgets",
        "constructor",  # Object.prototype members whose tool-name form is a usable name
        "prototype",
        "toString",
        "hasOwnProperty",
    ],
)
def test_names_claude_code_loads_are_listed(home: Path, name: str) -> None:
    _write_json(home / ".claude.json", {"mcpServers": {name: {"command": "cmd"}}})

    assert [candidate.name for candidate in ClaudeCodeSource().discover(_context(home)).candidates] == [name]


def test_settings_referencing_claude_project_dir_are_flagged(home: Path) -> None:
    # Claude Code sets CLAUDE_PROJECT_DIR for the stdio servers it starts; iCode doesn't
    # [doc: mcp, "Option 3: Add a local stdio server"].
    _write_json(
        home / ".claude.json",
        {
            "mcpServers": {
                "local": {"command": "cmd", "env": {"ROOT": "${CLAUDE_PROJECT_DIR}", "MODE": "fast"}},
                "remote": {
                    "type": "http",
                    "url": "https://x.test/mcp",
                    "headers": {"X-Project": "${CLAUDE_PROJECT_DIR:-.}"},
                },
            }
        },
    )

    local, remote = ClaudeCodeSource().discover(_context(home)).candidates

    assert local.config is not None and local.config.env["ROOT"] == "{{CLAUDE_PROJECT_DIR}}"
    assert local.warnings == (ImportWarning(ImportWarningKind.CLAUDE_PROJECT_DIR, "CLAUDE_PROJECT_DIR", field="ROOT"),)
    assert remote.warnings == (
        ImportWarning(ImportWarningKind.DEFAULT_DROPPED, "CLAUDE_PROJECT_DIR", field="X-Project"),
        ImportWarning(ImportWarningKind.CLAUDE_PROJECT_DIR, "CLAUDE_PROJECT_DIR", field="X-Project"),
    )


def test_oauth_settings_are_reported_as_not_imported(home: Path) -> None:
    _write_json(
        home / ".claude.json",
        {"mcpServers": {"remote": {"type": "http", "url": "https://x.test/mcp", "oauth": {"clientId": "id"}}}},
    )

    (candidate,) = ClaudeCodeSource().discover(_context(home)).candidates

    assert candidate.config is not None
    assert candidate.warnings == (ImportWarning(ImportWarningKind.OAUTH_IGNORED),)


def test_headers_naming_a_credential_claude_code_withholds_are_flagged(home: Path) -> None:
    # Claude Code reads these variables as empty in a remote server's headers; iCode would send
    # the real value [doc: mcp, "Credential variables that read as empty"].
    _write_json(
        home / ".claude.json",
        {
            "mcpServers": {
                "remote": {
                    "type": "http",
                    "url": "https://x.test/mcp",
                    "headers": {
                        "Authorization": "Bearer ${ANTHROPIC_AUTH_TOKEN}",
                        "X-Proxy": "${HTTPS_PROXY:-none}",
                        "X-Own": "${MY_TOKEN}",
                    },
                }
            }
        },
    )

    (candidate,) = ClaudeCodeSource().discover(_context(home)).candidates

    assert candidate.config is not None
    assert candidate.config.headers["Authorization"] == "Bearer {{ANTHROPIC_AUTH_TOKEN}}"
    assert candidate.warnings == (
        ImportWarning(ImportWarningKind.DEFAULT_DROPPED, "HTTPS_PROXY", field="X-Proxy"),
        ImportWarning(ImportWarningKind.CREDENTIAL_VARIABLE, "ANTHROPIC_AUTH_TOKEN", field="Authorization"),
        ImportWarning(ImportWarningKind.CREDENTIAL_VARIABLE, "HTTPS_PROXY", field="X-Proxy"),
    )


@pytest.mark.parametrize(
    ("timeout", "request_timeout"),
    [
        (600000, 600),
        (1500, 2),
        (1500.0, 2),  # a JSON number JavaScript reads as the whole number 1500
        (1000, 1),
        (999, None),  # Claude Code ignores values below one second too
        (None, None),
    ],
)
def test_timeout_in_milliseconds_becomes_a_request_timeout_in_seconds(
    home: Path, timeout: object, request_timeout: int | None
) -> None:
    timeout_field: dict[str, Any] = {} if timeout is None else {"timeout": timeout}
    _write_json(
        home / ".claude.json",
        {
            "mcpServers": {
                "local": {"command": "cmd", **timeout_field},
                "remote": {"type": "http", "url": "https://x.test", **timeout_field},
            }
        },
    )

    local, remote = ClaudeCodeSource().discover(_context(home)).candidates

    assert local.config is not None and remote.config is not None
    assert (local.config.request_timeout, remote.config.request_timeout) == (request_timeout, request_timeout)
    assert local.warnings == remote.warnings == ()


@pytest.mark.parametrize(
    ("entry_timeouts", "request_timeout"),
    [
        ({"request_timeout_ms": 120000}, 120),
        ({"request_timeout_ms": 1500}, 2),
        ({"request_timeout_ms": 900000}, 300),  # Claude Code caps it at five minutes
        ({"request_timeout_ms": 999}, None),  # then ignores it below one second, as any timeout
        ({"request_timeout_ms": 0}, None),
        ({"request_timeout_ms": -1}, None),
        ({"request_timeout_ms": 10**400}, None),
        ({"request_timeout_ms": 1500.5}, None),
        ({"request_timeout_ms": "120000"}, None),
        ({"request_timeout_ms": True}, None),
        ({"timeout": 5000, "request_timeout_ms": 120000}, 5),  # the documented timeout wins
        ({"timeout": 500, "request_timeout_ms": 120000}, None),
    ],
)
def test_a_remote_server_without_a_timeout_takes_its_request_timeout_ms(
    home: Path, entry_timeouts: dict[str, Any], request_timeout: int | None
) -> None:
    # Claude Code reads request_timeout_ms as the timeout of an http or sse server that sets none
    # [binary 2.1.302, internal].
    _write_json(
        home / ".claude.json", {"mcpServers": {"remote": {"type": "http", "url": "https://x.test", **entry_timeouts}}}
    )

    (candidate,) = ClaudeCodeSource().discover(_context(home)).candidates

    assert candidate.config is not None
    assert candidate.config.request_timeout == request_timeout
    assert candidate.warnings == ()


def test_a_stdio_server_has_no_request_timeout_ms(home: Path) -> None:
    _write_json(home / ".claude.json", {"mcpServers": {"local": {"command": "cmd", "request_timeout_ms": 120000}}})

    (candidate,) = ClaudeCodeSource().discover(_context(home)).candidates

    assert candidate.config is not None
    assert candidate.config.request_timeout is None
    assert candidate.warnings == (
        ImportWarning(ImportWarningKind.UNKNOWN_FIELDS_IGNORED, original="request_timeout_ms"),
    )


def test_fields_icode_has_no_setting_for_are_listed_once(home: Path) -> None:
    _write_json(
        home / ".claude.json",
        {
            "mcpServers": {
                "local": {"command": "cmd", "alwaysLoad": True, "headers": {"X": "y"}, "future": 1},
                "remote": {"type": "http", "url": "https://x.test", "alwaysLoad": False, "env": {"A": "b"}},
                "plain": {"command": "cmd", "args": [], "env": {}, "timeout": 5000},
                "internal": {"command": "cmd", "bareElicitationCapability": True, "role": "comms"},
                "remote-internal": {
                    "type": "http",
                    "url": "https://x.test",
                    "discoveryCache": True,
                    "bareElicitationCapability": False,
                    "role": "comms",
                },
                "permissions": {
                    "type": "http",
                    "url": "https://x.test",
                    "tools": [{"name": "search", "permission_policy": "always_allow"}],
                    "toolPermissions": {"search": "allow"},
                },
            }
        },
    )

    local, remote, plain, internal, remote_internal, permissions = (
        ClaudeCodeSource().discover(_context(home)).candidates
    )

    # alwaysLoad only changes when Claude Code loads the tools; iCode loads them upfront, so no note.
    # The internal settings only change how Claude Code itself talks to the server.
    assert local.warnings == (ImportWarning(ImportWarningKind.UNKNOWN_FIELDS_IGNORED, original="headers, future"),)
    assert remote.warnings == (ImportWarning(ImportWarningKind.UNKNOWN_FIELDS_IGNORED, original="env"),)
    assert plain.warnings == internal.warnings == remote_internal.warnings == ()
    # Per-tool approval rules have no iCode counterpart in a server's settings.
    assert permissions.warnings == (
        ImportWarning(ImportWarningKind.UNKNOWN_FIELDS_IGNORED, original="tools, toolPermissions"),
    )


def test_headers_helper_is_reported_as_not_imported(home: Path) -> None:
    _write_json(
        home / ".claude.json",
        {"mcpServers": {"remote": {"type": "http", "url": "https://x.test/mcp", "headersHelper": "get-token.sh"}}},
    )

    (candidate,) = ClaudeCodeSource().discover(_context(home)).candidates

    assert candidate.config is not None
    assert candidate.warnings == (ImportWarning(ImportWarningKind.HEADERS_HELPER_IGNORED, original="get-token.sh"),)


@pytest.mark.parametrize(
    ("entry", "transport"),
    [
        ({"command": "cmd"}, "stdio"),
        ({"command": "${HOME}/server"}, "stdio"),
        ({"type": "http", "url": "https://${HOST}/mcp"}, "http"),
        ({"type": "streamable-http", "url": "https://${HOST}/mcp"}, "http"),
        ({"type": "sse", "url": "https://x.test/sse"}, "sse"),
        ({"type": "ws", "url": "wss://x.test"}, "ws"),
    ],
)
def test_every_server_reports_its_transport_whether_or_not_it_can_be_imported(
    home: Path, entry: object, transport: str
) -> None:
    _write_json(home / ".claude.json", {"mcpServers": {"server": entry}})

    (candidate,) = ClaudeCodeSource().discover(_context(home)).candidates

    assert candidate.transport == transport


def test_unsupported_servers_keep_their_settings_as_written(home: Path) -> None:
    entry = {"type": "sse", "url": "https://example.com/sse", "headers": {"Authorization": "${TOKEN}"}}
    _write_json(home / ".claude.json", {"mcpServers": {"legacy": entry, "kept": {"command": "cmd"}}})

    legacy, kept = ClaudeCodeSource().discover(_context(home)).candidates

    assert (legacy.name, legacy.config, legacy.unsupported) == ("legacy", None, UnsupportedReason.UNSUPPORTED_TRANSPORT)
    assert legacy.entry == entry
    assert kept.config is not None
    assert kept.entry is None


@pytest.mark.parametrize(
    ("entry", "reason"),
    [
        ({"type": "sse", "url": "https://x.test/sse"}, UnsupportedReason.UNSUPPORTED_TRANSPORT),
        ({"type": "ws", "url": "wss://x.test", "headers": {"A": "b"}}, UnsupportedReason.UNSUPPORTED_TRANSPORT),
        ({"type": "http", "url": "https://${HOST}/mcp"}, UnsupportedReason.TEMPLATE_IN_URL),
        ({"command": "${HOME}/bin/server"}, UnsupportedReason.TEMPLATE_IN_COMMAND),
        ({"command": "server", "args": ["--root", "${PROJECT}"]}, UnsupportedReason.TEMPLATE_IN_COMMAND),
        # iCode escapes braces only in pairs, so a single brace beside a placeholder can't be written.
        ({"command": "server", "env": {"JSON": "{${VALUE}}"}}, UnsupportedReason.TEMPLATE_NEXT_TO_BRACE),
        (
            {"type": "http", "url": "https://x.test/mcp", "headers": {"X-Data": "${VALUE}}}"}},
            UnsupportedReason.TEMPLATE_NEXT_TO_BRACE,
        ),
    ],
)
def test_unsupported_servers_are_listed_with_a_reason(home: Path, entry: object, reason: UnsupportedReason) -> None:
    _write_json(home / ".claude.json", {"mcpServers": {"server": entry}})

    (candidate,) = ClaudeCodeSource().discover(_context(home)).candidates

    assert candidate.config is None
    assert candidate.unsupported is reason


def test_unreadable_files_are_reported_and_other_files_still_listed(home: Path, tmp_path: Path) -> None:
    workspace = tmp_path / "repo" / "app"
    workspace.mkdir(parents=True)
    config = home / ".claude.json"
    config.write_text("{broken", encoding="utf-8")
    broken_project = workspace / ".mcp.json"
    broken_project.write_text("[]", encoding="utf-8")
    _write_json(tmp_path / "repo" / ".mcp.json", {"mcpServers": {"project-server": {"command": "cmd"}}})

    result = ClaudeCodeSource().discover(_context(home, workspace))

    assert [candidate.name for candidate in result.candidates] == ["project-server"]
    assert result.source_errors == (
        McpSourceError(str(config), SourceErrorKind.INVALID_JSON),
        McpSourceError(str(broken_project), SourceErrorKind.NOT_AN_OBJECT),
    )


@pytest.mark.parametrize("text", ["", "  \n", "\ufeff", "\u3000\u2028"])
def test_a_blank_project_file_holds_no_servers(home: Path, workspace: Path, text: str) -> None:
    # Claude Code reads it as no servers [changelog 2.1.299; observed 2.1.302].
    (workspace / ".mcp.json").write_text(text, encoding="utf-8")
    _write_json(home / ".claude.json", {"mcpServers": {"user-server": {"command": "cmd"}}})

    result = ClaudeCodeSource().discover(_context(home, workspace))

    assert [candidate.name for candidate in result.candidates] == ["user-server"]
    assert result.source_errors == ()


def test_a_blank_claude_json_is_still_unreadable(home: Path) -> None:
    # Claude Code reports a blank global config as corrupted [observed 2.1.302].
    config = home / ".claude.json"
    config.write_text("  \n", encoding="utf-8")

    result = ClaudeCodeSource().discover(_context(home))

    assert result.source_errors == (McpSourceError(str(config), SourceErrorKind.INVALID_JSON),)


def test_a_project_file_holding_more_than_whitespace_must_be_json(home: Path, workspace: Path) -> None:
    # Python counts these separators as whitespace; JavaScript, and so Claude Code, doesn't.
    project = workspace / ".mcp.json"
    project.write_text("  \x1c  \n", encoding="utf-8")

    result = ClaudeCodeSource().discover(_context(home, workspace))

    assert result.source_errors == (McpSourceError(str(project), SourceErrorKind.INVALID_JSON),)


def test_the_filesystem_root_is_never_searched_for_a_project_file(tmp_path: Path) -> None:
    files = _project_files(str(tmp_path))

    # Claude Code walks up to, but not into, the root [binary 2.1.302].
    assert files[0] == Path(os.path.realpath(tmp_path)) / ".mcp.json"
    assert Path(tmp_path.anchor) / ".mcp.json" not in files
    assert _project_files(tmp_path.anchor) == []


def test_malformed_sections_are_ignored(home: Path, workspace: Path) -> None:
    _write_json(home / ".claude.json", {"mcpServers": ["x"], "projects": {str(workspace): "nope"}})

    result = ClaudeCodeSource().discover(_context(home, workspace))

    assert result.candidates == ()
    assert result.source_errors == ()


def test_imported_configs_survive_an_agent_profile_save_and_load(home: Path, tmp_path: Path) -> None:
    _write_json(
        home / ".claude.json",
        {
            "mcpServers": {
                "local": {"command": "npx", "args": ["server"], "env": {"TOKEN": "${TOKEN}", "RAW": "{{raw}}"}},
                "remote": {"type": "http", "url": "https://x.test/mcp", "headers": {"Authorization": "${AUTH}"}},
            }
        },
    )
    configs = [c.config for c in ClaudeCodeSource().discover(_context(home)).candidates]
    assert all(config is not None for config in configs)
    profile = AgentProfile(
        name="Importer",
        instructions="Follow the user's instructions.",
        tools=ToolsConfig(mcp=[config for config in configs if config is not None]),
    )

    loaded = load_profile_from_yaml(save_profile(profile, target_dir=tmp_path / "agents"))

    assert loaded.tools.mcp == profile.tools.mcp


def test_linked_config_files_are_read_through_the_link(home: Path, tmp_path: Path) -> None:
    # Dotfile managers (stow, chezmoi) link these files; Claude Code follows the link.
    dotfiles = tmp_path / "dotfiles"
    workspace = tmp_path / "repo"
    workspace.mkdir()
    real_config = _write_json(dotfiles / "claude.json", {"mcpServers": {"user-server": {"command": "cmd"}}})
    real_project = _write_json(dotfiles / "mcp.json", {"mcpServers": {"project-server": {"command": "cmd"}}})
    symlink_or_skip(home / ".claude.json", real_config)
    symlink_or_skip(workspace / ".mcp.json", real_project)

    result = ClaudeCodeSource().discover(_context(home, workspace))

    assert [candidate.name for candidate in result.candidates] == ["project-server", "user-server"]
    assert result.source_errors == ()
