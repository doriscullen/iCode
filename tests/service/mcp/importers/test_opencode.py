# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Tests for importing MCP servers configured in opencode 1.19.5."""

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
from chrys.service.profiles.agents.loader import load_profile_from_yaml
from chrys.service.profiles.agents.schema import AgentProfile, MCPServerConfig, ToolsConfig
from chrys.service.profiles.agents.serializer import save_profile


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
    # opencode creates its global folder on every start; an empty one holds no servers to import.
    (home / ".config" / "opencode").mkdir(parents=True)
    assert source.detect(_context(home, managed, workspace)) is False

    _write(home / ".config" / "opencode" / "opencode.json", "{}")
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
        request_timeout=60,
    )
    assert candidates["files"].scope == "global"
    assert candidates["files"].scope_path == ""
    assert candidates["files"].transport == "stdio"
    assert candidates["files"].warnings == (ImportWarning(ImportWarningKind.LITERAL_BRACES_ESCAPED),)
    assert candidates["docs"].config == MCPServerConfig(
        name="docs",
        transport="http",
        url="https://docs.test/mcp",
        headers={"Authorization": "Bearer {{DOCS_TOKEN}}"},
        request_timeout=60,
    )
    assert candidates["docs"].transport == "http"


def test_header_values_are_imported_trimmed_as_opencode_sends_them(home: Path, managed: Path) -> None:
    # opencode's fetch strips spaces, tabs and line breaks from both ends of a header value (observed with
    # opencode 1.19.3); iCode's HTTP client refuses to send them.
    _global(
        home,
        {
            "mcp": {
                "docs": _remote(
                    "https://docs.test/mcp", headers={"Authorization": "Bearer ", "X-Pad": " \tpadded {env:T}\r\n"}
                ),
                "files": _local("cmd", environment={"PAD": " kept "}),
            }
        },
    )

    candidates = _by_name(_discover(home, managed, T="t"))

    docs, files = candidates["docs"].config, candidates["files"].config
    assert docs is not None and files is not None
    assert docs.headers == {"Authorization": "Bearer", "X-Pad": "padded {{T}}"}
    # Environment values are passed to the process as written.
    assert files.env == {"PAD": " kept "}


def test_converted_values_resolve_as_opencode_substitutes_them(monkeypatch: pytest.MonkeyPatch) -> None:
    from chrys.service.mcp.importers.base import render_template
    from chrys.service.mcp.importers.opencode import _env_parts

    monkeypatch.setenv("IMPORT_TEST_TOKEN", "secret")
    parts = _env_parts("{{x}} {env:IMPORT_TEST_TOKEN}}")

    assert parts is not None
    assert resolve_env_templates(render_template(parts), location="test") == "{{x}} secret}"


def test_variable_references_are_imported_without_checking_current_values(home: Path, managed: Path) -> None:
    _global(home, {"mcp": {"s": _local("cmd", environment={"A": "{env:SET}", "B": "{env:EMPTY}{env:MISSING}"})}})

    (candidate,) = _discover(home, managed, SET="1", EMPTY="")

    assert candidate.config is not None
    assert candidate.config.env == {"A": "{{SET}}", "B": "{{EMPTY}}{{MISSING}}"}
    assert candidate.warnings == ()
    assert _discover(home, managed, SET="changed", EMPTY="now-set", MISSING="now-set") == (candidate,)


def test_settings_icode_lacks_are_reported(home: Path, managed: Path) -> None:
    _global(
        home,
        {
            "mcp": {
                "worker": _local("cmd", cwd="tools", enabled=False, timeout=1500),
                "here": _local("cmd", cwd="."),
                "signin": _remote("https://x.test/mcp", oauth={"clientId": "id"}),
                "implicit": _remote("https://implicit.test/mcp"),
                "plain": _remote("https://y.test/mcp", oauth=False),
            }
        },
    )

    candidates = _by_name(_discover(home, managed))

    worker = candidates["worker"]
    assert worker.config is not None
    assert worker.config.enabled is False
    assert worker.config.request_timeout == 2
    assert worker.warnings == (ImportWarning(ImportWarningKind.CWD_IGNORED, original="tools"),)
    assert candidates["here"].warnings == ()
    assert candidates["signin"].warnings == (ImportWarning(ImportWarningKind.OAUTH_IGNORED),)
    assert candidates["implicit"].warnings == (ImportWarning(ImportWarningKind.OAUTH_IGNORED),)
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
    ("global_timeout", "server_timeout", "expected"),
    [(None, None, 60), (4500, None, 5), (4500, 1501, 2), (None, 1000, 1)],
)
def test_default_timeout_and_server_global_precedence_survive_profile_roundtrip(
    home: Path,
    managed: Path,
    tmp_path: Path,
    global_timeout: int | None,
    server_timeout: int | None,
    expected: int,
) -> None:
    entry = _local("cmd") if server_timeout is None else _local("cmd", timeout=server_timeout)
    document: dict[str, Any] = {"mcp": {"s": entry}}
    if global_timeout is not None:
        document["experimental"] = {"mcp_timeout": global_timeout}
    _global(home, document)

    (candidate,) = _discover(home, managed)

    assert candidate.config is not None
    assert candidate.config.request_timeout == expected
    if global_timeout is None and server_timeout is None:
        assert candidate.timeout_source_ms is None
    path = save_profile(
        AgentProfile(name="OpenCode Import", tools=ToolsConfig(mcp=[candidate.config])),
        target_dir=tmp_path / "profiles",
    )
    (loaded,) = load_profile_from_yaml(path).tools.mcp
    assert loaded.request_timeout == expected


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
            unsupported_field="mcp.api.url",
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
        ('{"mcp": null}', SourceErrorKind.INVALID_CONFIG),
        ('{"experimental": {"mcp_timeout": null}}', SourceErrorKind.INVALID_CONFIG),
    ],
)
def test_a_project_file_opencode_rejects_is_reported_and_the_rest_still_listed(
    home: Path, workspace: Path, managed: Path, text: str, kind: SourceErrorKind
) -> None:
    _global(home, {"mcp": {"kept": _local("cmd")}})
    bad = _write(workspace / "opencode.json", text)

    result = OpenCodeSource().discover(_context(home, managed, workspace))

    assert [candidate.name for candidate in result.candidates] == ["kept"]
    assert [(error.path, error.kind) for error in result.source_errors] == [(str(bad), kind)]


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
    assert result.source_errors == (McpSourceError(str(bad), SourceErrorKind.INVALID_JSON, line=1, column=3),)


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
    assert api.warnings == (
        ImportWarning(ImportWarningKind.OAUTH_IGNORED),
        ImportWarning(ImportWarningKind.MERGED, field="Authorization", original=str(global_file)),
    )
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
    assert candidate.warnings == (ImportWarning(ImportWarningKind.MERGED, original=str(project_file)),)


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


def test_relative_xdg_config_home_uses_workspace_instead_of_process_cwd(
    home: Path, workspace: Path, managed: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    launch = tmp_path / "launch"
    launch.mkdir()
    monkeypatch.chdir(launch)
    path = _write(
        workspace / "relative-config" / "opencode" / "opencode.json",
        {"mcp": {"workspace": _local("workspace-server")}},
    )
    context = _context(home, managed, workspace, XDG_CONFIG_HOME="relative-config")
    source = OpenCodeSource()

    assert source.detect(context) is True
    _write(
        launch / "relative-config" / "opencode" / "opencode.json",
        {"mcp": {"launch": _local("launch-server")}},
    )

    result = source.discover(context)

    assert result.source_errors == ()
    assert [(c.name, c.source_path) for c in result.candidates] == [("workspace", str(path))]


def test_relative_xdg_config_home_without_workspace_does_not_read_process_cwd(
    home: Path, managed: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    _write(tmp_path / "relative-config" / "opencode" / "opencode.json", {"mcp": {"stray": _local("cmd")}})
    context = _context(home, managed, XDG_CONFIG_HOME="relative-config")
    source = OpenCodeSource()

    assert source.detect(context) is False
    assert source.discover(context).candidates == ()


def test_relative_xdg_without_workspace_keeps_other_config_layers_independent(
    home: Path, managed: Path, tmp_path: Path
) -> None:
    custom = _write(tmp_path / "custom.json", {"mcp": {"custom": _local("custom-server")}})
    _write(managed / "opencode.json", "invalid json")
    context = _context(home, managed, XDG_CONFIG_HOME="relative-config", OPENCODE_CONFIG=str(custom))

    result = OpenCodeSource().discover(context)

    assert len(result.source_errors) == 1
    assert [c.name for c in result.candidates] == ["custom"]


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


@pytest.mark.parametrize(
    "policy",
    [
        {"tools": {"gh_*": False, "gh_read": True}},
        {"permission": {"*": "deny"}},
        {"permissions": {}},
        {"tools": {"x": "invalid"}},
        {"permission": {"bash": {"x": []}}},
    ],
)
def test_global_tool_permissions_are_reported_without_interpreting_them(
    home: Path, managed: Path, policy: dict
) -> None:
    _global(home, {**policy, "mcp": {"gh": _local("cmd", enabled=False, timeout=1500)}})
    (candidate,) = _discover(home, managed)
    assert candidate.config is not None
    assert (candidate.config.enabled, candidate.config.request_timeout, candidate.config.allowed_tools) == (
        False,
        2,
        None,
    )
    assert candidate.warnings == (
        ImportWarning(ImportWarningKind.TOOL_PERMISSIONS_IGNORED, original=next(iter(policy))),
    )


@pytest.mark.parametrize("section", ["agents", "agent", "mode"])
def test_agent_permissions_do_not_gate_valid_mcp_settings(home: Path, managed: Path, section: str) -> None:
    document = {"mcp": {"gh": _local("cmd")}}
    _global(home, document)
    expected = _discover(home, managed)
    _global(
        home, {**document, section: {"build": {"tools": {"gh_*": False}, "permission": {"x": []}, "permissions": {}}}}
    )
    assert _discover(home, managed) == expected


@pytest.mark.parametrize("permission", ['{"gh_*": "deny"}', "not json"])
def test_environment_permissions_are_reported_without_overriding_the_server(
    home: Path, managed: Path, permission: str
) -> None:
    _global(home, {"mcp": {"gh": _local("cmd")}})
    (candidate,) = _discover(home, managed, OPENCODE_PERMISSION=permission)
    assert candidate.config is not None and candidate.config.allowed_tools is None
    assert candidate.warnings == (
        ImportWarning(ImportWarningKind.TOOL_PERMISSIONS_IGNORED, original="OPENCODE_PERMISSION"),
    )


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


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("  https://x.test/mcp  ", "https://x.test/mcp"),
        ("\x00\thttps://x.test/mcp\r\n", "https://x.test/mcp"),
        ("https://x.test/path name?label=a b", "https://x.test/path name?label=a b"),
        ("https://x.te\tst/mcp", "https://x.test/mcp"),
    ],
)
def test_urls_keep_source_whitespace_normalization_without_losing_path_or_query_spaces(
    home: Path, managed: Path, url: str, expected: str
) -> None:
    _global(home, {"mcp": {"api": _remote(url, oauth=False)}})

    (candidate,) = _discover(home, managed)

    assert candidate.config is not None
    assert candidate.config.url == expected


@pytest.mark.parametrize("url", ["https://bad host.test/mcp", "https://bad\x01host.test/mcp"])
def test_url_host_whitespace_and_controls_are_not_accepted(home: Path, managed: Path, url: str) -> None:
    _global(home, {"mcp": {"api": _remote(url, oauth=False)}})

    assert _discover(home, managed) == ()


def test_header_case_aliases_follow_web_headers_order_and_keep_dynamic_references(home: Path, managed: Path) -> None:
    _global(
        home,
        {
            "mcp": {
                "api": _remote(
                    "https://x.test/mcp",
                    oauth=False,
                    headers={"X-Test": " upper ", "x-test": "{env:LOWER}", "X-TEST": " last "},
                )
            }
        },
    )

    (candidate,) = _discover(home, managed, LOWER="never-copy-this-value")

    assert candidate.config is not None
    assert candidate.config.headers == {"X-Test": "upper, {{LOWER}}, last"}


@pytest.mark.parametrize(
    ("headers", "expected"),
    [
        ({"X-Test": "first", "x-test": ""}, "first,"),
        ({"X-Test": "first", "x-test": "", "X-TEST": "last"}, "first, , last"),
        ({"X-Test": "", "x-test": "last"}, ", last"),
        ({"X-Test": "", "x-test": ""}, ","),
    ],
)
def test_empty_header_alias_values_preserve_source_wire_value(
    home: Path, managed: Path, headers: dict[str, str], expected: str
) -> None:
    _global(home, {"mcp": {"api": _remote("https://x.test/mcp", oauth=False, headers=headers)}})

    (candidate,) = _discover(home, managed)

    assert candidate.config is not None
    assert candidate.config.headers == {"X-Test": expected}


@pytest.mark.parametrize(
    ("timeout", "seconds", "source_ms"),
    [(1000, 1, None), (1501, 2, 1501), (10**300, MAX_REQUEST_TIMEOUT_SECONDS, 10**300)],
)
def test_preview_keeps_timeout_source_only_when_rounding_or_capping_changes_it(
    home: Path, managed: Path, timeout: int, seconds: int, source_ms: int | None
) -> None:
    _global(home, {"mcp": {"api": _local("cmd", timeout=timeout)}})

    (candidate,) = _discover(home, managed)

    assert candidate.config is not None
    assert candidate.config.request_timeout == seconds
    assert candidate.timeout_source_ms == source_ms


def test_preview_uses_the_effective_global_timeout_source(home: Path, managed: Path) -> None:
    _global(home, {"experimental": {"mcp_timeout": 4501}, "mcp": {"api": _local("cmd")}})

    (candidate,) = _discover(home, managed)

    assert candidate.config is not None
    assert candidate.config.request_timeout == 5
    assert candidate.timeout_source_ms == 4501


def test_relative_working_directory_warning_records_its_source_base(home: Path, workspace: Path, managed: Path) -> None:
    _global(home, {"mcp": {"relative": _local("cmd", cwd="../tools"), "absolute": _local("cmd", cwd=str(home))}})

    candidates = _by_name(_discover(home, managed, workspace))

    assert candidates["relative"].warnings == (
        ImportWarning(ImportWarningKind.CWD_IGNORED, field=str(workspace), original="../tools"),
    )
    assert candidates["absolute"].warnings == (ImportWarning(ImportWarningKind.CWD_IGNORED, original=str(home)),)


@pytest.mark.parametrize(
    ("entry", "field", "variable"),
    [
        (_local("cmd", environment={"TOKEN": "{env:BAD-NAME}"}), "mcp.s.environment.TOKEN", "BAD-NAME"),
        (_remote("https://x.test", headers={"X-Test": "{{env:A}"}), "mcp.s.headers.X-Test", ""),
        (_local("cmd", "{env:ARG}"), "mcp.s.command[1]", ""),
        (_local("cmd", environment={"TOKEN": "{file:secret}"}), "mcp.s.environment.TOKEN", ""),
        (_remote("{file:url}"), "mcp.s.url", ""),
    ],
)
def test_unsupported_metadata_locates_the_setting_without_copying_its_value(
    home: Path, managed: Path, entry: dict[str, Any], field: str, variable: str
) -> None:
    _global(home, {"mcp": {"s": entry}})

    (candidate,) = _discover(home, managed)

    assert candidate.config is None
    assert (candidate.unsupported_field, candidate.unsupported_variable) == (field, variable)


def test_only_escaped_literal_values_get_the_brace_notice(home: Path, managed: Path) -> None:
    _global(
        home,
        {
            "mcp": {
                "literal": _local("cmd", environment={"VALUE": "{{literal}} {env:NAME}"}),
                "variable": _local("cmd", environment={"VALUE": "{env:NAME}"}),
                "command": _local("cmd", "{{literal}}"),
            }
        },
    )

    candidates = _by_name(_discover(home, managed))

    assert candidates["literal"].warnings == (ImportWarning(ImportWarningKind.LITERAL_BRACES_ESCAPED),)
    assert candidates["variable"].warnings == candidates["command"].warnings == ()


def test_jsonc_error_position_matches_the_original_comments(home: Path, managed: Path) -> None:
    text = '{\n  /* comment\n     on two lines */\n  // another comment\n  "mcp": [,]\n}'
    config_file = _global(home, text, name="opencode.jsonc")

    result = OpenCodeSource().discover(_context(home, managed))

    assert result.source_errors == (McpSourceError(str(config_file), SourceErrorKind.INVALID_JSON, line=5, column=11),)


@pytest.mark.parametrize(
    ("document", "field"),
    [
        ({"mcp": []}, "mcp"),
        ({"mcp": {"broken": {"type": "local", "command": "private-value"}}}, "mcp.broken"),
        ({"experimental": {"mcp_timeout": None}}, "experimental.mcp_timeout"),
    ],
)
def test_invalid_configuration_errors_locate_fields_without_values(
    home: Path, managed: Path, document: dict[str, Any], field: str
) -> None:
    config_file = _global(home, document)

    result = OpenCodeSource().discover(_context(home, managed))

    assert result.source_errors == (McpSourceError(str(config_file), SourceErrorKind.INVALID_CONFIG, field=field),)


@pytest.mark.parametrize(
    ("name", "entry", "field"),
    [
        ("{env:SERVER}", _local("cmd"), "mcp.{env:SERVER}"),
        ("s", _local("cmd", environment={"{env:ENV_NAME}": "value"}), "mcp.s.environment.{env:ENV_NAME}"),
        ("s", _remote("https://x.test", headers={"{env:HEADER}": "value"}), "mcp.s.headers.{env:HEADER}"),
        ("s", {"type": "local", "{env:COMMAND}": ["cmd"]}, "mcp.s.{env:COMMAND}"),
        ("s", {"{env:TYPE}": "local", "command": ["cmd"]}, "mcp.s.{env:TYPE}"),
        ("s", _remote("https://x.test", oauth={"{env:CLIENT}": "id"}), "mcp.s.oauth.{env:CLIENT}"),
        (
            "s",
            _remote("https://x.test", disabled=True, oauth={"{env:CLIENT}": "id"}),
            "mcp.s.oauth.{env:CLIENT}",
        ),
    ],
)
def test_dynamic_names_are_unsupported_before_static_schema_validation(
    home: Path, managed: Path, name: str, entry: dict[str, Any], field: str
) -> None:
    _global(home, {"mcp": {name: entry}})

    result = OpenCodeSource().discover(_context(home, managed, SERVER="private-secret-server", HEADER="X-Test"))

    assert result.source_errors == ()
    (candidate,) = result.candidates
    assert candidate.config is None
    assert candidate.unsupported is UnsupportedReason.TEMPLATE_IN_FIELD_NAME
    assert candidate.name == name
    assert candidate.unsupported_field == field
    assert "private-secret-server" not in repr(candidate)


def test_file_references_in_names_are_not_read(home: Path, managed: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from chrys.service.mcp.importers import opencode as importer

    secret_file = home / "private-name.txt"
    secret_file.write_text("private-secret-header", encoding="utf-8")
    field = "{file:" + str(secret_file) + "}"
    _global(home, {"mcp": {"s": _remote("https://x.test", headers={field: "value"})}})
    read_source = importer.read_source_bytes

    def guarded_read(path: Path) -> bytes | None:
        assert path != secret_file
        return read_source(path)

    monkeypatch.setattr(importer, "read_source_bytes", guarded_read)

    (candidate,) = _discover(home, managed)

    assert candidate.unsupported is UnsupportedReason.TEMPLATE_IN_FIELD_NAME
    assert candidate.unsupported_field == "mcp.s.headers." + field
    assert "private-secret-header" not in repr(candidate)


def test_overwritten_dynamic_names_do_not_block_the_effective_configuration(
    home: Path, managed: Path, workspace: Path
) -> None:
    _global(
        home,
        {"mcp": {"s": _remote("https://x.test", disabled=True, oauth={"{env:CLIENT}": "id"})}},
    )
    _write(workspace / "opencode.json", {"mcp": {"s": {"type": "remote", "url": "https://x.test", "oauth": False}}})

    result = OpenCodeSource().discover(_context(home, managed, workspace))

    assert result.source_errors == ()
    (candidate,) = result.candidates
    assert candidate.unsupported is None
    assert candidate.config is not None
    assert candidate.config.enabled is False


def test_effective_dynamic_name_uses_the_layer_that_still_contributes_it(
    home: Path, managed: Path, workspace: Path
) -> None:
    source = _global(home, {"mcp": {"s": _remote("https://x.test", oauth={"{env:CLIENT}": "id"})}})
    _write(workspace / "opencode.json", {"mcp": {"s": {"type": "remote", "url": "https://project.test"}}})

    (candidate,) = _discover(home, managed, workspace)

    assert candidate.unsupported is UnsupportedReason.TEMPLATE_IN_FIELD_NAME
    assert candidate.source_path == str(source)


def test_dynamic_names_outside_mcp_do_not_affect_import(home: Path, managed: Path) -> None:
    _global(home, {"unrelated": {"{env:OTHER}": "value"}, "mcp": {"s": _local("cmd")}})

    (candidate,) = _discover(home, managed)

    assert candidate.config is not None
