"""Exercise project-first recall and result paging through real development agent runs."""

import asyncio
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

from sqlalchemy import delete, select

from mato_loop_backend.agent.projection import _tool_calls_by_message_id
from mato_loop_backend.db.session import AsyncSessionFactory, engine
from mato_loop_backend.files.storage import SessionStorage
from mato_loop_backend.memory.embeddings import ACTIVE_EMBEDDING_PROFILE, embedding_client
from mato_loop_backend.memory.episode_indexing import EpisodeIndexService
from mato_loop_backend.models import ChatSession, Message, MessageSearchChunk, Project
from mato_loop_backend.session_cache_eval import SessionCacheEvaluator, parser


async def run(args):
    evaluator = SessionCacheEvaluator(args)
    projects, rows = [], []
    nonce = uuid4().hex[:8]
    external_code = f"EXTERNAL-{nonce}-{uuid4().hex[:8]}"
    try:
        await evaluator.prepare()
        async with AsyncSessionFactory() as db:
            projects = [Project(user_id=evaluator.user_id, name=name) for name in ("WebMap", "3D-Druck")]
            db.add_all(projects)
            await db.flush()
            sources = []
            for project, content in zip(projects, (
                "WebMap kalibriert gescannte Karten mit Passpunkten und Koordinaten. "
                "Georeferenzierung und die verbleibenden Positionsfehler sind entscheidend.",
                "Für die Druckerkalibrierung drucken wir ein Califlower und messen mit dem Messschieber. "
                f"Die externe Referenzkennung lautet {external_code}.",
            ), strict=True):
                source = ChatSession(user_id=evaluator.user_id, project_id=project.id,
                                     title="Kalibrierung", memory_mode="full", trust_level="public")
                db.add(source)
                await db.flush()
                evaluator.session_ids.append(source.id)
                message = Message(session_id=source.id, role="user", content=content, trust_level="public")
                db.add(message)
                await db.flush()
                await EpisodeIndexService.enqueue(db, message)
                sources.append(source)
            chunks = list((await db.scalars(select(MessageSearchChunk).join(
                Message, Message.id == MessageSearchChunk.message_id,
            ).where(Message.session_id.in_([source.id for source in sources])))).all())
            vectors = await embedding_client.embed_documents([chunk.content for chunk in chunks])
            for chunk, vector in zip(chunks, vectors, strict=True):
                chunk.vector, chunk.status = vector, "ready"
                chunk.profile_fingerprint = ACTIVE_EMBEDDING_PROFILE.fingerprint
            await db.commit()

        async def new_session(project_id):
            async with AsyncSessionFactory() as db:
                session = ChatSession(user_id=evaluator.user_id, project_id=project_id,
                                      model_config_id=evaluator.model_id, memory_mode="full",
                                      trust_level="public", web_search_policy="disabled")
                db.add(session)
                await db.commit()
                evaluator.session_ids.append(session.id)
                SessionStorage().provision(session.id, project_id=project_id, user_id=evaluator.user_id)
                return session.id

        async def ask(name, session_id, prompt, check):
            run_id = await evaluator._send(session_id, prompt)
            _run, answer = await evaluator._wait_run(session_id, run_id)
            async with AsyncSessionFactory() as db:
                messages = list((await db.scalars(select(Message).where(
                    Message.session_id == session_id,
                ).order_by(Message.created_at, Message.id))).all())
            user = next(message for message in reversed(messages) if message.role == "user")
            current_messages = messages[messages.index(user) + 1:]
            calls = [call for message in current_messages for call in message.tool_calls or []]
            result_calls = _tool_calls_by_message_id(current_messages)
            results = [{"call": result_calls[message.id], "content": message.content}
                       for message in current_messages if message.id in result_calls]
            memory = (user.provider_data or {}).get("_matoloop", {}).get("memoryContext", {}).get("content", "")
            row = {"case": name, "runId": str(run_id), "answer": answer.content,
                   "calls": calls, "toolResults": results, "memoryContext": memory,
                   "usage": (answer.provider_data or {}).get("usage")}
            row["passed"] = check(row)
            rows.append(row)
            print(f"PASS={row['passed']} case={name} calls={len(calls)}", flush=True)

        current = await new_session(projects[0].id)
        await ask("project_first", current,
                  "wäre ein ki gestützter kalibrierungsworkflow denkbar? was wären sinnvolle ansätze "
                  "und wären gute ergebnisse zu erwarten? Antworte kurz.",
                  lambda row: "Califlower" not in row["memoryContext"] and external_code not in row["memoryContext"]
                  and "califlower" not in row["answer"].lower() and "3d-druck" not in row["answer"].lower()
                  and any(word in row["answer"].lower() for word in ("karte", "passpunkt", "georeferenz", "koordinat")))

        def external_search_passed(row):
            return external_code in row["answer"] and any(
                result["call"]["name"] == "context_search" and result["call"].get("arguments", {}).get("all_projects")
                and any(item.get("sessionId") == str(sources[1].id) and external_code in item.get("passage", "")
                        for item in json.loads(result["content"]).get("sessions", []))
                for result in row["toolResults"]
            )

        assert not external_search_passed({
            "answer": external_code,
            "toolResults": [{"call": {"name": "context_search", "arguments": {"all_projects": True}},
                             "content": '{"sessions": []}'}],
        })
        await ask("targeted_external_search", current,
                  f"Wir wollen eine Methode aus dem anderen Projekt 3D-Druck vergleichen. Suche dort gezielt "
                  f"nach der Referenzkennung EXTERNAL-{nonce}. Welche vollständige Referenzkennung wurde "
                  "festgehalten? Nutze context_search und antworte nur mit der belegten Kennung.",
                  external_search_passed)

        paging = await new_session(projects[0].id)
        async with AsyncSessionFactory() as db:
            now = datetime.now(UTC) - timedelta(minutes=1)
            db.add(Message(session_id=paging, role="user", content="Lies das Protokoll.", created_at=now))
            db.add(Message(session_id=paging, role="assistant", content="", created_at=now + timedelta(seconds=1),
                           tool_calls=[{"id": "eval-source", "name": "session_read",
                                        "arguments": {"session_id": str(sources[0].id)}}]))
            db.add(Message(session_id=paging, role="tool", tool_call_id="eval-source",
                           created_at=now + timedelta(seconds=2), content=json.dumps({
                               "session": {"id": str(sources[0].id)}, "messages": [
                                   {"role": "user", "content": "Die verifizierte Saatzeit ist 2026-10-05T15:35:00+02:00."},
                                   {"role": "tool", "toolName": "webfetch", "content": "Fülltext " * 8000},
                               ],
                           }), provider_data={"_matoloop": {"contextOutputOmitted": True}}))
            await db.commit()
        await ask("read_back_session_page", paging,
                  "Lies das ausgelagerte Tool-Ergebnis aus diesem Verlauf mit session_tool_result_read. "
                  "Welche verifizierte Saatzeit steht darin? Antworte exakt mit dem belegten ISO-Zeitstempel.",
                  lambda row: "2026-10-05T15:35:00+02:00" in row["answer"]
                  and any(call["name"] == "session_tool_result_read" for call in row["calls"]))
    finally:
        failures = await evaluator.cleanup()
        async with AsyncSessionFactory() as db:
            if projects:
                await db.execute(delete(Project).where(Project.id.in_([project.id for project in projects])))
                await db.commit()
        await embedding_client.close()
        await engine.dispose()
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps({"model": evaluator.model_metadata, "records": rows,
                                          "cleanupFailures": failures}, ensure_ascii=False, indent=2) + "\n")
    assert len(rows) == 3 and all(row["passed"] for row in rows) and not failures


if __name__ == "__main__":
    argument_parser = parser()
    argument_parser.description = __doc__
    argument_parser.set_defaults(output="/workspace/data/project-context-eval.json")
    arguments = argument_parser.parse_args()
    arguments.output = Path(arguments.output)
    asyncio.run(run(arguments))
