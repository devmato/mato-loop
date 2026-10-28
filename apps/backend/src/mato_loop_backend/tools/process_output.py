from __future__ import annotations

import re
from heapq import merge

_AUTH_HEADER_LINE = re.compile(
    r"(?:^|(?<=\r))[^\r\n]*\b(?:authorization|proxy-authorization|set-cookie|cookies?|x-csrftoken|x-csrf-token|x-xsrf-token)"
    r"(?:[ _-]headers?|[ _-]sent|[ _-]in[ _-]response)?[\"']?[ \t]*[:=][^\r\n]*",
    re.IGNORECASE | re.MULTILINE,
)


def redact_process_output(
    output: dict[str, str], values: tuple[str, ...] = (), *,
    truncated: bool = False, complete: bool = True,
) -> dict[str, str]:
    """Mask known secrets and recognizable authentication-header lines before publication."""
    if truncated and values:
        safe = redact_process_output(output, values, complete=False)
        return {key: text + ("[redacted]" if output[key] else "") for key, text in safe.items()}
    # shortcut: buffer multiline secrets until completion; add cross-line matching if live output needs them.
    if not complete and any("\n" in value or "\r" in value for value in values):
        return {key: "" for key in output}
    secrets = sorted({value for value in values if value}, key=len, reverse=True)
    secret_pattern = re.compile("(?=(" + "|".join(map(re.escape, secrets)) + "))") if secrets else None
    safe = {}
    for key, text in output.items():
        # Partial lines may contain a header name or value split across transport chunks.
        if not complete:
            text = text[:max(text.rfind("\n"), text.rfind("\r")) + 1]
        matches = (
            (match.start(), match.end(), "[redacted authentication header]")
            for match in _AUTH_HEADER_LINE.finditer(text)
        )
        if secret_pattern is not None:
            matches = merge(matches, (
                (match.start(1), match.end(1), "[redacted]")
                for match in secret_pattern.finditer(text)
            ))
        parts = []
        end = 0
        for start, stop, marker in matches:
            if start >= end:
                parts.extend((text[end:start], marker))
            end = max(end, stop)
        parts.append(text[end:])
        safe[key] = "".join(parts)
    return safe
