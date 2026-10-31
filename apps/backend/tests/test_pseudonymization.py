import json

import pytest

from mato_loop_backend.privacy import (
    ProtectionCache,
    PseudonymizationError,
    RequestPseudonymizer,
    StreamingPseudonymRestorer,
)
from mato_loop_backend.privacy import pseudonymization as pseudonymization_module


def test_pseudonymizer_reuses_markers_and_restores_nested_values() -> None:
    pseudonymizer = RequestPseudonymizer()
    protected = pseudonymizer.protect_value(
        {
            "recipient": "anna@example.com",
            "items": ["anna@example.com", "password=supersecret"],
        }
    )

    assert "anna@example.com" not in str(protected)
    assert "supersecret" not in str(protected)
    assert protected["recipient"] == protected["items"][0]
    assert pseudonymizer.restore_value(protected) == {
        "recipient": "anna@example.com",
        "items": ["anna@example.com", "password=supersecret"],
    }


@pytest.mark.parametrize("payload, secret", [
    ({"password": "supersecret"}, "supersecret"),
    ({"anna@example.com": "member"}, "anna@example.com"),
])
def test_json_tool_result_keeps_secret_field_detection(payload, secret):
    messages = RequestPseudonymizer().protect_messages([
        {"role": "tool", "content": json.dumps(payload)},
    ])
    assert secret not in messages[-1]["content"] and "[[MLP_" in messages[-1]["content"]


def test_pseudonymizer_preserves_names_locations_and_technical_terms() -> None:
    pseudonymizer = RequestPseudonymizer()
    source = (
        "Anna Müller nutzt docker compose in Berlin mit lemonade, llama und modelmanager."
    )

    protected = pseudonymizer.protect_text(source)

    assert protected == source
    assert pseudonymizer.restore_text(protected) == source


@pytest.mark.parametrize(
    "sensitive_value",
    [
        "anna@example.com",
        "192.0.2.1",
        "DE89370400440532013000",
        "4111 1111 1111 1111",
        "+49 30 123456",
    ],
)
def test_pseudonymizer_protects_valid_identifiers(sensitive_value: str) -> None:
    pseudonymizer = RequestPseudonymizer()

    protected = pseudonymizer.protect_text(f"Wert: {sensitive_value}")

    assert sensitive_value not in protected
    assert pseudonymizer.restore_text(protected) == f"Wert: {sensitive_value}"


def test_pseudonymizer_protects_structured_secret_and_complete_private_key() -> None:
    pseudonymizer = RequestPseudonymizer()
    key_label = "PRIVATE KEY"
    private_key = "\n".join(
        (
            f"-----BEGIN {key_label}-----",
            "ZmFrZS1wcml2YXRlLWtleS1tYXRlcmlhbA==",
            f"-----END {key_label}-----",
        )
    )
    github_token = "ghp_" + "a1B2c3D4e5F6g7H8i9J0k1L2m3N4o5P6q7R8"
    source = f'github_token: "{github_token}"\n{private_key}'

    protected = pseudonymizer.protect_text(source)

    assert "ghp_" not in protected
    assert "BEGIN PRIVATE KEY" not in protected
    assert "ZmFrZS" not in protected
    assert pseudonymizer.restore_text(protected) == source


def test_protect_messages_keeps_marker_instruction_stable() -> None:
    pseudonymizer = RequestPseudonymizer()

    protected = pseudonymizer.protect_messages(
        [
            {"role": "system", "content": "Systemvertrag"},
            {"role": "user", "content": "Kontakt: anna@example.com"},
        ]
    )

    assert protected[0]["content"].startswith("Systemvertrag\n\nHost privacy placeholders")
    assert "Never create a new privacy placeholder" in protected[0]["content"]
    assert "anna@example.com" not in protected[1]["content"]

    untouched = RequestPseudonymizer().protect_messages(
        [{"role": "system", "content": "Systemvertrag"}]
    )
    assert untouched[0] == protected[0]


def test_pseudonymizer_preserves_unavailable_error_code(monkeypatch) -> None:
    def unavailable():
        raise PseudonymizationError("privacy.pseudonymizationUnavailable")

    monkeypatch.setattr(pseudonymization_module, "_recognizers", unavailable)

    with pytest.raises(
        PseudonymizationError, match="privacy.pseudonymizationUnavailable"
    ):
        RequestPseudonymizer().protect_text("anna@example.com")


def test_streaming_restorer_handles_marker_boundaries() -> None:
    pseudonymizer = RequestPseudonymizer()
    protected = pseudonymizer.protect_text("anna@example.com")
    restorer = StreamingPseudonymRestorer(pseudonymizer)

    chunks = [restorer.feed(character) for character in "Kontakt: " + protected]
    chunks.append(restorer.flush())

    assert "".join(chunks) == "Kontakt: anna@example.com"


def test_streaming_restorer_rejects_unknown_request_marker() -> None:
    pseudonymizer = RequestPseudonymizer()
    protected = pseudonymizer.protect_text("anna@example.com")
    unknown = protected[:-2] + "UNKNOWN]]"
    restorer = StreamingPseudonymRestorer(pseudonymizer)

    with pytest.raises(PseudonymizationError, match="privacy.pseudonymizationFailed"):
        restorer.feed(unknown)


def test_visible_restoration_replaces_and_reports_unknown_markers() -> None:
    pseudonymizer = RequestPseudonymizer()
    protected = pseudonymizer.protect_text("anna@example.com")
    unknown = protected[:-2] + "UNKNOWN]]"
    reported = []

    assert pseudonymizer.restore_text(
        f"Kontakt: {unknown}",
        unknown_marker_replacement="[...]",
        on_unknown_marker=lambda: reported.append(True),
    ) == "Kontakt: [...]"
    assert reported == [True]
    with pytest.raises(PseudonymizationError, match="privacy.pseudonymizationFailed"):
        pseudonymizer.restore_value({"address": unknown})


def test_run_key_stabilizes_identity_without_sharing_restoration_authority() -> None:
    key = b"r" * 32
    first = RequestPseudonymizer(key=key)
    old = first.protect_text("old@example.com")
    shared = first.protect_text("shared@example.com")
    estimate = RequestPseudonymizer(key=key)
    estimate.protect_text("speculative@example.com")
    second = RequestPseudonymizer(key=key)
    assert second.protect_text("shared@example.com") == shared
    assert second.restore_text(shared) == "shared@example.com"
    assert RequestPseudonymizer().protect_text("shared@example.com") != shared
    with pytest.raises(PseudonymizationError):
        second.restore_value({"recipient": old})
    # Raw placeholders do not activate old values, even with the same run key.
    second.protect_messages([{"role": "user", "content": old}])
    with pytest.raises(PseudonymizationError):
        second.restore_text(old)
    second.retain_transmitted({"messages": [{"role": "user", "content": "No contact"}]})
    with pytest.raises(PseudonymizationError):
        second.restore_text(shared)


def test_protection_cache_rescans_only_new_text_and_separates_keys(monkeypatch) -> None:
    cache = ProtectionCache()
    key = b"r" * 32
    protected = RequestPseudonymizer(key=key, protection_cache=cache).protect_text(
        "anna@example.com"
    )
    analyze = pseudonymization_module._analyze
    calls = []
    monkeypatch.setattr(pseudonymization_module, "_analyze", lambda text: calls.append(text) or analyze(text))
    current = RequestPseudonymizer(key=key, protection_cache=cache)

    assert current.protect_text("anna@example.com") == protected
    assert current.restore_text(protected) == "anna@example.com"
    current.protect_text("new@example.com")
    other = RequestPseudonymizer(key=b"o" * 32, protection_cache=cache)
    other_protected = other.protect_text("anna@example.com")
    assert other_protected != protected
    with pytest.raises(PseudonymizationError):
        current.restore_value(other_protected)
    assert calls == ["new@example.com", "anna@example.com"]


def test_protection_cache_evicts_old_candidates_and_skips_oversized_text(monkeypatch) -> None:
    calls = []
    monkeypatch.setattr(pseudonymization_module, "_analyze", lambda text: calls.append(text) or [])
    cache = ProtectionCache(2048)
    candidates = ["a" * 800, "b" * 800, "x" * 4096, "a" * 800]

    for text in candidates:
        RequestPseudonymizer(key=b"r" * 32, protection_cache=cache).protect_text(text)

    assert calls == candidates


def test_exact_originals_and_single_pass_restoration(monkeypatch) -> None:
    pseudonymizer = RequestPseudonymizer()
    original = ["password=CaseSensitive", "password=casesensitive"]
    protected = pseudonymizer.protect_value(original)
    assert protected[0] != protected[1]
    assert pseudonymizer.restore_value(protected) == original
    monkeypatch.setattr(pseudonymization_module, "_analyze", lambda text: [
        pseudonymization_module.RecognizerResult("SECRET", 0, len(text), 1.0)
    ])
    embedded = "secret containing [[MLP_original]]"
    assert pseudonymizer.restore_text(pseudonymizer.protect_text(embedded)) == embedded


def test_identity_collision_fails_closed(monkeypatch) -> None:
    monkeypatch.setattr(pseudonymization_module.hmac, "digest", lambda *args: b"x" * 32)
    pseudonymizer = RequestPseudonymizer()
    pseudonymizer.protect_text("first@example.com")
    with pytest.raises(PseudonymizationError):
        pseudonymizer.protect_text("second@example.com")

def test_streaming_visible_restoration_replaces_incomplete_marker() -> None:
    pseudonymizer = RequestPseudonymizer()
    pseudonymizer.protect_text("anna@example.com")
    reported = []
    restorer = StreamingPseudonymRestorer(
        pseudonymizer,
        unknown_marker_replacement="[...]",
        on_unknown_marker=lambda: reported.append(True),
    )

    restored = restorer.feed(f"Kontakt: {pseudonymizer.marker_prefix}9999")

    assert restored == "Kontakt: "
    assert restorer.flush() == "[...]"
    assert reported == [True]
