# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Codex HTTP header validation and precedence at the import boundary."""

from __future__ import annotations

import pytest

from chrys.service.mcp.importers._codex_headers import (
    HeaderIgnoreReason,
    IgnoredCodexHeader,
    normalize_codex_headers,
)


def test_valid_literals_keep_unicode_empty_and_internal_tabs() -> None:
    result = normalize_codex_headers(
        {"X-Unicode": "你好", "X-Empty": "", "X-Tab": " \tvalue\tinside\t ", "X-Literal": "{{literal}}"},
        {"X-Dynamic": "TOKEN"},
    )

    assert result.static_headers == {
        "x-unicode": "你好",
        "x-empty": "",
        "x-tab": "value\tinside",
        "x-literal": "{{literal}}",
    }
    assert result.env_headers == {"x-dynamic": "TOKEN"}
    assert result.ignored == ()
    assert result.conflicts == ()


@pytest.mark.parametrize("name", ["", "Bad Header", "X:Test", "X-你好", "X-\nTest", " X-Test"])
def test_invalid_names_are_omitted_from_both_maps(name: str) -> None:
    result = normalize_codex_headers({name: "secret-value"}, {name: "TOKEN"})

    assert result.static_headers == {}
    assert result.env_headers == {}
    assert result.ignored == (
        IgnoredCodexHeader(f"http_headers.{name}", HeaderIgnoreReason.INVALID_NAME),
        IgnoredCodexHeader(f"env_http_headers.{name}", HeaderIgnoreReason.INVALID_NAME),
    )
    assert result.conflicts == ()
    assert "secret-value" not in repr(result)


@pytest.mark.parametrize(
    "value", ["\nsecret\n", "\rsecret\r", "secret\nvalue", "\x00secret", "\x01secret", "\x7fsecret", "\ud800"]
)
def test_invalid_original_values_are_omitted_before_whitespace_trimming(value: str) -> None:
    result = normalize_codex_headers({"X-Test": value}, {})

    assert result.static_headers == {}
    assert result.ignored == (IgnoredCodexHeader("http_headers.X-Test", HeaderIgnoreReason.INVALID_VALUE),)
    assert result.conflicts == ()
    assert "secret" not in repr(result)


def test_identical_case_aliases_are_compared_by_server_observed_value() -> None:
    result = normalize_codex_headers(
        {"x-test": " same ", "X-Test": "\tsame\t"},
        {"x-env": "TOKEN", "X-Env": "TOKEN"},
    )

    assert result.static_headers == {"x-test": "same"}
    assert result.env_headers == {"x-env": "TOKEN"}
    assert result.conflicts == ()
    assert result.ignored == ()


def test_different_case_aliases_report_field_names_without_secret_values() -> None:
    result = normalize_codex_headers(
        {"x-static": "secret-one", "X-Static": "secret-two"},
        {"x-env": "FIRST_TOKEN", "X-Env": "SECOND_TOKEN"},
    )

    assert result.static_headers == {}
    assert result.env_headers == {}
    assert result.conflicts == ("x-env", "x-static")
    assert result.ignored == ()
    assert "secret" not in repr(result)
    assert "TOKEN" not in repr(result)


def test_static_and_environment_precedence_is_not_a_case_conflict() -> None:
    result = normalize_codex_headers({"X-Test": "literal"}, {"x-test": "TOKEN"})

    assert result.static_headers == {"x-test": "literal"}
    assert result.env_headers == {"x-test": "TOKEN"}
    assert result.conflicts == ()


def test_environment_reference_masks_static_alias_conflict() -> None:
    result = normalize_codex_headers({"X-Test": "one", "x-test": "two"}, {"X-Test": "TOKEN"})

    assert result.static_headers == {}
    assert result.env_headers == {"x-test": "TOKEN"}
    assert result.conflicts == ()


def test_invalid_environment_header_name_cannot_mask_a_static_conflict() -> None:
    result = normalize_codex_headers({"X-Test": "one", "x-test": "two"}, {" X-Test": "TOKEN"})

    assert result.conflicts == ("x-test",)
    assert result.env_headers == {}
    assert result.ignored == (IgnoredCodexHeader("env_http_headers. X-Test", HeaderIgnoreReason.INVALID_NAME),)


def test_bearer_masks_authorization_aliases_in_both_maps() -> None:
    result = normalize_codex_headers(
        {"authorization": "secret-one", "Authorization": "secret-two", "X-Test": "kept"},
        {"Authorization": "FIRST_TOKEN", "AUTHORIZATION": "SECOND_TOKEN"},
        bearer_authorization=True,
    )

    assert result.static_headers == {"x-test": "kept"}
    assert result.env_headers == {}
    assert result.conflicts == ()
    assert result.ignored == ()
    assert "secret" not in repr(result)


def test_without_bearer_authorization_alias_conflict_is_effective() -> None:
    result = normalize_codex_headers({}, {"Authorization": "FIRST", "AUTHORIZATION": "SECOND"})

    assert result.conflicts == ("authorization",)


def test_bearer_discards_an_unrepresentable_authorization_variable() -> None:
    result = normalize_codex_headers({}, {"Authorization": "IGNORED-INVALID"}, bearer_authorization=True)

    assert result.env_headers == {}
    assert result.conflicts == ()
    assert result.ignored == ()


def test_variable_name_validation_is_left_to_the_caller() -> None:
    result = normalize_codex_headers({}, {"X-Test": "BAD-NAME"})

    assert result.env_headers == {"x-test": "BAD-NAME"}
    assert result.ignored == ()
    assert result.conflicts == ()
