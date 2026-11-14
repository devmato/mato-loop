"""Parse portable repository rules and match their directory-relative globs."""

from __future__ import annotations

import fnmatch
import re
from dataclasses import dataclass
from functools import cache
from typing import Literal

import yaml

RuleFormat = Literal["plain", "copilot", "claude", "cursor"]
MAX_PATTERNS = 128


class _RuleLoader(yaml.SafeLoader):
    pass


def _mapping(loader: _RuleLoader, node: yaml.MappingNode) -> dict:
    result = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node)
        if not isinstance(key, str) or key in result:
            raise ValueError("Invalid rule metadata keys.")
        result[key] = loader.construct_object(value_node)
    return result


_RuleLoader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _mapping)


def _split_patterns(value: str) -> list[str]:
    # Commas inside brace expansions belong to one glob, not separate patterns.
    return re.split(r",\s*(?![^{}]*\})", value)


def _expand_pattern(pattern: str) -> list[str]:
    match = re.search(r"\{([^{}]*)\}", pattern)
    if match is None:
        if "{" in pattern or "}" in pattern:
            raise ValueError("Invalid rule glob.")
        return [pattern]
    choices = match[1].split(",")
    if len(choices) < 2:
        raise ValueError("Invalid rule brace expansion.")
    expanded = []
    for choice in choices:
        expanded.extend(_expand_pattern(pattern[:match.start()] + choice + pattern[match.end():]))
        if len(expanded) > MAX_PATTERNS:
            raise ValueError("Too many rule globs.")
    return expanded


def _patterns(value: object, *, basename_recursive: bool = False) -> tuple[str, ...]:
    values = _split_patterns(value) if isinstance(value, str) else value
    if not isinstance(values, list) or not values or len(values) > MAX_PATTERNS:
        raise ValueError("Invalid rule globs.")
    result = []
    for item in values:
        if not isinstance(item, str) or not item.strip() or len(item) > 1024:
            raise ValueError("Invalid rule glob.")
        for pattern in _expand_pattern(item.strip()):
            if (
                len(pattern) > 1024 or "\\" in pattern or ":" in pattern
                or pattern.startswith("!") or any(part in {".", ".."} for part in pattern.split("/"))
                or re.search(r"[?*+@!]\(", pattern)
            ):
                raise ValueError("Unsupported rule glob.")
            if basename_recursive and "/" not in pattern:
                pattern = "**/" + pattern
            result.append(pattern.removeprefix("/"))
            if len(result) > MAX_PATTERNS:
                raise ValueError("Too many rule globs.")
    return tuple(dict.fromkeys(result))


def matches_rule_path(path: str, pattern: str) -> bool:
    """Match segments separately so '*' never crosses directory boundaries."""
    path_parts, pattern_parts = path.split("/"), pattern.split("/")

    @cache
    def match(path_index: int, pattern_index: int) -> bool:
        if pattern_index == len(pattern_parts):
            return path_index == len(path_parts)
        if pattern_parts[pattern_index] == "**":
            return match(path_index, pattern_index + 1) or (
                path_index < len(path_parts) and match(path_index + 1, pattern_index)
            )
        return (
            path_index < len(path_parts)
            and fnmatch.fnmatchcase(path_parts[path_index], pattern_parts[pattern_index])
            and match(path_index + 1, pattern_index + 1)
        )

    return match(0, 0)


@dataclass(frozen=True)
class RepositoryRule:
    content: str
    patterns: tuple[str, ...] = ()
    activation: Literal["automatic", "relevant", "manual"] = "automatic"
    description: str = ""


def parse_repository_rule(content: str, rule_format: RuleFormat) -> RepositoryRule:
    if rule_format == "plain":
        return RepositoryRule(content)
    text = content.removeprefix("\ufeff")
    metadata = {}
    body = text
    if text.startswith("---"):
        match = re.fullmatch(r"---\r?\n(.*?)^---(?:\r?\n|$)(.*)", text, re.DOTALL | re.MULTILINE)
        if match is None:
            raise ValueError("Invalid rule frontmatter.")
        try:
            header = match[1] or ""
            if any(isinstance(token, (yaml.tokens.AliasToken, yaml.tokens.AnchorToken)) for token in yaml.scan(header)):
                raise ValueError("Rule metadata aliases are not supported.")
            metadata = yaml.load(header, Loader=_RuleLoader)
            if metadata is None:
                metadata = {}
        except (yaml.YAMLError, RecursionError) as exc:
            raise ValueError("Invalid rule frontmatter.") from exc
        if not isinstance(metadata, dict):
            raise ValueError("Invalid rule frontmatter.")
        body = match[2]
    if rule_format == "copilot":
        return RepositoryRule(body, _patterns(metadata.get("applyTo")))
    if rule_format == "claude":
        return RepositoryRule(
            body, _patterns(metadata["paths"], basename_recursive=True) if "paths" in metadata else (),
        )
    always = metadata.get("alwaysApply", False)
    if not isinstance(always, bool):
        raise ValueError("Invalid Cursor activation.")
    description = metadata.get("description", "")
    if not isinstance(description, str) or len(description) > 2000:
        raise ValueError("Invalid Cursor description.")
    if always:
        return RepositoryRule(body)
    patterns = _patterns(metadata["globs"]) if metadata.get("globs") else ()
    if patterns:
        return RepositoryRule(body, patterns)
    return RepositoryRule(body, activation="relevant" if description else "manual", description=description)
