# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Tests for importing MCP servers configured in opencode 1.19.3."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import pytest

from chrys.foundation.util.env_templates import resolve_env_templates
from chrys.service.mcp.importers import (
    ImportContext,
    ImportWarning,
    ImportWarningKind,
    McpImportCandidate,
    McpSourceError,
    SourceErrorKind,
    UnsupportedReason,
)
from chrys.service.mcp.importers.base import MAX_REQUEST_TIMEOUT_SECONDS
from chrys.service.mcp.importers.opencode import OpenCodeSource
from chrys.service.profiles.agents.schema import MCPServerConfig


@pytest.fixture
def home(tmp_path: Path) -> Path:
    path = tmp_path / "home"
    path.mkdir()
    return path


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    path = tmp_path / "repo" / "app"
    path.mkdir(parents=True)
    (tmp_path / "repo" / ".git").mkdir()
    return path


@pytest.fixture
def managed(tmp_path: Path) -> Path:
    # Stands in for /Library/Application Support/opencode or /etc/opencode, which a test must not read.
    path = tmp_path / "managed"
    path.mkdir()
    return path


def _write(path: Path, document: Any) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(document if isinstance(document, str) else json.dumps(document), encoding="utf-8")
    return path


def _global(home: Path, document: Any, name: str = "opencode.json") -> Path:
    return _write(home / ".config" / "opencode" / name, document)


def _context(home: Path, managed: Path, workspace: Path | None = None, **environ: str) -> ImportContext:
    return ImportContext(
        workspace_cwd=None if workspace is None else str(workspace),
        home=home,
        environ={"OPENCODE_TEST_MANAGED_CONFIG_DIR": str(managed), **environ},
    )


def _discover(
    home: Path, managed: Path, workspace: Path | None = None, **environ: str
) -> tuple[McpImportCandidate, ...]:
    return OpenCodeSource().discover(_context(home, managed, workspace, **environ)).candidates


def _by_name(candidates: tuple[McpImportCandidate, ...]) -> dict[str, McpImportCandidate]:
    return {candidate.name: candidate for candidate in candidates}


def _local(*command: str, **fields: Any) -> dict[str, Any]:
    return {"type": "local", "command": list(command), **fields}


def _remote(url: str, **fields: Any) -> dict[str, Any]:
    return {"type": "remote", "url": url, **fields}


def test_detect_needs_the_global_folder_or_a_config_file(home: Path, workspace: Path, managed: Path) -> None:
    source = OpenCodeSource()
    assert source.detect(_context(home, managed, workspace)) is False

    _write(workspace / "opencode.jsonc", "{}")
    assert source.detect(_context(home, managed, workspace)) is True

    (workspace / "opencode.jsonc").unlink()
    # opencode creates its global folder on every start.
    (home / ".config" / "opencode").mkdir(parents=True)
    assert source.detect(_context(home, managed, workspace)) is True


def test_local_and_remote_servers_are_converted(home: Path, managed: Path) -> None:
    _global(
        home,
        {
            "mcp": {
                "files": _local(
                    "npx", "-y", "server-files", environment={"TOKEN": "{env:FILES_TOKEN}", "MODE": "{{literal}}"}
                ),
                "docs": _remote("https://docs.test/mcp", headers={"Authorization": "Bearer {env:DOCS_TOKEN}"}),
            }
        },
    )

    candidates = _by_name(_discover(home, managed, FILES_TOKEN="a", DOCS_TOKEN="b"))

    assert candidates["files"].config == MCPServerConfig(
        name="files",
        transport="stdio",
        command="npx",
        args=["-y", "server-files"],
        env={"TOKEN": "{{FILES_TOKEN}}", "MODE": "{{{{literal}}}}"},
    )
    assert candidates["files"].scope == "global"
    assert candidates["files"].scope_path == ""
    assert candidates["files"].transport == "stdio"
    assert candidates["files"].warnings == ()
    assert candidates["docs"].config == MCPServerConfig(
        name="docs", transport="http", url="https://docs.test/mcp", headers={"Authorization": "Bearer {{DOCS_TOKEN}}"}
    )
    assert candidates["docs"].transport == "http"


def test_converted_values_resolve_as_opencode_substitutes_them(monkeypatch: pytest.MonkeyPatch) -> None:
    from chrys.service.mcp.importers.base import render_template
    from chrys.service.mcp.importers.opencode import _env_parts

    monkeypatch.setenv("IMPORT_TEST_TOKEN", "secret")
    parts = _env_parts("{{x}} {env:IMPORT_TEST_TOKEN}}")

    assert parts is not None
    assert resolve_env_templates(render_template(parts), location="test") == "{{x}} secret}"


def test_an_unset_variable_gets_a_warning(home: Path, managed: Path) -> None:
    # opencode reads an unset or empty variable as empty [code: C/variable.ts:37]; iCode fails the connection.
    _global(home, {"mcp": {"s": _local("cmd", environment={"A": "{env:SET}", "B": "{env:EMPTY}{env:MISSING}"})}})

    (candidate,) = _discover(home, managed, SET="1", EMPTY="")

    assert candidate.warnings == (
        ImportWarning(ImportWarningKind.VARIABLE_UNSET, "EMPTY", field="B"),
        ImportWarning(ImportWarningKind.VARIABLE_UNSET, "MISSING", field="B"),
    )


def test_settings_icode_lacks_are_reported(home: Path, managed: Path) -> None:
    _global(
        home,
        {
            "mcp": {
                "worker": _local("cmd", cwd="tools", enabled=False, timeout=1500),
                "here": _local("cmd", cwd="."),
                "signin": _remote("https://x.test/mcp", oauth={"clientId": "id"}),
                "plain": _remote("https://y.test/mcp", oauth=False),
            }
        },
    )

    candidates = _by_name(_discover(home, managed))

    worker = candidates["worker"]
    assert worker.config is not None
    assert worker.config.enabled is False
    assert worker.config.request_timeout == 2
    assert worker.warnings == (
        ImportWarning(ImportWarningKind.CWD_IGNORED, original="tools"),
        ImportWarning(ImportWarningKind.DISABLED),
    )
    assert candidates["here"].warnings == ()
    assert candidates["signin"].warnings == (ImportWarning(ImportWarningKind.OAUTH_IGNORED),)
    assert candidates["plain"].warnings == ()


def test_experimental_mcp_timeout_is_the_fallback_timeout(home: Path, managed: Path) -> None:
    _global(
        home,
        {
            "experimental": {"mcp_timeout": 45000},
            "mcp": {"default": _local("a"), "own": _local("b", timeout=10000)},
        },
    )

    candidates = _by_name(_discover(home, managed))

    assert candidates["default"].config is not None and candidates["default"].config.request_timeout == 45
    assert candidates["own"].config is not None and candidates["own"].config.request_timeout == 10


@pytest.mark.parametrize(
    ("entry", "reason"),
    [
        (_local("cmd", environment={"KEY": "{file:~/.secrets/key}"}), UnsupportedReason.FILE_REFERENCE),
        (_remote("https://x.test", headers={"A": "{file:token}"}), UnsupportedReason.FILE_REFERENCE),
        (_local("{env:HOME}/bin/server"), UnsupportedReason.TEMPLATE_IN_COMMAND),
        (_local("server", "--port={env:PORT}"), UnsupportedReason.TEMPLATE_IN_COMMAND),
        (_remote("https://{env:HOST}/mcp"), UnsupportedReason.TEMPLATE_IN_URL),
        (_local("cmd", environment={"A": "{env:MY-VAR}"}), UnsupportedReason.VARIABLE_NAME),
        (_local("cmd", environment={"A": "{{env:X}"}), UnsupportedReason.TEMPLATE_NEXT_TO_BRACE),
    ],
)
def test_servers_icode_cant_run_as_written_are_listed_but_not_importable(
    home: Path, managed: Path, entry: dict[str, Any], reason: UnsupportedReason
) -> None:
    _global(home, {"mcp": {"s": entry}})

    (candidate,) = _discover(home, managed)

    assert candidate.config is None
    assert candidate.unsupported is reason


def test_an_unsupported_server_shows_its_settings_in_the_common_shape(home: Path, managed: Path) -> None:
    _global(home, {"mcp": {"s": _local("run", "{env:X}", environment={"K": "v"})}})

    (candidate,) = _discover(home, managed)

    assert candidate.entry == {"command": "run", "args": ["{env:X}"], "env": {"K": "v"}}
    assert candidate.transport == "stdio"


@pytest.mark.parametrize(
    ("url", "reason"),
    [
        ("{env:MCP_URL}", UnsupportedReason.TEMPLATE_IN_URL),
        ("{env:MCP_SCHEME}://x.test/mcp", UnsupportedReason.TEMPLATE_IN_URL),
        ("{file:url.txt}", UnsupportedReason.FILE_REFERENCE),
    ],
)
def test_url_references_are_listed_with_a_reason_instead_of_skipped(
    home: Path, managed: Path, url: str, reason: UnsupportedReason
) -> None:
    config_file = _global(home, {"mcp": {"api": _remote(url, headers={"X-Team": "docs"})}})
    _write(config_file.parent / "url.txt", "https://x.test/mcp")

    result = OpenCodeSource().discover(_context(home, managed, MCP_URL="https://x.test/mcp", MCP_SCHEME="https"))

    assert result.source_errors == ()
    assert result.candidates == (
        McpImportCandidate(
            "api",
            "global",
            str(config_file),
            None,
            unsupported=reason,
            entry={"url": url, "headers": {"X-Team": "docs"}},
            transport="http",
        ),
    )


def test_a_file_reference_in_a_setting_not_imported_doesnt_block_the_import(home: Path, managed: Path) -> None:
    _global(home, {"mcp": {"s": _remote("https://x.test", oauth={"clientSecret": "{file:secret}"})}})

    (candidate,) = _discover(home, managed)

    assert candidate.config is not None


@pytest.mark.parametrize(
    "entry",
    [
        _local(),
        _local(""),
        _remote("not a url"),
        _remote("https://"),
        {"enabled": True},
        {"type": "sse", "url": "https://x.test", "enabled": True},
    ],
)
def test_servers_opencode_doesnt_start_are_not_listed(home: Path, managed: Path, entry: dict[str, Any]) -> None:
    _global(home, {"mcp": {"skipped": entry, "kept": _local("cmd")}})

    assert [candidate.name for candidate in _discover(home, managed)] == ["kept"]


def test_jsonc_comments_and_trailing_commas_are_read(home: Path, managed: Path) -> None:
    _global(
        home,
        """{
  // a comment holding "quotes" and {env:X}
  "mcp": {
    /* block */ "s": {"type": "local", "command": ["cmd", "a//b",],},
  },
}""",
        name="opencode.jsonc",
    )

    (candidate,) = _discover(home, managed)

    assert candidate.config is not None
    assert candidate.config.args == ["a//b"]


@pytest.mark.parametrize(
    ("text", "kind"),
    [
        ("{", SourceErrorKind.INVALID_JSON),
        ('{"mcp": [,]}', SourceErrorKind.INVALID_JSON),
        ("/* never closed", SourceErrorKind.INVALID_JSON),
        ("[]", SourceErrorKind.NOT_AN_OBJECT),
        ('{"mcp": []}', SourceErrorKind.INVALID_CONFIG),
        ('{"mcp": {"bad": {"type": "local", "command": "cmd"}}}', SourceErrorKind.INVALID_CONFIG),
        ('{"mcp": {"bad": {"type": "remote", "url": "https://x.test", "timeout": 0}}}', SourceErrorKind.INVALID_CONFIG),
        # Infinity to opencode; too long for a Python float.
        (
            '{"mcp": {"bad": {"type": "local", "command": ["cmd"], "timeout": 1%s}}}' % ("0" * 400),
            SourceErrorKind.INVALID_CONFIG,
        ),
        ('{"tools": {"x": "no"}}', SourceErrorKind.INVALID_CONFIG),
        ('{"permission": "sometimes"}', SourceErrorKind.INVALID_CONFIG),
        ('{"permissions": {}}', SourceErrorKind.INVALID_CONFIG),
        ('{"mcp": null}', SourceErrorKind.INVALID_CONFIG),
        ('{"experimental": {"mcp_timeout": null}}', SourceErrorKind.INVALID_CONFIG),
        # Any JSON value where opencode expects a word.
        ('{"permission": {"bash": {"x": []}}}', SourceErrorKind.INVALID_CONFIG),
        ('{"agent": {"b": {"permission": {"x": {"y": {}}}}}}', SourceErrorKind.INVALID_CONFIG),
    ],
)
def test_a_project_file_opencode_rejects_is_reported_and_the_rest_still_listed(
    home: Path, workspace: Path, managed: Path, text: str, kind: SourceErrorKind
) -> None:
    _global(home, {"mcp": {"kept": _local("cmd")}})
    bad = _write(workspace / "opencode.json", text)

    result = OpenCodeSource().discover(_context(home, managed, workspace))

    assert [candidate.name for candidate in result.candidates] == ["kept"]
    assert result.source_errors == (McpSourceError(str(bad), kind),)


def test_an_empty_file_is_an_empty_config(home: Path, managed: Path) -> None:
    _global(home, "")

    assert OpenCodeSource().discover(_context(home, managed)).source_errors == ()


def test_one_bad_global_file_drops_every_global_file(home: Path, workspace: Path, managed: Path) -> None:
    # opencode falls back to no global config at all [code: C/config.ts:295-303].
    _global(home, {"mcp": {"from-config-json": _local("a")}}, name="config.json")
    bad = _global(home, "{ broken", name="opencode.jsonc")
    _write(workspace / "opencode.json", {"mcp": {"project": _local("b")}})

    result = OpenCodeSource().discover(_context(home, managed, workspace))

    assert [candidate.name for candidate in result.candidates] == ["project"]
    assert result.source_errors == (McpSourceError(str(bad), SourceErrorKind.INVALID_JSON),)


def test_a_malformed_server_with_a_boolean_enabled_reads_as_a_switch(home: Path, managed: Path) -> None:
    # The union falls through to its bare {"enabled": ...} member, unknown keys dropped.
    _global(home, {"mcp": {"s": _local("cmd")}}, name="config.json")
    _global(home, {"mcp": {"s": {"type": "local", "command": "not a list", "enabled": False}}})

    (candidate,) = _discover(home, managed)

    assert candidate.config is not None
    assert candidate.config.command == "cmd"
    assert candidate.config.enabled is False


def test_files_merge_per_server_and_per_key_later_files_winning(home: Path, workspace: Path, managed: Path) -> None:
    global_file = _global(
        home,
        {
            "mcp": {
                "api": _remote("https://global.test/mcp", headers={"Authorization": "Bearer {env:T}", "X-Team": "a"}),
                "tool": _local("global-cmd", "--flag", environment={"A": "1"}),
            }
        },
    )
    project_file = _write(
        workspace / "opencode.json",
        {
            "mcp": {
                "api": _remote("https://project.test/mcp", headers={"X-Team": "b"}),
                "tool": {"command": ["project-cmd"], "environment": {"B": "2"}, "type": "local"},
            }
        },
    )

    candidates = _by_name(_discover(home, managed, workspace, T="t"))

    api = candidates["api"]
    assert api.config is not None
    assert api.config.url == "https://project.test/mcp"
    # Headers merge per key: the one only the global file sets goes to the project's URL too.
    assert api.config.headers == {"Authorization": "Bearer {{T}}", "X-Team": "b"}
    assert (api.scope, api.source_path, api.scope_path) == ("project", str(project_file), str(workspace))
    assert api.warnings == (ImportWarning(ImportWarningKind.MERGED, field="Authorization", original=str(global_file)),)
    tool = candidates["tool"]
    assert tool.config is not None
    # Arrays are replaced, not merged.
    assert (tool.config.command, tool.config.args, tool.config.env) == ("project-cmd", [], {"A": "1", "B": "2"})


def test_a_bare_switch_in_a_later_file_turns_an_inherited_server_off(
    home: Path, workspace: Path, managed: Path
) -> None:
    global_file = _global(home, {"mcp": {"s": _local("cmd")}})
    project_file = _write(workspace / "opencode.json", {"mcp": {"s": {"enabled": False}}})

    (candidate,) = _discover(home, managed, workspace)

    assert candidate.config is not None and candidate.config.enabled is False
    # The server is still the global file's: that file set its type.
    assert (candidate.scope, candidate.source_path) == ("global", str(global_file))
    assert candidate.warnings == (
        ImportWarning(ImportWarningKind.MERGED, original=str(project_file)),
        ImportWarning(ImportWarningKind.DISABLED),
    )


def test_layers_are_read_in_opencode_order(home: Path, tmp_path: Path, workspace: Path, managed: Path) -> None:
    repo = workspace.parent
    custom = _write(tmp_path / "custom.json", {"mcp": {"s": _local("custom")}})
    sources = [
        (_global(home, {"mcp": {"s": _local("global-json")}}, name="config.json"), "global"),
        (_global(home, {"mcp": {"s": _local("global-opencode-json")}}), "global"),
        (_global(home, {"mcp": {"s": _local("global-jsonc")}}, name="opencode.jsonc"), "global"),
        (custom, "global"),
        (_write(repo / "opencode.json", {"mcp": {"s": _local("repo-json")}}), "project"),
        (_write(repo / "opencode.jsonc", {"mcp": {"s": _local("repo-jsonc")}}), "project"),
        (_write(workspace / "opencode.json", {"mcp": {"s": _local("app-json")}}), "project"),
        (_write(workspace / "opencode.jsonc", {"mcp": {"s": _local("app-jsonc")}}), "project"),
        # .opencode folders are read from the workspace up, so the upper one wins.
        (_write(workspace / ".opencode" / "opencode.json", {"mcp": {"s": _local("app-dot")}}), "project"),
        (_write(repo / ".opencode" / "opencode.jsonc", {"mcp": {"s": _local("repo-dot")}}), "project"),
        (_write(home / ".opencode" / "opencode.json", {"mcp": {"s": _local("home-dot")}}), "global"),
        (_write(managed / "opencode.json", {"mcp": {"s": _local("managed")}}), "global"),
    ]
    ctx = _context(home, managed, workspace, OPENCODE_CONFIG=str(custom))

    # Remove the files from the last one down: each time the next one down must win.
    for path, scope in reversed(sources):
        (candidate,) = OpenCodeSource().discover(ctx).candidates
        assert (candidate.source_path, candidate.scope) == (str(path), scope)
        path.unlink()


def test_inline_config_wins_over_files_but_not_over_managed(home: Path, managed: Path) -> None:
    _global(home, {"mcp": {"s": _local("global")}})
    content = json.dumps({"mcp": {"s": _local("inline")}})

    (candidate,) = _discover(home, managed, OPENCODE_CONFIG_CONTENT=content)
    assert (candidate.source_path, candidate.config and candidate.config.command) == (
        "OPENCODE_CONFIG_CONTENT",
        "inline",
    )

    _write(managed / "opencode.jsonc", {"mcp": {"s": _local("managed")}})
    (candidate,) = _discover(home, managed, OPENCODE_CONFIG_CONTENT=content)
    assert candidate.config is not None and candidate.config.command == "managed"


def test_project_files_stop_at_the_repository_top(home: Path, tmp_path: Path, workspace: Path, managed: Path) -> None:
    _write(tmp_path / "opencode.json", {"mcp": {"above": _local("cmd")}})
    _write(tmp_path / ".opencode" / "opencode.json", {"mcp": {"above-dot": _local("cmd")}})
    _write(workspace.parent / "opencode.json", {"mcp": {"repo": _local("cmd")}})

    assert [c.name for c in _discover(home, managed, workspace)] == ["repo"]


def test_outside_a_repository_project_files_are_searched_up_to_the_root(
    home: Path, tmp_path: Path, managed: Path
) -> None:
    folder = tmp_path / "plain" / "sub"
    folder.mkdir(parents=True)
    _write(tmp_path / "opencode.json", {"mcp": {"above": _local("cmd")}})

    candidates = _discover(home, managed, folder)

    # The search goes on above tmp_path, into folders this test doesn't own: look only at its own.
    ours = [c for c in candidates if c.scope_path.startswith(str(tmp_path.resolve()))]
    assert [(c.name, c.scope, c.scope_path) for c in ours] == [("above", "project", str(tmp_path.resolve()))]


def test_a_config_dir_spelled_apart_from_a_folder_already_read_is_read_again(
    home: Path, workspace: Path, managed: Path
) -> None:
    # opencode drops a repeated folder only when it is spelled the same [code: C/paths.ts:23-41].
    global_file = _global(home, {"mcp": {"s": _local("global")}})
    _write(workspace / "opencode.json", {"mcp": {"s": _local("project")}})

    (candidate,) = _discover(home, managed, workspace, OPENCODE_CONFIG_DIR=f"{global_file.parent}{os.sep}")

    assert candidate.config is not None and candidate.config.command == "global"


def test_disabling_project_config_skips_project_files(home: Path, workspace: Path, managed: Path) -> None:
    _write(workspace / "opencode.json", {"mcp": {"p": _local("cmd")}})
    _write(workspace / ".opencode" / "opencode.json", {"mcp": {"d": _local("cmd")}})
    _write(home / ".opencode" / "opencode.json", {"mcp": {"h": _local("cmd")}})

    names = [c.name for c in _discover(home, managed, workspace, OPENCODE_DISABLE_PROJECT_CONFIG="TRUE")]

    assert names == ["h"]


def test_config_dir_and_xdg_config_home_are_followed(home: Path, tmp_path: Path, managed: Path) -> None:
    xdg = tmp_path / "xdg"
    _write(xdg / "opencode" / "opencode.json", {"mcp": {"xdg": _local("cmd")}})
    _global(home, {"mcp": {"default-home": _local("cmd")}})
    config_dir = _write(tmp_path / "cfg" / "opencode.jsonc", {"mcp": {"dir": _local("cmd")}}).parent

    candidates = _discover(home, managed, XDG_CONFIG_HOME=str(xdg), OPENCODE_CONFIG_DIR=str(config_dir))

    assert [(c.name, c.scope) for c in candidates] == [("xdg", "global"), ("dir", "global")]


def test_opencode_2_forms_are_lowered(home: Path, managed: Path) -> None:
    _global(
        home,
        {
            "mcp": {
                "timeout": {"catalog": 20000, "execution": 20000},
                "servers": {
                    "nested": {"type": "local", "command": ["cmd"], "disabled": True},
                    "split": {"type": "remote", "url": "https://x.test", "timeout": {"startup": 1, "catalog": 2}},
                },
                "flat": {
                    "type": "local",
                    "command": ["cmd"],
                    "codemode": True,
                    "timeout": {"catalog": 3000, "execution": 3000},
                },
            }
        },
    )

    candidates = _by_name(_discover(home, managed))

    assert list(candidates) == ["flat", "nested", "split"]
    nested, split, flat = candidates["nested"].config, candidates["split"].config, candidates["flat"].config
    assert nested is not None and nested.enabled is False
    # A split timeout opencode 1 can't hold is dropped; the lowered global one applies instead.
    assert split is not None and split.request_timeout == 20
    assert flat is not None and (flat.enabled, flat.request_timeout) == (True, 3)


def test_a_server_whose_tools_opencode_hides_is_imported_without_them(home: Path, managed: Path) -> None:
    _global(
        home,
        {
            "tools": {"gh_*": False, "gh_search": True, "gh_s*": False, "gh_read": True},
            "mcp": {"gh": _local("cmd"), "other": _local("cmd")},
        },
    )

    candidates = _by_name(_discover(home, managed))

    gh = candidates["gh"].config
    assert gh is not None
    # A later rule hides gh_search again; gh_read stays shown.
    assert gh.allowed_tools == ["read"]
    assert candidates["gh"].warnings == (ImportWarning(ImportWarningKind.TOOLS_LIMITED, field="gh_*", original="read"),)
    assert candidates["other"].config is not None and candidates["other"].config.allowed_tools is None
    assert candidates["other"].warnings == ()


@pytest.mark.parametrize("key", ["*", "*_*", "g?_*", "g*"])
def test_any_pattern_covering_every_tool_hides_the_server(home: Path, managed: Path, key: str) -> None:
    _global(home, {"permission": {key: "deny"}, "mcp": {"gh": _local("cmd")}})

    (candidate,) = _discover(home, managed)

    assert candidate.config is not None and candidate.config.allowed_tools == []


def test_rules_icode_cant_express_are_named(home: Path, managed: Path) -> None:
    _global(
        home,
        {
            # "gh" alone names a tool called gh, never one of the server's.
            "tools": {"gh": False, "gh_delete": False},
            "permission": {"gh_write*": {"*": "deny"}, "gh_read": {"/secret": "deny"}},
            "agent": {"plan": {"tools": {"gh_*": False}}},
            "mcp": {"gh": _local("cmd")},
        },
    )

    (candidate,) = _discover(home, managed)

    assert candidate.config is not None and candidate.config.allowed_tools is None
    assert candidate.warnings == (
        ImportWarning(ImportWarningKind.TOOL_RULES_IGNORED, original="gh_delete, gh_write*, plan: gh_*"),
    )


def test_permission_from_the_environment_is_applied_last(home: Path, managed: Path) -> None:
    _global(home, {"tools": {"gh_*": True}, "mcp": {"gh": _local("cmd")}})

    (candidate,) = _discover(home, managed, OPENCODE_PERMISSION='{"gh_*": "deny"}')
    assert candidate.config is not None and candidate.config.allowed_tools == []

    (candidate,) = _discover(home, managed, OPENCODE_PERMISSION="not json")
    assert candidate.config is not None and candidate.config.allowed_tools is None


def test_the_home_opencode_folder_is_global_even_when_the_project_search_finds_it(home: Path, managed: Path) -> None:
    folder = home / "notes"
    folder.mkdir()
    # The search for project files stops at home, the top of this repository.
    (home / ".git").mkdir()
    _write(home / ".opencode" / "opencode.json", {"mcp": {"h": _local("cmd")}})

    (candidate,) = _discover(home, managed, folder)

    assert (candidate.scope, candidate.scope_path) == ("global", "")


def test_a_timeout_longer_than_icode_can_wait_is_capped(home: Path, managed: Path) -> None:
    _global(home, {"mcp": {"slow": _local("cmd", timeout=10**300)}})

    (candidate,) = _discover(home, managed)

    assert candidate.config is not None
    assert candidate.config.request_timeout == MAX_REQUEST_TIMEOUT_SECONDS
