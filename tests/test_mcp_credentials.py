"""Table-driven tests for the shared prose-aware MCP credential detector.

All negative cases use invented synthetic values inside isolated tests; no real
credential is ever referenced. The exact ordinary sentences from
evrenesat/aworkflow#52 and its 2026-10-02 owner reproduction are the literal
acceptance fixtures.
"""

from __future__ import annotations

import pytest

from aflow.mcp_credentials import contains_mcp_credential

# Ordinary prose from issue #52 and its owner reproduction, plus the plan's
# literal distinguishing acceptance fixtures.
ACCEPT_CASES = [
    # Exact reported sentences (issue #52 body and 2026-10-02 reproduction).
    ("issue-body-sentence", "Keep bearer credentials out of prompts"),
    ("issue-reproduction-phrase", "Use explicit environment bearer support"),
    # Uppercase scheme wording with a comma after the following ordinary noun.
    ("uppercase-comma", "Keep Bearer credentials, out of prompts"),
    # Sentence ending in the ordinary word "support." after the scheme word.
    ("ends-support", "Provide explicit environment bearer support."),
    # The noun "(credentials)" in parentheses within a longer explanatory sentence.
    ("paren-noun", "Store the bearer (credentials) in the vault, not prompts."),
    # Longer explanatory sentence using an actual nonbreaking space.
    ("nbsp-sentence", "Keep bearer\u00a0credentials out of prompts and logs"),
    # A plain "bearer token" scheme mention and other ordinary words.
    ("bearer-token-prose", "A bearer token is a standard HTTP scheme."),
    ("bearer-tab-prose", "Keep bearer\tcredentials out of prompts"),
]

# Credential-shaped values that must be rejected, keyed by the rule they prove.
REJECT_CASES = [
    # Rule 3: whole value is Bearer <alphabetic candidate>.
    ("rule3-whole-alphabetic", "Bearer shortword"),
    ("rule3-whole-lowercase", "bearer shortword"),
    ("rule3-whole-tab", "Bearer\tshortword"),
    ("rule3-whole-nbsp", "Bearer\u00a0shortword"),
    # Rule 2: explicit Authorization label with a Bearer value.
    ("rule2-explicit-header", "Authorization: Bearer shortword"),
    ("rule2-explicit-header-eq", "Authorization=Bearer shortword"),
    ("rule2-serialized-quoted-header", '{"Authorization": "Bearer shortword"}'),
    ("rule2-serialized-single-quote", "{'Authorization': 'Bearer shortword'}"),
    ("rule2-fenced", "```\nAuthorization: Bearer shortword\n```"),
    # Rule 4: token-shaped candidate inside a longer sentence.
    ("rule4-hyphen-digit", "Use Bearer fake-token-52 to authenticate."),
    ("rule4-alnum-digit", "The header is `Bearer Ab12` here."),
    ("rule4-jwt-dots", "Set (Bearer abc.def.ghi) in the config."),
    ("rule4-base64-padding", 'Send "Bearer YWJjZA==" with the request.'),
    ("rule4-parens", "Configure Bearer (fake-token-52) for the probe."),
    # Rule 1: existing assignment/query patterns (preserved unchanged).
    ("rule1-token-query", "aflow://projects/x?token=secret123"),
    ("rule1-access-token", "url?access_token=abc123"),
    ("rule1-assignment", "export authorization=abc123"),
]


@pytest.mark.parametrize(("label", "value"), ACCEPT_CASES, ids=[c[0] for c in ACCEPT_CASES])
def test_accepts_ordinary_prose(label: str, value: object) -> None:
    assert contains_mcp_credential(value) is False


@pytest.mark.parametrize(("label", "value"), REJECT_CASES, ids=[c[0] for c in REJECT_CASES])
def test_rejects_credential_shapes(label: str, value: object) -> None:
    assert contains_mcp_credential(value) is True


@pytest.mark.parametrize(
    "value",
    [42, None, True, 3.5, b"Bearer shortword", ("Bearer", "shortword")[:1]],
    ids=["int", "none", "bool", "float", "bytes", "bare-tuple"],
)
def test_scalars_other_than_strings_are_not_credentials(value: object) -> None:
    # Non-string scalars are never credentials; a one-element tuple of the bare
    # word "Bearer" carries no scheme/value pair.
    assert contains_mcp_credential(value) is False


def test_nested_mapping_and_list_recursion() -> None:
    benign = "Keep bearer credentials out of prompts"
    assert contains_mcp_credential({"note": benign, "items": [benign, "plain"]}) is False
    assert contains_mcp_credential({"note": benign, "items": ["Bearer fake-token-52"]}) is True
    assert contains_mcp_credential({"headers": {"Authorization": "Bearer shortword"}}) is True


def test_tuple_set_frozenset_recursion() -> None:
    assert contains_mcp_credential(("Bearer fake-token-52",)) is True
    assert contains_mcp_credential({"Bearer YWJjZA==", "plain"}) is True
    assert contains_mcp_credential(frozenset({"Bearer Ab12"})) is True
    assert contains_mcp_credential(("keep bearer credentials",)) is False


def test_safe_sibling_does_not_cancel_rejecting_nested_value() -> None:
    payload = {
        "safe": "Use explicit environment bearer support.",
        "nested": [
            {"label": "ordinary"},
            {"value": "Authorization: Bearer shortword"},
        ],
    }
    assert contains_mcp_credential(payload) is True


def test_mixed_case_and_punctuation_do_not_escape() -> None:
    assert contains_mcp_credential("USE BEARER FAKE-TOKEN-52 NOW") is True
    assert contains_mcp_credential("bearer fake-token-52,") is True
    assert contains_mcp_credential("Bearer fake-token-52!") is True


def test_shared_tool_and_resource_guards_reject_before_operation() -> None:
    """Prove the shared registry guards use the same decision and never run ops."""
    from fastmcp.exceptions import ResourceError, ToolError

    from aflow.mcp_control_plane import _resource_result, _tool_result

    calls: list[str] = []

    def benign() -> dict[str, object]:
        calls.append("ran")
        return {"ok": True}

    # A benign credential-free tool argument runs the operation unchanged.
    assert _tool_result(
        benign, {"note": "keep bearer credentials out of prompts"}
    ) == {"ok": True}
    assert calls == ["ran"]

    # A benign resource result returns the unchanged operation result.
    benign_document = {"document": "Keep bearer credentials out of prompts"}

    def benign_resource() -> dict[str, object]:
        calls.append("resource-ran")
        return benign_document

    assert _resource_result(
        benign_resource, {"uri": "aflow://projects/x/capabilities"}
    ) == benign_document
    assert calls == ["ran", "resource-ran"]

    def should_not_run() -> dict[str, object]:
        raise AssertionError("operation must not run")

    # Credential-shaped nested tool arguments raise the safe ToolError mapping.
    with pytest.raises(ToolError) as tool_exc:
        _tool_result(
            should_not_run,
            {
                "idempotency_key": "Bearer fake-token-52",
                "note": "keep bearer credentials out of prompts",
            },
        )
    assert str(tool_exc.value) == "operation_rejected"
    assert "fake-token-52" not in str(tool_exc.value)
    assert calls == ["ran", "resource-ran"]

    # A credential-shaped resource URI raises the safe ResourceError mapping.
    with pytest.raises(ResourceError) as resource_exc:
        _resource_result(should_not_run, {"uri": "aflow://projects/x?token=secret123"})
    assert str(resource_exc.value) == "operation_rejected"
    assert "secret123" not in str(resource_exc.value)
    assert calls == ["ran", "resource-ran"]

    # The same nested payload is rejected by each direct guard before operation.
    nested_payload = {
        "note": "Keep bearer credentials out of prompts",
        "nested": [{"value": "Bearer fake-token-52"}],
    }

    with pytest.raises(ToolError) as nested_tool_exc:
        _tool_result(should_not_run, nested_payload)
    assert str(nested_tool_exc.value) == "operation_rejected"
    assert "fake-token-52" not in str(nested_tool_exc.value)
    assert calls == ["ran", "resource-ran"]

    with pytest.raises(ResourceError) as nested_resource_exc:
        _resource_result(should_not_run, nested_payload)
    assert str(nested_resource_exc.value) == "operation_rejected"
    assert "fake-token-52" not in str(nested_resource_exc.value)
    assert calls == ["ran", "resource-ran"]
