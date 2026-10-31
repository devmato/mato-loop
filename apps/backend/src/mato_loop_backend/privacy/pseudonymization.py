from __future__ import annotations

import copy
import hmac
import json
import re
import secrets
from collections import OrderedDict
from collections.abc import Callable
from functools import lru_cache
from sys import getsizeof
from threading import Lock
from typing import Any

from detect_secrets.core.scan import scan_line
from detect_secrets.plugins.keyword import KeywordDetector
from detect_secrets.settings import transient_settings
from presidio_analyzer import EntityRecognizer, RecognizerResult
from presidio_analyzer.predefined_recognizers import (
    CreditCardRecognizer,
    EmailRecognizer,
    IbanRecognizer,
    IpRecognizer,
    PhoneRecognizer,
)

_PROTECTION_CACHE_MAX_BYTES = 16 * 1024 * 1024
_SESSION_PROTECTION_CACHE_MAX_BYTES = 64 * 1024 * 1024


class ProtectionCache:
    def __init__(self, max_bytes: int = _PROTECTION_CACHE_MAX_BYTES) -> None:
        self._entries: OrderedDict[
            tuple[bytes, str, str | None], tuple[str, dict[str, str], int]
        ] = OrderedDict()
        self._size_bytes = 0
        self._lock = Lock()
        self._max_bytes = max_bytes

    def get(self, key: tuple[str, str | None]) -> tuple[str, dict[str, str]] | None:
        with self._lock:
            entry = self._entries.get(key)
            if entry is not None:
                self._entries.move_to_end(key)
                return entry[0], entry[1]
        return None

    def store(self, key: tuple[str, str | None], protected: str, originals: dict[str, str]) -> None:
        # Allow for containers and conservatively count shared strings twice.
        size = (256 + sum(getsizeof(value) for value in (*key, protected)) + getsizeof(originals)
                + sum(getsizeof(marker) + getsizeof(original) for marker, original in originals.items()))
        if size > self._max_bytes:
            return
        with self._lock:
            previous = self._entries.pop(key, None)
            if previous is not None:
                self._size_bytes -= previous[2]
            while self._size_bytes + size > self._max_bytes:
                _, oldest = self._entries.popitem(last=False)
                self._size_bytes -= oldest[2]
            self._entries[key] = (protected, originals, size)
            self._size_bytes += size


_SESSION_PROTECTION_CACHE = ProtectionCache(_SESSION_PROTECTION_CACHE_MAX_BYTES)


def shared_protection_cache() -> ProtectionCache:
    return _SESSION_PROTECTION_CACHE


_SECRET_SCAN_LOCK = Lock()
_MARKER_PATTERN = re.compile(r"\[\[MLP_.*?(?:\]\]|$)", re.DOTALL)
_TRANSMITTED_MARKER_PATTERN = re.compile(r"\[\[MLP_[A-F0-9]{32}\]\]")
_SECRET_SCAN_SETTINGS = {
    "plugins_used": [
        {"name": name}
        for name in (
            "ArtifactoryDetector",
            "AWSKeyDetector",
            "AzureStorageKeyDetector",
            "BasicAuthDetector",
            "CloudantDetector",
            "DiscordBotTokenDetector",
            "GitHubTokenDetector",
            "GitLabTokenDetector",
            "IbmCloudIamDetector",
            "IbmCosHmacDetector",
            "JwtTokenDetector",
            "MailchimpDetector",
            "NpmDetector",
            "OpenAIDetector",
            "PrivateKeyDetector",
            "PypiTokenDetector",
            "SendGridDetector",
            "SlackDetector",
            "SoftlayerDetector",
            "SquareOAuthDetector",
            "StripeDetector",
            "TelegramBotTokenDetector",
            "TwilioKeyDetector",
        )
    ],
    "filters_used": [],
}


class PseudonymizationError(RuntimeError):
    pass


@lru_cache(maxsize=1)
def _recognizers() -> tuple[EntityRecognizer, ...]:
    try:
        return (
            CreditCardRecognizer(supported_language="de"),
            EmailRecognizer(supported_language="de"),
            IbanRecognizer(supported_language="de"),
            IpRecognizer(supported_language="de"),
            PhoneRecognizer(supported_language="de", leniency=3),
        )
    except Exception as exc:
        raise PseudonymizationError("privacy.pseudonymizationUnavailable") from exc


def _expand_private_key_span(text: str, start: int, end: int) -> tuple[int, int]:
    line_start = text.rfind("\n", 0, start) + 1
    line_end = text.find("\n", end)
    if line_end < 0:
        line_end = len(text)
    header = text[line_start:line_end].strip()

    if header.startswith("-----BEGIN ") and header.endswith("-----"):
        label = header[len("-----BEGIN ") : -len("-----")]
        end_marker = f"-----END {label}-----"
        block_end = text.find(end_marker, line_end)
        if block_end >= 0:
            return line_start, block_end + len(end_marker)

    if header.startswith("PuTTY-User-Key-File-"):
        block_end = text.find("\nPrivate-MAC:", line_end)
        if block_end >= 0:
            mac_end = text.find("\n", block_end + 1)
            return line_start, len(text) if mac_end < 0 else mac_end

    return start, end


def _secret_results(text: str) -> list[RecognizerResult]:
    results: list[RecognizerResult] = []
    offset = 0
    keyword_detector = KeywordDetector()
    with _SECRET_SCAN_LOCK, transient_settings(_SECRET_SCAN_SETTINGS):
        for line_with_ending in text.splitlines(keepends=True):
            line = line_with_ending.rstrip("\r\n")
            potentials = list(scan_line(line))
            potentials.extend(
                keyword_detector.analyze_line(
                    filename="message.yaml",
                    line=line,
                )
            )
            for potential in potentials:
                secret = potential.secret_value
                if not secret:
                    continue
                position = 0
                while (position := line.find(secret, position)) >= 0:
                    start = offset + position
                    end = start + len(secret)
                    if potential.type == "Private Key":
                        start, end = _expand_private_key_span(text, start, end)
                    results.append(
                        RecognizerResult(
                            entity_type="SECRET",
                            start=start,
                            end=end,
                            score=1.0,
                        )
                    )
                    position += len(secret)
            offset += len(line_with_ending)
    return results


def _analyze(text: str) -> list[RecognizerResult]:
    results = _secret_results(text)
    for recognizer in _recognizers():
        current = recognizer.analyze(
            text=text,
            entities=recognizer.supported_entities,
            nlp_artifacts=None,
        )
        minimum_score = 0.4 if isinstance(recognizer, PhoneRecognizer) else 0.45
        results.extend(result for result in current if result.score >= minimum_score)
    return results


class RequestPseudonymizer:
    _marker_instruction = (
        "Host privacy placeholders are opaque values. Copy every placeholder exactly and "
        "unchanged in answers and tool arguments; never interpret, abbreviate, or reformat it. "
        "Never create a new privacy placeholder. Only use placeholders that already appear "
        "in the input."
    )

    def __init__(
        self, *, key: bytes | None = None,
        protection_cache: ProtectionCache | None = None,
    ) -> None:
        # Restoration is request-local; a shared cache is keyed by an HMAC of this key.
        self._key = key if key is not None else secrets.token_bytes(32)
        self._originals: dict[str, str] = {}
        self._protection_cache = protection_cache
        self._protection_cache_key = None

    @property
    def max_marker_length(self) -> int:
        return len(self.marker_prefix) + 32 + 2

    @property
    def marker_prefix(self) -> str:
        return "[[MLP_"

    def retain_transmitted(self, body: dict[str, Any]) -> None:
        """Discard restoration authority for fields omitted by the transport serializer."""
        transmitted = set(_TRANSMITTED_MARKER_PATTERN.findall(json.dumps(body, ensure_ascii=False)))
        self._originals = {
            marker: original for marker, original in self._originals.items()
            if marker in transmitted
        }

    def protect_messages(self, messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
        try:
            protected = copy.deepcopy(messages)
            for message in protected:
                content = message.get("content")
                if isinstance(content, str):
                    if message.get("role") == "tool":
                        try:
                            value = json.loads(content)
                        except ValueError:
                            message["content"] = self.protect_text(content)
                        else:
                            # Store decoded originals so tool arguments restore real newlines, not JSON escapes.
                            message["content"] = json.dumps(self.protect_value(value, protect_keys=True), ensure_ascii=False)
                    else:
                        message["content"] = self.protect_text(content)
                if message.get("role") != "assistant":
                    # Only assistant messages can carry executable calls. Omitted arguments
                    # must not activate a raw marker supplied elsewhere in this request.
                    message.pop("tool_calls", None)
                tool_calls = message.get("tool_calls")
                if isinstance(tool_calls, list):
                    for call in tool_calls:
                        if isinstance(call, dict) and "arguments" in call:
                            call["arguments"] = self.protect_value(call["arguments"])
                # Opaque continuation data cannot be inspected or safely pseudonymized.
                message.pop("provider_data", None)
            system_message = next(
                (
                    message
                    for message in protected
                    if message.get("role") == "system"
                    and isinstance(message.get("content"), str)
                ),
                None,
            )
            if system_message is None:
                protected.insert(0, {"role": "system", "content": self._marker_instruction})
            else:
                system_message["content"] += f"\n\n{self._marker_instruction}"
            return protected
        except PseudonymizationError:
            raise
        except Exception as exc:
            raise PseudonymizationError("privacy.pseudonymizationFailed") from exc

    def protect_value(self, value: Any, *, protect_keys: bool = False) -> Any:
        if isinstance(value, str):
            return self.protect_text(value)
        if isinstance(value, list):
            return [self.protect_value(item, protect_keys=protect_keys) for item in value]
        if isinstance(value, dict):
            return {(self.protect_text(key) if protect_keys else key):
                    self.protect_text(item, field_name=key) if isinstance(item, str)
                    else self.protect_value(item, protect_keys=protect_keys) for key, item in value.items()}
        return value

    def restore_value(self, value: Any) -> Any:
        if isinstance(value, str):
            return self.restore_text(value)
        if isinstance(value, list):
            return [self.restore_value(item) for item in value]
        if isinstance(value, dict):
            return {key: self.restore_value(item) for key, item in value.items()}
        return value

    def protect_text(self, text: str, *, field_name: str | None = None) -> str:
        if not text:
            return text
        cache_key = (text, field_name)
        cached = None
        if self._protection_cache is not None:
            if self._protection_cache_key is None:
                self._protection_cache_key = hmac.digest(self._key, b"matoloop:privacy-text-cache:v1", "sha256")
            cache_key = (self._protection_cache_key, *cache_key)
            cached = self._protection_cache.get(cache_key)
        if cached is not None:
            protected, originals = cached
            self._originals.update(originals)
            return protected
        try:
            results = _analyze(text)
            # Keep keyword-based protection for structured fields such as {"password": "value"}.
            if field_name is not None and KeywordDetector().analyze_line(
                filename="message.yaml", line=f'{json.dumps(field_name)}: "value"',
            ):
                results.append(RecognizerResult(entity_type="SECRET", start=0, end=len(text), score=1.0))
        except PseudonymizationError:
            raise
        except Exception as exc:
            raise PseudonymizationError("privacy.pseudonymizationFailed") from exc
        selected = []
        occupied: list[tuple[int, int]] = []
        for result in sorted(
            results,
            key=lambda item: (-item.score, -(item.end - item.start), item.start),
        ):
            if any(result.start < end and result.end > start for start, end in occupied):
                continue
            selected.append(result)
            occupied.append((result.start, result.end))
        protected = text
        detected: dict[str, str] = {}
        for result in sorted(selected, key=lambda item: item.start, reverse=True):
            original = text[result.start : result.end]
            identity = json.dumps((result.entity_type, original), ensure_ascii=False).encode()
            digest = hmac.digest(self._key, identity, "sha256").hex()[:32].upper()
            marker = f"{self.marker_prefix}{digest}]]"
            if marker in self._originals and self._originals[marker] != original:
                raise PseudonymizationError("privacy.pseudonymizationFailed")
            self._originals[marker] = detected[marker] = original
            protected = protected[: result.start] + marker + protected[result.end :]
        if self._protection_cache is not None:
            self._protection_cache.store(cache_key, protected, detected)
        return protected

    def restore_text(
        self,
        text: str,
        *,
        unknown_marker_replacement: str | None = None,
        on_unknown_marker: Callable[[], None] | None = None,
    ) -> str:
        def restore(match: re.Match[str]) -> str:
            marker = match.group()
            if marker in self._originals:
                return self._originals[marker]
            if unknown_marker_replacement is None:
                raise PseudonymizationError("privacy.pseudonymizationFailed")
            if on_unknown_marker is not None:
                on_unknown_marker()
            return unknown_marker_replacement

        return _MARKER_PATTERN.sub(restore, text)


class StreamingPseudonymRestorer:
    def __init__(
        self,
        pseudonymizer: RequestPseudonymizer,
        *,
        unknown_marker_replacement: str | None = None,
        on_unknown_marker: Callable[[], None] | None = None,
    ) -> None:
        self._pseudonymizer = pseudonymizer
        self._prefix = pseudonymizer.marker_prefix
        self._unknown_marker_replacement = unknown_marker_replacement
        self._on_unknown_marker = on_unknown_marker
        self._buffer = ""

    def feed(self, text: str) -> str:
        self._buffer += text
        restored: list[str] = []
        while self._buffer:
            marker_start = self._buffer.find(self._prefix)
            if marker_start < 0:
                retained = 0
                for length in range(1, min(len(self._buffer), len(self._prefix) - 1) + 1):
                    if self._prefix.startswith(self._buffer[-length:]):
                        retained = length
                safe_end = len(self._buffer) - retained
                restored.append(self._buffer[:safe_end])
                self._buffer = self._buffer[safe_end:]
                break
            if marker_start > 0:
                restored.append(self._buffer[:marker_start])
                self._buffer = self._buffer[marker_start:]
                continue
            marker_end = self._buffer.find("]]", len(self._prefix))
            if marker_end < 0:
                if (
                    self._pseudonymizer.max_marker_length
                    and len(self._buffer) > self._pseudonymizer.max_marker_length
                ):
                    if self._unknown_marker_replacement is None:
                        raise PseudonymizationError("privacy.pseudonymizationFailed")
                    restored.append(self._unknown_marker_replacement)
                    self._buffer = self._buffer[
                        self._pseudonymizer.max_marker_length :
                    ]
                    if self._on_unknown_marker is not None:
                        self._on_unknown_marker()
                    continue
                break
            marker_end += 2
            restored.append(
                self._pseudonymizer.restore_text(
                    self._buffer[:marker_end],
                    unknown_marker_replacement=self._unknown_marker_replacement,
                    on_unknown_marker=self._on_unknown_marker,
                )
            )
            self._buffer = self._buffer[marker_end:]
        return "".join(restored)

    def flush(self) -> str:
        remaining = self._pseudonymizer.restore_text(
            self._buffer,
            unknown_marker_replacement=self._unknown_marker_replacement,
            on_unknown_marker=self._on_unknown_marker,
        )
        self._buffer = ""
        return remaining
