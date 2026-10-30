from __future__ import annotations

import json
import re
from typing import Any

from mato_loop_backend.memory.episode_retrieval import (
    EpisodeReference,
    ProjectSessionIndexItem,
)
from mato_loop_backend.memory.service import memory_reference_timestamps
from mato_loop_backend.models import MemoryRecord, Message
from mato_loop_backend.providers.output_budget import generation_reserve, raw_input_budget
from mato_loop_backend.settings import AgentRuntimeSettings, MemorySettings
from mato_loop_backend.settings.models import ModelConfig

AUTOMATIC_EPISODE_REFERENCE_MAX_CHARACTERS = 320
AUTOMATIC_MEMORY_MAX_CHARACTERS = 500
AUTOMATIC_RECENT_MESSAGE_MAX_CHARACTERS = 240
AUTOMATIC_CONTEXT_BUDGET_RATIO = 0.1
MEMORY_OVERVIEW_MAX_CHARACTERS = 12_000
PROJECT_SESSION_INDEX_MAX_CHARACTERS = 2_000
PROJECT_SESSION_INDEX_TITLE_MAX_CHARACTERS = 32


def _memory_rag_query(records: list[Message]) -> str | None:
    user_messages = [item.content.strip() for item in records if item.role == "user"]
    if not user_messages or not user_messages[-1]:
        return None
    user_messages = [item for item in user_messages if item]
    current = user_messages[-1][:2_000]
    return (
        (user_messages[-2][:1_000] + "\n" + current)[:2_000]
        if len(current) <= 120 and len(user_messages) > 1
        else current
    )


def _automatic_context_suppressed(records: list[Message]) -> bool:
    current = next(
        (item.content.casefold() for item in reversed(records) if item.role == "user"),
        "",
    )
    normalized = re.sub(r"\s+", " ", current).strip()
    return bool(
        re.search(
            r"\b(?:translate|übersetze|uebersetze|rewrite|umschreiben|"
            r"explain the phrase)\b|\bwhat does .{0,40}\bmean\b|"
            r"\b(?:summari[sz]e|proofread|edit|rephrase) (?:this|the following) "
            r"(?:text|content|passage|message|document)\b|"
            r"\b(?:fasse|korrigiere|bearbeite|formuliere) "
            r"(?:diesen|diese|das folgende|den folgenden) "
            r"(?:text|inhalt|abschnitt|nachricht|dokument)\b",
            normalized,
        )
        or re.search(
            r"\b(?:do not|don't|dont|ignore|avoid|without|"
            r"nicht|ohne)\b.{0,80}\b"
            r"(?:memory|memories|context|previous|prior|earlier|"
            r"erinnerung(?:en)?|gedächtnis|gedaechtnis|kontext|"
            r"vorherigen|früheren|frueheren)\b",
            normalized,
        )
        or re.search(
            r"\b(?:use|using|nutze|verwende)\b.{0,30}\b(?:no|keine?)\b.{0,30}\b"
            r"(?:memory|memories|context|previous|prior|earlier|"
            r"erinnerung(?:en)?|gedächtnis|gedaechtnis|kontext|"
            r"vorherigen|früheren|frueheren)\b",
            normalized,
        )
        or re.search(
            r"\b(?:memory|memories|context|previous|prior|earlier|"
            r"erinnerung(?:en)?|gedächtnis|gedaechtnis|kontext|"
            r"vorherige|frühere|fruehere)\b.{0,80}\b"
            r"(?:irrelevant|do not|don't|dont|ignore|nicht verwenden|nicht nutzen)\b",
            normalized,
        )
    )


def _episode_reference_request_text(records: list[Message]) -> str:
    current = next(
        (
            item.content.casefold()
            for item in reversed(records)
            if item.role == "user"
        ),
        "",
    )
    normalized = re.sub(r"\s+", " ", current).strip()
    if _automatic_context_suppressed(records):
        return ""
    return normalized


def _recent_episode_overview_requested(records: list[Message]) -> bool:
    normalized = _episode_reference_request_text(records)
    return bool(
        re.search(
            r"\b(?:was haben wir|worüber haben wir|woran haben wir) (?:zuletzt|in der letzten session|im letzten chat)\b"
            r"|\b(?:letzte|vorherige) session\b"
            r"|\bwhat did we (?:last|previously) discuss\b"
            r"|\b(?:last|previous) (?:session|chat|conversation)\b",
            normalized,
        )
    )


def _session_bootstrap_requested(records: list[Message]) -> bool:
    return bool(_episode_reference_request_text(records)) and sum(
        item.role == "user" for item in records
    ) == 1 and not any(item.role == "assistant" for item in records)


def _memory_overview_requested(records: list[Message]) -> bool:
    current_index = next(
        (index for index in range(len(records) - 1, -1, -1) if records[index].role == "user"),
        None,
    )
    if current_index is None:
        return False
    current = re.sub(r"\s+", " ", records[current_index].content.casefold()).strip()
    direct_patterns = (
        r"\bwas (?:hast|weißt|weisst|kennst) du .*\b(?:erinner|gedächtnis|über mich|ueber mich)",
        r"\bwas (?:weißt|weisst|kennst) du (?:alles )?(?:über|ueber|von) mir\b",
        r"\bwelche .*\b(?:erinnerungen|informationen) .*\b(?:gespeichert|gemerkt)\b",
        r"\bwhat (?:else )?do you (?:remember|know) (?:about me)?\b",
        r"\bwhat (?:memories|information) do you have (?:about me)?\b",
    )
    if any(re.search(pattern, current) for pattern in direct_patterns):
        return True
    follow_up = bool(
        re.search(
            r"\b(?:was|welche|und)?\s*(?:noch|sonst|mehr)\b|\b(?:what|anything) else\b|\bmore\b",
            current,
        )
    )
    if not follow_up:
        return False
    previous_user_index = next(
        (
            index
            for index in range(current_index - 1, -1, -1)
            if records[index].role == "user"
        ),
        -1,
    )
    return any(
        (call.get("name") == "memory_browse" or (
            call.get("name") == "memory_retrieve"
            and (call.get("arguments") or {}).get("mode") == "browse"
        ))
        for item in records[previous_user_index + 1 : current_index]
        if item.role == "assistant"
        for call in (item.tool_calls or [])
        if isinstance(call, dict)
    ) or any(
        isinstance(item.provider_data, dict)
        and isinstance(item.provider_data.get("_matoloop"), dict)
        and isinstance(item.provider_data["_matoloop"].get("memoryOverview"), dict)
        for item in records[previous_user_index + 1 : current_index]
        if item.role == "assistant"
    )


def _memory_overview_continuation(records: list[Message]) -> dict[str, Any] | None:
    current_index = next(
        (index for index in range(len(records) - 1, -1, -1) if records[index].role == "user"),
        None,
    )
    if current_index is None:
        return None
    current = re.sub(r"\s+", " ", records[current_index].content.casefold()).strip()
    if not re.search(
        r"\b(?:was|welche|und)?\s*(?:noch|sonst|mehr)\b|\b(?:what|anything) else\b|\bmore\b",
        current,
    ):
        return None
    previous_user_index = next(
        (
            index
            for index in range(current_index - 1, -1, -1)
            if records[index].role == "user"
        ),
        -1,
    )
    for item in reversed(records[previous_user_index + 1 : current_index]):
        runtime_state = (
            item.provider_data.get("_matoloop")
            if item.role == "assistant" and isinstance(item.provider_data, dict)
            else None
        )
        overview = (
            runtime_state.get("memoryOverview")
            if isinstance(runtime_state, dict)
            else None
        )
        if isinstance(overview, dict):
            return overview
    return None


def _automatic_context_character_budget(
    model: ModelConfig,
    runtime_settings: AgentRuntimeSettings,
    memory_settings: MemorySettings,
) -> int:
    input_budget = raw_input_budget(max(
        0,
        model.context_window_tokens
        - generation_reserve(model)
        - runtime_settings.safety_reserve_tokens,
    ))
    token_budget = min(
        memory_settings.automatic_context_max_tokens,
        int(input_budget * AUTOMATIC_CONTEXT_BUDGET_RATIO),
    )
    return max(0, int(token_budget * model.characters_per_token))


def _serialized_automatic_size(payload: dict[str, Any]) -> int:
    return len(json.dumps(payload, ensure_ascii=False))


def _append_bounded(
    payload: dict[str, list[dict[str, Any]]],
    key: str,
    item: dict[str, Any],
    text_fields: list[tuple[dict[str, Any], str]],
    *,
    max_characters: int,
) -> bool:
    payload[key].append(item)
    while (overflow := _serialized_automatic_size(payload) - max_characters) > 0:
        field = next(
            ((container, name) for container, name in reversed(text_fields) if container[name]),
            None,
        )
        if field is None:
            payload[key].pop()
            return False
        container, name = field
        container[name] = container[name][: max(0, len(container[name]) - overflow)]
        item["truncated"] = True
    if not any(container[name] for container, name in text_fields):
        payload[key].pop()
        return False
    return True


def _automatic_memory_item(item: MemoryRecord) -> tuple[dict[str, Any], list[tuple[dict[str, Any], str]]]:
    content = item.content[:AUTOMATIC_MEMORY_MAX_CHARACTERS]
    payload = {
        "source": "retrieved",
        "id": str(item.id),
        "revision": item.revision,
        "category": item.category,
        "content": content,
        "truncated": len(content) < len(item.content),
        **memory_reference_timestamps(item),
    }
    return payload, [(payload, "content")]


def _relevant_episode_item(
    reference: EpisodeReference,
) -> tuple[dict[str, Any], list[tuple[dict[str, Any], str]]]:
    preview = reference.anchor_content[:AUTOMATIC_EPISODE_REFERENCE_MAX_CHARACTERS]
    anchor = {
        "id": str(reference.anchor_message_id),
        "role": reference.anchor_role,
        "createdAt": reference.anchor_created_at.isoformat(),
        "preview": preview,
    }
    payload = {
        "source": "relevant",
        "sessionId": str(reference.session_id),
        "projectId": str(reference.project_id) if reference.project_id else None,
        "title": reference.session_title,
        "sessionReadArguments": {
            "session_id": str(reference.session_id),
            "around_message_id": str(reference.anchor_message_id),
        },
        "anchorMessage": anchor,
        "truncated": len(preview) < len(reference.anchor_content),
    }
    text_fields = [(anchor, "preview")]
    return payload, text_fields


def _recent_episode_item(
    reference: EpisodeReference,
) -> tuple[dict[str, Any], list[tuple[dict[str, Any], str]]]:
    excerpt = list(reference.excerpt[-2:])
    messages = [
        {
            "id": str(message.id),
            "role": message.role,
            "createdAt": message.created_at.isoformat(),
            "content": message.content[:AUTOMATIC_RECENT_MESSAGE_MAX_CHARACTERS],
        }
        for message in excerpt
    ]
    if not messages and reference.anchor_content:
        messages = [
            {
                "id": str(reference.anchor_message_id),
                "role": reference.anchor_role,
                "createdAt": reference.anchor_created_at.isoformat(),
                "content": reference.anchor_content[:AUTOMATIC_RECENT_MESSAGE_MAX_CHARACTERS],
            }
        ]
    payload = {
        "source": "recent",
        "sessionId": str(reference.session_id),
        "projectId": str(reference.project_id) if reference.project_id else None,
        "title": reference.session_title,
        "sessionReadArguments": {
            "session_id": str(reference.session_id),
            "around_message_id": str(reference.anchor_message_id),
        },
        "messages": messages,
        "truncated": any(
            len(message["content"]) < len(source.content)
            for message, source in zip(messages, excerpt, strict=False)
        )
        or (not excerpt and len(reference.anchor_content) > AUTOMATIC_RECENT_MESSAGE_MAX_CHARACTERS),
    }
    return payload, [(message, "content") for message in messages]


def _bounded_automatic_payload(
    memories: list[MemoryRecord],
    relevant_episodes: list[EpisodeReference],
    recent_episodes: list[EpisodeReference],
    *,
    max_characters: int,
    project_session: bool = False,
    project_sessions: list[ProjectSessionIndexItem] | None = None,
    has_more_project_sessions: bool = False,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, Any] | None]:
    payload: dict[str, Any] = {
        "durableMemories": [],
        "priorSessionEpisodes": [],
    }
    if project_sessions is not None:
        payload["projectSessionIndex"] = {
            "sessions": [],
            "hasMore": has_more_project_sessions,
        }
    if max_characters < _serialized_automatic_size(payload):
        return [], [], None

    if project_sessions is not None:
        project_index = payload["projectSessionIndex"]
        index_limit = min(
            PROJECT_SESSION_INDEX_MAX_CHARACTERS,
            max_characters // 2,
        )
        for session in project_sessions:
            item = {
                "title": session.session_title[
                    :PROJECT_SESSION_INDEX_TITLE_MAX_CHARACTERS
                ],
                "sessionReadArguments": {
                    "session_id": str(session.session_id),
                    "around_message_id": str(session.latest_message_id),
                },
            }
            project_index["sessions"].append(item)
            if (
                overflow := len(json.dumps(project_index, ensure_ascii=False)) - index_limit
            ) > 0:
                item["title"] = item["title"][: max(0, len(item["title"]) - overflow)]
            if (
                not item["title"]
                or len(json.dumps(project_index, ensure_ascii=False)) > index_limit
                or _serialized_automatic_size(payload) > max_characters
            ):
                project_index["sessions"].pop()
                project_index["hasMore"] = True
                break

    seen_memory_ids: set[object] = set()
    unique_memories = []
    for item in memories:
        if item.id not in seen_memory_ids:
            seen_memory_ids.add(item.id)
            unique_memories.append(item)

    preferred_episodes = relevant_episodes if project_session else recent_episodes
    secondary_episodes = recent_episodes if project_session else relevant_episodes
    seen_session_ids = {item.session_id for item in preferred_episodes}
    unique_secondary = []
    for item in secondary_episodes:
        if item.session_id not in seen_session_ids:
            seen_session_ids.add(item.session_id)
            unique_secondary.append(item)
    unique_preferred = list(
        {item.session_id: item for item in preferred_episodes}.values()
    )

    if project_session:
        groups = [
            ("priorSessionEpisodes", unique_preferred, _relevant_episode_item),
            ("priorSessionEpisodes", unique_secondary, _recent_episode_item),
            ("durableMemories", unique_memories, _automatic_memory_item),
        ]
        for key, items, build in groups:
            for source in items:
                item, text_fields = build(source)
                _append_bounded(
                    payload,
                    key,
                    item,
                    text_fields,
                    max_characters=max_characters,
                )
        return (
            payload["durableMemories"],
            payload["priorSessionEpisodes"],
            payload.get("projectSessionIndex"),
        )

    groups = [
        ("durableMemories", unique_memories, _automatic_memory_item),
        ("priorSessionEpisodes", unique_preferred, _recent_episode_item),
        ("priorSessionEpisodes", unique_secondary, _relevant_episode_item),
    ]
    for index in range(max((len(items) for _, items, _ in groups), default=0)):
        for key, items, build in groups:
            if index >= len(items):
                continue
            item, text_fields = build(items[index])
            _append_bounded(
                payload,
                key,
                item,
                text_fields,
                max_characters=max_characters,
            )
    return (
        payload["durableMemories"],
        payload["priorSessionEpisodes"],
        payload.get("projectSessionIndex"),
    )


def _cross_session_context_text(
    memory_references: list[tuple[str, MemoryRecord]],
    episode_payload: list[dict[str, Any]],
    automatic_memory_payload: list[dict[str, Any]] | None = None,
    project_session_index: dict[str, Any] | None = None,
) -> str | None:
    if (
        not memory_references
        and not automatic_memory_payload
        and not episode_payload
        and project_session_index is None
    ):
        return None
    payload = {
        "durableMemories": [
            *_durable_memory_payload(memory_references),
            *(automatic_memory_payload or []),
        ],
        "priorSessionEpisodes": episode_payload,
    }
    if project_session_index is not None:
        payload["projectSessionIndex"] = project_session_index
    return json.dumps(payload, ensure_ascii=False)


def _durable_memory_payload(
    memory_references: list[tuple[str, MemoryRecord]],
) -> list[dict[str, Any]]:
    return [
        {
            "source": source,
            "id": str(item.id),
            "revision": item.revision,
            "category": item.category,
            "audience": item.audience,
            "content": item.content,
            **memory_reference_timestamps(item),
        }
        for source, item in memory_references
    ]


def _bounded_memory_overview(
    items: list[MemoryRecord],
) -> tuple[list[dict[str, Any]], MemoryRecord | None, bool]:
    payload = []
    used_characters = 0
    content_truncated = False
    for item in items:
        separator = 2 if payload else 0
        available = MEMORY_OVERVIEW_MAX_CHARACTERS - used_characters - separator
        if available <= 0:
            break
        content = item.content
        if len(content) > available:
            if payload:
                break
            suffix = "... [truncated]"
            content = content[: max(0, available - len(suffix))] + suffix
            content_truncated = True
        payload.append(
            {
                "source": "overview",
                "id": str(item.id),
                "revision": item.revision,
                "category": item.category,
                "pinned": item.pinned,
                "audience": item.audience,
                "content": content,
                **memory_reference_timestamps(item),
                "sourceSession": (
                    {"id": str(item.source_session.id)}
                    if item.source_session
                    else None
                ),
            }
        )
        used_characters += separator + len(content)
        if content_truncated:
            break
    omitted_items = len(payload) < len(items)
    return payload, (items[len(payload) - 1] if payload else None), omitted_items
