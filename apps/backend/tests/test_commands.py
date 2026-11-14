from unittest.mock import AsyncMock
from uuid import uuid4, uuid5

import pytest

from mato_loop_backend.agent.projection import _summary_source, message_dict
from mato_loop_backend.api.routes.projects import ProjectRequest
from mato_loop_backend.messaging.telegram import telegram_commands
from mato_loop_backend.models import (
    ChatSession,
    ExecutionAgent,
    Message,
    Project,
    UserAccount,
)
from mato_loop_backend.sessions.coding import (
    RepositoryResult,
    WorktreeRemovedResult,
    WorktreeResult,
    inspect_repository,
)
from mato_loop_backend.sessions.commands import (
    command_catalog,
    parse_command,
    prepare_command,
    registered_commands,
)
from mato_loop_backend.sessions.git_commands import (
    COMMIT_INSTRUCTIONS,
    COMMIT_RULES,
    OUTPUT_FORMATS,
    OUTPUT_RULES,
    REVIEW_INSTRUCTIONS,
    validate_git_context,
)
from mato_loop_backend.sessions.serialization import message_payload
from mato_loop_backend.sessions.service import SessionServiceError
from mato_loop_backend.settings import AiSettings, TrustLevel
from mato_loop_backend.tools.sessions import _message_item


def test_active_file_contract_accepts_json_computer_uuid():
    from mato_loop_backend.sessions.contracts import ActiveFileRequest

    agent_id = uuid4()
    payload = ActiveFileRequest.model_validate({
        "root": "workspace", "executionAgentId": str(agent_id), "path": "/file.txt",
        "sha256": "a" * 64, "dirty": True,
    })
    assert payload.execution_agent_id == agent_id


async def test_browser_and_telegram_share_portable_coding_commands(monkeypatch):
    assert set(OUTPUT_FORMATS) == {item.name for item in registered_commands() if item.name.startswith("git-")}
    assert {command.command for command in telegram_commands() if command.command.startswith("git_")} == {
        item.name.replace("-", "_") for item in registered_commands() if item.name.startswith("git-")
    }
    assert (parse_command("/git-review").name, parse_command("/git_review@my_bot").name) == ("git-review", "git-review")
    assert parse_command("Explain /git-merge") is None
    monkeypatch.setattr("mato_loop_backend.sessions.git_commands.inspect_repository", AsyncMock(side_effect=AssertionError("Catalog must not probe Git")))
    catalog = await command_catalog(AsyncMock(), ChatSession(user_id=uuid4()))
    assert {item["name"]: item["disabledReason"] for item in catalog if item["source"] == "git"} == dict.fromkeys(OUTPUT_FORMATS)


def test_git_merge_and_pr_follow_explicit_commit_message_rules():
    assert all(COMMIT_RULES in parse_command(f"/{name}").instructions for name in ("git-commit", "git-finish", "git-merge", "git-pr"))


def test_pr_preserves_the_branch_binding_for_retries_and_publishes_detached_work_explicitly():
    assert all(text in parse_command("/git-pr").instructions for text in (
        "repository publication and branch conventions", "existing branch/upstream mappings",
        "ask if its target or name is unclear", "Do not automatically publish technical worktree branch names",
        "prior Finish or local integration is not required", "normal non-force push",
        "Keep the current branch or detached HEAD; do not create or switch local branches",
        "use it as the rebase target", "<reviewed-commit>:refs/heads/<publication-branch>",
        "pass the publication branch explicitly as the PR source",
    ))


def test_push_resolves_integrated_detached_and_named_work_without_requiring_a_pr():
    assert all(text in parse_command("/git-push").instructions for text in (
        "push that target branch's current commit", "not the worktree HEAD",
        "Use a non-technical current branch name", "For detached HEAD or a technical worktree branch, ask",
        "unless the user already selected one", "Explicit authorization to publish a technical branch name is sufficient",
        "Prior local integration or a pull request is not required", "normal non-force push",
    ))


@pytest.mark.parametrize("name", ["git-review", "git-finish", "git-merge", "git-pr"])
def test_git_reviews_share_risk_and_simplicity_criteria_without_optional_blockers(name):
    instructions = parse_command(f"/{name}").instructions
    assert instructions.count(REVIEW_INSTRUCTIONS) == 1
    assert all(text in instructions for text in (
        REVIEW_INSTRUCTIONS, "concurrency and scaling at the expected load", "missing tests for risky logic",
        "significant performance problems", "smallest complete fix", "Optional simplifications do not block integration",
    ))
    assert "unresolved findings" not in instructions and "Stop on concrete findings" not in instructions


@pytest.mark.parametrize("name", ["git-merge", "git-finish"])
def test_git_merge_prepares_the_workspace_before_requiring_a_clean_original(name):
    instructions = parse_command(f"/{name}").instructions
    assert instructions.index("original checkout may remain dirty") < instructions.index("Use the current local target")
    assert instructions.index("Only before updating the original checkout") < instructions.index("both clean working trees")
    assert "fast-forward-only merge" in instructions


@pytest.mark.parametrize("name", ["git-finish", "git-merge", "git-pr"])
def test_git_integration_rebases_only_own_unpublished_commits(name):
    from mato_loop_backend.sessions.git_commands import REBASE_INSTRUCTIONS

    assert REBASE_INSTRUCTIONS in parse_command(f"/{name}").instructions
    if name == "git-finish":
        assert "no separate worktree, rebase or integration is needed" in parse_command(f"/{name}").instructions


def test_git_results_include_full_stored_commit_messages_even_when_integration_is_blocked():
    assert all(text in OUTPUT_RULES for text in (
        "full stored message from Git", "subject, body and trailers verbatim", "even if later integration is blocked",
    ))


@pytest.mark.parametrize("branch", ["--upload-pack=bad", "main..other", "main:other", "bad\nbranch"])
def test_project_rejects_invalid_git_branch(branch):
    with pytest.raises(ValueError):
        ProjectRequest(name="Project", gitTargetBranch=branch)


async def test_non_git_provider_supports_arguments_without_a_computer(monkeypatch):
    from mato_loop_backend.sessions import commands
    from mato_loop_backend.sessions.command_contracts import Command, CommandProvider

    command = Command("example-task", "Beispiel", "Allgemeiner Auftrag", "Perform the requested task.")
    prepare = AsyncMock(return_value={"instructions": command.instructions})
    monkeypatch.setattr(commands, "_providers", dict(commands._providers))
    commands.register_command_provider(CommandProvider("example", (command,), AsyncMock(return_value={command.name: None}), prepare))
    session = ChatSession(user_id=uuid4())
    parsed = commands.parse_command("/example-task some arguments")
    catalog = await commands.command_catalog(AsyncMock(), session)
    await commands.prepare_command(AsyncMock(), session, parsed, "/example-task some arguments")
    assert (parsed, [item["source"] for item in catalog if item["name"] == command.name], prepare.await_args.args[-1]) == (command, ["example"], "/example-task some arguments")


def test_command_registration_rejects_underscore_names():
    from mato_loop_backend.sessions.command_contracts import Command, CommandProvider
    from mato_loop_backend.sessions.commands import register_command_provider

    command = Command("example_task", "Beispiel", "Allgemeiner Auftrag", "Perform the requested task.")
    with pytest.raises(ValueError):
        register_command_provider(CommandProvider("example", (command,), AsyncMock(), AsyncMock()))


async def test_fork_is_a_host_action_not_an_agent_prompt():
    with pytest.raises(SessionServiceError, match="validation.invalid"):
        await prepare_command(AsyncMock(), ChatSession(id=uuid4()), parse_command("/fork"))


async def test_catalog_rejects_an_unowned_session_before_provider_discovery(monkeypatch):
    from mato_loop_backend.api.errors import ApiError
    from mato_loop_backend.api.routes import commands as routes

    monkeypatch.setattr(routes, "get_owned_session", AsyncMock(return_value=None))
    discover = AsyncMock()
    monkeypatch.setattr(routes, "command_catalog", discover)
    with pytest.raises(ApiError) as error:
        await routes.catalog(routes.CommandCatalogRequest(session_id=uuid4()), UserAccount(id=uuid4()), AsyncMock())
    assert (error.value.code, discover.await_count) == ("session.notFound", 0)


async def test_command_binds_worktree_target_and_displays_its_prompt_without_runtime_instructions(monkeypatch):
    agent_id = uuid4()
    session = ChatSession(id=uuid4(), user_id=uuid4(), preferred_execution_agent_id=agent_id, current_directory="/worktree", workspace_mode="worktree", coding_workspace={
        "mode": "worktree", "executionAgentId": str(agent_id), "repository": "/project", "path": "/worktree", "branch": "session",
        "remote": None, "targetBranch": "develop",
    })
    monkeypatch.setattr("mato_loop_backend.sessions.git_commands.inspect_repository", AsyncMock(return_value=RepositoryResult(
        repository="/worktree", remotes=["origin"], defaultRemote="origin", defaultBranch="main", branch="session",
    )))
    db = AsyncMock()
    db.scalar.return_value = None
    definition = parse_command("/git-review")
    command = await prepare_command(db, session, definition, "/git-review only parser changes")
    message = Message(id=uuid4(), role="user", content="/git-review only parser changes", provider_data={"_matoloop": {"codingCommand": command}})
    result = message_dict(message)
    assert '"targetBranch": "develop"' in result["content"]
    assert command["context"]["remote"] is None
    assert message.content == "/git-review only parser changes"
    assert message_payload(message)["commandPrompt"] == (
        definition.instructions + "\n\n" + OUTPUT_RULES + OUTPUT_FORMATS[definition.name] + "\n\nonly parser changes"
    )


@pytest.mark.parametrize("name", ["git-merge", "git-pr"])
@pytest.mark.parametrize("legacy_commit_flag", [False, True])
async def test_worktree_command_uses_the_current_branch_and_commits_task_changes_by_default(monkeypatch, name, legacy_commit_flag):
    agent_id = uuid4()
    remote = "origin" if name == "git-pr" else None
    session = ChatSession(id=uuid4(), user_id=uuid4(), preferred_execution_agent_id=agent_id,
        current_directory="/worktree/subdirectory", workspace_mode="local", coding_workspace={
            "mode": "worktree", "executionAgentId": str(agent_id), "repository": "/project", "path": "/worktree",
            "branch": "old-name", "remote": remote, "targetBranch": "develop",
        })
    monkeypatch.setattr("mato_loop_backend.sessions.git_commands.inspect_repository", AsyncMock(return_value=RepositoryResult(
        repository="/worktree", remotes=[remote] if remote else [], defaultRemote=remote, defaultBranch="main", branch="renamed",
        worktrees=[{"path": "/project", "branch": "develop", "main": True, "locked": False},
                   {"path": "/worktree", "branch": "renamed", "main": False, "locked": False}],
    )))
    content = f"/{name}" + (" --commit" if legacy_commit_flag else "")
    command = await prepare_command(AsyncMock(), session, parse_command(content), content)
    assert (command["context"]["branch"], command["context"]["remote"], command["context"]["targetBranch"]) == ("renamed", remote, "develop")
    assert all(text in command["prompt"] for text in (
        COMMIT_INSTRUCTIONS, "this action authorizes a task commit when needed",
        "Explicit user restrictions on creating commits take precedence",
        "only after the session checkout is clean", "never commit or discard unrelated changes",
    ))
    assert command["prompt"].count(COMMIT_RULES) == command["prompt"].count(REVIEW_INSTRUCTIONS) == 1
    message = Message(id=uuid4(), role="user", content=content, provider_data={"_matoloop": {"codingCommand": command}})
    assert message_payload(message)["commandPrompt"] in message_dict(message)["content"]


@pytest.mark.parametrize("name", ["git-review", "git-commit", "git-push", "git-sync"])
async def test_commit_option_is_rejected_for_other_git_commands(name):
    with pytest.raises(SessionServiceError, match="validation.invalid"):
        await prepare_command(AsyncMock(), ChatSession(id=uuid4()), parse_command(f"/{name}"), f"/{name} --commit")


async def test_command_ignores_a_git_target_from_another_computer(monkeypatch):
    agent_id = uuid4()
    session = ChatSession(id=uuid4(), user_id=uuid4(), preferred_execution_agent_id=agent_id, current_directory="/worktree", workspace_mode="worktree", coding_workspace={
        "mode": "worktree", "executionAgentId": str(uuid4()), "repository": "/project", "path": "/worktree", "branch": "session",
        "remote": "origin", "targetBranch": "develop",
    })
    monkeypatch.setattr("mato_loop_backend.sessions.git_commands.inspect_repository", AsyncMock(return_value=RepositoryResult(
        repository="/worktree", remotes=["origin"], defaultRemote="origin", defaultBranch="main", branch="session",
    )))
    command = await prepare_command(AsyncMock(), session, parse_command("/git-review"))
    assert command["context"]["targetBranch"] == "main"


async def test_local_command_rejects_a_changed_publication_target(monkeypatch):
    session = ChatSession(id=uuid4(), user_id=uuid4(), preferred_execution_agent_id=uuid4(), current_directory="/project", workspace_mode="local")
    info = RepositoryResult(
        repository="/project", remotes=["origin"], defaultRemote="origin", defaultBranch="main", branch="main",
        remoteIdentities={"origin": "a" * 64},
    )
    monkeypatch.setattr("mato_loop_backend.sessions.git_commands.inspect_repository", AsyncMock(return_value=info))
    await prepare_command(AsyncMock(), session, parse_command("/git-remote"), "/git-remote origin https://example.com/new.git")
    info.remote_identities["origin"] = "b" * 64
    with pytest.raises(SessionServiceError, match="git.worktreeChanged"):
        await prepare_command(AsyncMock(), session, parse_command("/git-sync"))


async def test_worktree_checks_destination_policy_before_creating_files(monkeypatch):
    from mato_loop_backend.sessions.coding import select_session_workspace

    agent_id = uuid4()
    session = ChatSession(id=uuid4(), user_id=uuid4(), preferred_execution_agent_id=agent_id, session_profile={"worktrees_enabled": True})
    project = Project(id=uuid4(), user_id=session.user_id, default_execution_agent_id=agent_id,
                      default_current_directory="/project", git_remote="origin", git_target_branch="main")
    client = AsyncMock()
    monkeypatch.setattr("mato_loop_backend.sessions.coding.coding_client", AsyncMock(return_value=client))
    db = AsyncMock()
    db.scalar.return_value = None
    db.get.return_value = ExecutionAgent(home_directory="/workspace", external_filesystem_policy="deny")
    monkeypatch.setattr("mato_loop_backend.sessions.coding.inspect_repository", AsyncMock(return_value=RepositoryResult(
        repository="/project", remotes=[], defaultRemote=None, defaultBranch="main", branch="main", worktreeRoot="/data/worktrees", projectWorktreeDirectory="/data/worktrees/12345678",
    )))
    with pytest.raises(SessionServiceError, match="permission.denied"):
        await select_session_workspace(db, session, project, create=True)
    client.request.assert_not_awaited()


@pytest.mark.parametrize("outside", [False, True])
@pytest.mark.parametrize("attached", [False, True])
@pytest.mark.parametrize("remote", [None, "upstream"])
async def test_worktree_creation_binds_an_independent_identity(monkeypatch, outside, attached, remote):
    from mato_loop_backend.sessions.coding import select_session_workspace

    agent_id = uuid4()
    session = ChatSession(id=uuid4(), user_id=uuid4(), preferred_execution_agent_id=agent_id, session_profile={"worktrees_enabled": True})
    project = Project(id=uuid4(), user_id=session.user_id, default_execution_agent_id=agent_id, default_current_directory="/project", git_target_branch="develop", git_remote=remote)
    identity = uuid4()
    name = uuid5(identity, "1").hex[:8]
    destination = f"/data/worktrees/{str(project.id)[:8]}/{name}"
    client = AsyncMock()
    client.request.return_value = WorktreeResult(
        repository="/project", path="/outside" if outside else destination,
        branch=f"worktree-{name}" if attached else None,
        baseCommit="a" * 40, targetBranch="develop", commonDirectory="/project/.git",
        gitDirectory="/project/.git/worktrees/session",
    )
    monkeypatch.setattr("mato_loop_backend.sessions.coding.coding_client", AsyncMock(return_value=client))
    monkeypatch.setattr("mato_loop_backend.sessions.coding.inspect_repository", AsyncMock(return_value=RepositoryResult(
        repository="/project", commonDirectory="/project/.git", remotes=[], defaultRemote=None, defaultBranch="main", branch="main", worktreeRoot="/data/worktrees", projectWorktreeDirectory=f"/data/worktrees/{str(project.id)[:8]}",
    )))
    db = AsyncMock()
    db.scalar.return_value = None
    db.execute.return_value = [("/other-worktree", {}, {
        "executionAgentId": str(agent_id), "path": "/other-worktree", "commonDirectory": "/project/.git",
    }, uuid4(), "Other task", "running")]
    db.get.return_value = ExecutionAgent(home_directory="/workspace", external_filesystem_policy="auto")
    if outside:
        with pytest.raises(SessionServiceError, match="executionAgent.protocolError"):
            await select_session_workspace(db, session, project, create=True, worktree_id=identity)
        return
    monkeypatch.setattr("mato_loop_backend.sessions.service.validate_execution_preferences", AsyncMock(return_value=(destination, "auto")))
    await select_session_workspace(db, session, project, create=True, worktree_id=identity)
    assert (session.current_directory, client.request.await_args.args[1]["sessionId"],
            client.request.await_args.args[1]["remote"]) == (destination, str(identity), remote)


@pytest.mark.parametrize("registered", [True, False])
async def test_existing_external_worktree_is_selected_only_when_registered_without_creating_a_branch(monkeypatch, registered):
    from mato_loop_backend.sessions.coding import select_session_workspace

    session = ChatSession(id=uuid4(), user_id=uuid4(), preferred_execution_agent_id=uuid4(), session_profile={"worktrees_enabled": True})
    session.current_directory = "/project"
    project = Project(id=uuid4(), user_id=session.user_id, default_execution_agent_id=uuid4(), default_current_directory="/other")
    info = RepositoryResult(repository="/project", commonDirectory="/project/.git", remotes=[], defaultRemote=None,
        defaultBranch="develop", branch="develop", worktrees=[{"path": "/project", "branch": "develop", "main": True, "locked": False},
            *([{"path": "/external", "branch": "task", "main": False, "locked": False}] if registered else [])])
    monkeypatch.setattr("mato_loop_backend.sessions.coding.inspect_repository", AsyncMock(side_effect=[info, info.model_copy(update={"repository": "/external", "branch": "task"})]))
    monkeypatch.setattr("mato_loop_backend.sessions.service.validate_execution_preferences", AsyncMock(return_value=("/external", "auto")))
    client = AsyncMock()
    monkeypatch.setattr("mato_loop_backend.sessions.coding.coding_client", AsyncMock(return_value=client))
    db = AsyncMock()
    db.scalar.return_value = None
    if not registered:
        with pytest.raises(SessionServiceError, match="git.worktreeChanged"):
            await select_session_workspace(db, session, project, "/external")
        client.request.assert_not_awaited()
        return
    await select_session_workspace(db, session, project, "/external")
    assert (session.current_directory, client.request.await_count) == ("/external", 0)


async def test_workspace_switch_grants_the_selected_working_directory(monkeypatch):
    from sqlalchemy.ext.asyncio import AsyncSession

    from mato_loop_backend.api.routes import git as routes
    from mato_loop_backend.permissions.policy import execution_directory_scope
    from mato_loop_backend.settings import AiSettings
    from mato_loop_backend.settings.models import LlmProviderConfig, ModelConfig

    provider = LlmProviderConfig(name="Test", adapter_type="openai-compatible")
    model = ModelConfig(provider_id=provider.id, model_id="test")
    session = ChatSession(id=uuid4(), user_id=uuid4(), preferred_execution_agent_id=uuid4(),
                          current_directory="/external", agent_profile_id=uuid4(), model_config_id=model.id)
    db = AsyncMock(spec=AsyncSession)
    db.scalar.return_value = None
    monkeypatch.setattr(routes, "workspace_session", AsyncMock(return_value=(session, Project())))
    monkeypatch.setattr(routes, "select_session_workspace", AsyncMock(return_value="ask"))
    monkeypatch.setattr(routes, "load_effective_ai", AsyncMock(return_value=AiSettings(providers=[provider], models=[model])))
    await routes.switch_workspace(session.id, routes.SessionWorkspaceRequest(directory="/external"), UserAccount(id=session.user_id), db)
    grant = db.add.call_args.args[0]
    assert (grant.capability, grant.resource_scope, grant.agent_profile_id, grant.trust_level) == (
        "execution.directory", execution_directory_scope(session.preferred_execution_agent_id, "/external"), session.agent_profile_id, "public",
    )


@pytest.mark.parametrize("legacy_preview", [False, True])
async def test_repository_checks_the_actual_git_metadata_destination(monkeypatch, legacy_preview):
    validate = AsyncMock(side_effect=[("/repo", "auto"), SessionServiceError(403, "permission.denied")])
    monkeypatch.setattr("mato_loop_backend.sessions.service.validate_execution_preferences", validate)
    client = AsyncMock()
    client.request.return_value = RepositoryResult(
        repository="/repo", commonDirectory="/outside/metadata", remotes=[],
        defaultRemote=None, defaultBranch="main", branch="main",
    )
    if legacy_preview:
        from mato_loop_backend.execution.client import ExecutionClientError

        client.request.side_effect = [
            ExecutionClientError("tool.invalidArguments", {"field": "checkRemoval", "reason": "extra_field"}),
            client.request.return_value,
        ]
    monkeypatch.setattr("mato_loop_backend.sessions.coding.coding_client", AsyncMock(return_value=client))
    with pytest.raises(SessionServiceError, match="permission.denied"):
        await inspect_repository(AsyncMock(), uuid4(), uuid4(), "/repo", check_removal=legacy_preview)
    assert validate.await_args.args[-1] == "/outside/metadata"
    assert client.request.await_count == (2 if legacy_preview else 1)


async def test_queued_followup_rejects_a_predecessor_from_another_session():
    from mato_loop_backend.sessions.message_queue import enqueue

    db = AsyncMock()
    db.get.side_effect = [None, None, Message(session_id=uuid4())]
    with pytest.raises(SessionServiceError, match="validation.invalid"):
        await enqueue(db, ChatSession(id=uuid4()), uuid4(), {"content": "Follow up"}, after_run_id=uuid4())


@pytest.mark.parametrize("changed_field", [None, "targetBranch", "remote", "remoteIdentity"])
def test_followup_revalidates_the_git_target(changed_field):
    context = {"repository": "/worktree", "targetBranch": "main", "remote": "origin", "remoteIdentity": "a" * 64}
    fresh = {**context, changed_field: "other"} if changed_field else context
    if changed_field:
        with pytest.raises(SessionServiceError, match="git.worktreeChanged"):
            validate_git_context(context, fresh)
    else:
        validate_git_context(context, fresh)


@pytest.mark.parametrize("source", ["codingFinish", "followup"])
def test_followup_message_shows_the_sent_prompt_and_preserves_model_provenance(source):
    from mato_loop_backend.agent.projection import command_message_content

    message = Message(id=uuid4(), role="user", content="/git-commit", provider_data={"_matoloop": {
        "source": {"type": source},
        "codingCommand": {"label": "Commit", "prompt": "Internal instructions", "instructions": "Commit task changes."},
    }})
    payload = message_payload(message)
    assert (payload["sourceType"], payload["commandPrompt"], "not a new user request" in command_message_content(message)) == (
        source, "Internal instructions", True,
    )


async def test_continuation_rejects_coding_commands_before_run_reservation(monkeypatch):
    from types import SimpleNamespace

    from mato_loop_backend.agent.runtime import AgentRuntime
    from mato_loop_backend.sessions.service import continue_run

    session = ChatSession(id=uuid4(), user_id=uuid4())
    db = AsyncMock()
    db.scalar.return_value = None
    monkeypatch.setattr("mato_loop_backend.sessions.archive.is_session_archived", AsyncMock(return_value=False))
    reserve = AsyncMock()
    monkeypatch.setattr(AgentRuntime, "reserve_continuation", reserve)
    with pytest.raises(SessionServiceError, match="session.runAlreadyActive"):
        await continue_run(session.id, uuid4(), SimpleNamespace(content="/git-merge", message_id=uuid4()),
                           SimpleNamespace(id=session.user_id), db, attachments=None,
                           owned_session_query=AsyncMock(return_value=session), message_serializer=lambda value: value)
    reserve.assert_not_called()


def test_command_task_survives_compaction_and_model_session_retrieval():
    marker = "Target: origin/develop; merge review before publishing."
    message = Message(id=uuid4(), role="user", content="/git-merge", provider_data={"_matoloop": {
        "codingCommand": {"instructions": marker},
    }})
    summary = _summary_source(None, [message])
    retrieved, _ = _message_item(message, 12_000, {}, TrustLevel.PRIVATE, reference_tool_results=False)
    assert all(marker in text for text in (summary, retrieved["content"]))


@pytest.mark.parametrize("name", list(OUTPUT_FORMATS))
@pytest.mark.parametrize("directory", [None, "/project"])
async def test_git_commands_are_prepared_without_git_prerequisites(monkeypatch, name, directory):
    session = ChatSession(id=uuid4(), user_id=uuid4(), preferred_execution_agent_id=uuid4() if directory else None,
                          current_directory=directory, workspace_mode="local")
    monkeypatch.setattr("mato_loop_backend.sessions.git_commands.inspect_repository", AsyncMock(return_value=RepositoryResult(
        repository=None, remotes=[], defaultRemote=None, defaultBranch=None, branch=None,
    )))
    task = await prepare_command(AsyncMock(), session, parse_command(f"/{name}"))
    assert task["context"]["workspaceDirectory"] == directory
    assert all(text in task["instructions"] for text in (
        "Read and follow current repository instructions and referenced guidelines",
        "Reuse reviews and successful checks only when their evidence still covers the current code",
        "A commit alone does not invalidate a review of unchanged content",
        "When this action requires review or checks",
        "cover missing, incomplete or stale evidence and rerun affected checks",
        "Reuse never bypasses current permission and target checks",
    ))
    assert ((
        "This integration/PR needs a separate worktree; an attached source branch must differ from the target. "
    ) in task["instructions"]) == (name in {"git-merge", "git-pr"})


def test_existing_session_cannot_change_workspace_mode():
    from mato_loop_backend.api.routes.sessions import SessionUpdateRequest

    with pytest.raises(ValueError):
        SessionUpdateRequest(workspace_mode="worktree")


@pytest.mark.parametrize("url", ["https://user:secret@example.com/repo.git", "https://example.com/repo?token=secret"])
async def test_remote_flow_rejects_embedded_credentials(monkeypatch, url):
    session = ChatSession(id=uuid4(), user_id=uuid4(), current_directory="/project", workspace_mode="local")
    monkeypatch.setattr("mato_loop_backend.sessions.git_commands.inspect_repository", AsyncMock(return_value=RepositoryResult(
        repository="/project", remotes=[], defaultRemote=None, defaultBranch="main", branch="main",
    )))
    with pytest.raises(SessionServiceError, match="git.invalidRemote"):
        await prepare_command(AsyncMock(), session, parse_command("/git-remote"), f"/git-remote origin {url}")


async def test_target_adoption_rejects_a_remote_changed_since_the_preview(monkeypatch):
    from mato_loop_backend.api.errors import ApiError
    from mato_loop_backend.api.routes.git import GitTargetRequest, set_target

    user = UserAccount(id=uuid4())
    session = ChatSession(id=uuid4(), user_id=user.id, current_directory="/project", workspace_mode="local")
    monkeypatch.setattr("mato_loop_backend.api.routes.git.get_owned_session", AsyncMock(return_value=session))
    monkeypatch.setattr("mato_loop_backend.sessions.archive.is_session_archived", AsyncMock(return_value=False))
    monkeypatch.setattr("mato_loop_backend.api.routes.git.inspect_repository", AsyncMock(return_value=RepositoryResult(
        repository="/project", commonDirectory="/project/.git", remotes=["origin"], defaultRemote="origin",
        defaultBranch="main", branch="main", remoteIdentities={"origin": "b" * 64},
    )))
    with pytest.raises(ApiError) as error:
        await set_target(session.id, GitTargetRequest(remote="origin", remote_identity="a" * 64), user, AsyncMock())
    assert error.value.code == "git.worktreeChanged"


async def test_target_adoption_does_not_change_an_unrelated_project_default(monkeypatch):
    from mato_loop_backend.api.routes.git import GitTargetRequest, set_target

    user = UserAccount(id=uuid4())
    agent_id = uuid4()
    project = Project(id=uuid4(), user_id=user.id, default_execution_agent_id=agent_id,
                      default_current_directory="/other", git_remote="upstream")
    session = ChatSession(id=uuid4(), user_id=user.id, project_id=project.id,
                          preferred_execution_agent_id=agent_id, current_directory="/project", workspace_mode="local")
    monkeypatch.setattr("mato_loop_backend.api.routes.git.get_owned_session", AsyncMock(return_value=session))
    monkeypatch.setattr("mato_loop_backend.sessions.archive.is_session_archived", AsyncMock(return_value=False))
    monkeypatch.setattr("mato_loop_backend.api.routes.git.inspect_repository", AsyncMock(return_value=RepositoryResult(
        repository="/project", commonDirectory="/project/.git", remotes=["origin"], defaultRemote="origin",
        defaultBranch="main", branch="main", remoteIdentities={"origin": "a" * 64},
    )))
    db = AsyncMock()
    db.scalar.return_value = project
    result = await set_target(session.id, GitTargetRequest(remote="origin", remote_identity="a" * 64), user, db)
    assert (result["projectId"], project.git_remote, session.coding_workspace["remote"]) == (None, "upstream", "origin")


@pytest.mark.parametrize("directory", ["/worktree", "/unrelated"])
@pytest.mark.parametrize("active_directory", ["/worktree/subdirectory", "/other-worktree"])
async def test_worktree_removal_protects_only_overlapping_active_runs(monkeypatch, directory, active_directory):
    from mato_loop_backend.api.errors import ApiError
    from mato_loop_backend.api.routes import git as routes
    from mato_loop_backend.sessions import coding

    user = UserAccount(id=uuid4())
    agent_id = uuid4()
    project = Project(id=uuid4(), user_id=user.id, default_execution_agent_id=uuid4(), default_current_directory="/other")
    session = ChatSession(id=uuid4(), user_id=user.id, project_id=project.id,
                          preferred_execution_agent_id=agent_id, current_directory="/project")
    affected = ChatSession(id=uuid4(), user_id=user.id, preferred_execution_agent_id=agent_id,
                           current_directory="/worktree/subdirectory", workspace_mode="worktree",
                           coding_workspace={"mode": "worktree", "path": "/worktree"})
    client = AsyncMock()
    client.request.return_value = WorktreeRemovedResult(removed=True)
    monkeypatch.setattr(routes, "workspace_session", AsyncMock(return_value=(session, project)))
    monkeypatch.setattr(routes, "inspect_repository", AsyncMock(return_value=RepositoryResult(
        repository="/project", commonDirectory="/project/.git", remotes=[], defaultRemote=None,
        defaultBranch="main", branch="task",
        worktrees=[{"path": "/project", "branch": "main", "main": True, "locked": False},
                   {"path": "/worktree", "branch": "task", "main": False, "locked": False}],
    )))
    monkeypatch.setattr(coding, "coding_client", AsyncMock(return_value=client))
    monkeypatch.setattr("mato_loop_backend.sessions.service.validate_execution_preferences", AsyncMock(return_value=("/project", "auto")))
    monkeypatch.setattr("mato_loop_backend.settings.load_effective_ai", AsyncMock(return_value=AiSettings()))
    grant = AsyncMock()
    monkeypatch.setattr("mato_loop_backend.sessions.service.add_directory_grant", grant)
    db = AsyncMock()
    db.scalars.side_effect = [[active_directory], [affected]]

    if directory == "/unrelated":
        with pytest.raises(ApiError, match="git.worktreeChanged"):
            await routes.git_action(session.id, routes.GitActionRequest(action="remove", directory=directory, force=True), user, db)
        return
    if active_directory == "/worktree/subdirectory":
        with pytest.raises(ApiError, match="git.worktreeInUse"):
            await routes.git_action(session.id, routes.GitActionRequest(action="remove", directory=directory, force=True), user, db)
        client.request.assert_not_awaited()
        return
    await routes.git_action(session.id, routes.GitActionRequest(action="remove", directory=directory, force=True), user, db)

    assert client.request.await_args.args[1]["path"] == "/worktree"
    assert (session.current_directory, affected.current_directory, affected.workspace_mode,
            affected.coding_workspace["path"]) == ("/project", "/project", "local", "/project")
    grant.assert_awaited_once()


async def test_branch_management_uses_the_session_repository_after_project_defaults_change(monkeypatch):
    from mato_loop_backend.api.routes import git as routes

    user = UserAccount(id=uuid4())
    project = Project(id=uuid4(), user_id=user.id, default_execution_agent_id=uuid4(), default_current_directory="/other")
    session = ChatSession(id=uuid4(), user_id=user.id, project_id=project.id,
                          preferred_execution_agent_id=uuid4(), current_directory="/project/src")
    monkeypatch.setattr(routes, "git_session", AsyncMock(return_value=session))
    monkeypatch.setattr(routes, "inspect_repository", AsyncMock(return_value=RepositoryResult(
        repository="/project", commonDirectory="/project/.git", remotes=[], defaultRemote=None, defaultBranch="main", branch="main",
        worktrees=[{"path": "/project", "branch": "main", "main": True, "locked": False}],
    )))
    client, db = AsyncMock(), AsyncMock()
    db.scalar.return_value = project
    monkeypatch.setattr(routes, "coding_client", AsyncMock(return_value=client))
    await routes.git_action(session.id, routes.GitActionRequest(action="branch-create", directory="/project/src", name="task"), user, db)
    assert client.request.await_args.args[:2] == ("git.branch", {
        "path": "/project", "action": "create", "name": "task", "targetBranch": None,
    })


async def test_worktree_details_derive_sessions_from_the_current_directory_and_keep_unknown_status(monkeypatch):
    from mato_loop_backend.execution.client import ExecutionClientError
    from mato_loop_backend.sessions import coding
    from mato_loop_backend.sessions.coding import WorktreeStatusResult

    user, agent_id = UserAccount(id=uuid4()), uuid4()
    session = ChatSession(id=uuid4(), title="Shared work", user_id=user.id, preferred_execution_agent_id=agent_id,
                          current_directory="/worktree/subdirectory", coding_workspace={"path": "/other"})
    info = RepositoryResult(repository="/project", commonDirectory="/project/.git", remotes=[], defaultRemote=None, defaultBranch="main", branch="main",
        worktrees=[{"path": path, "branch": "task", "main": False, "locked": False} for path in ("/worktree", "/other")])
    client = AsyncMock()
    client.request.side_effect = [info, WorktreeStatusResult(repository="/worktree", dirty=True), ExecutionClientError("path.unavailable")]
    monkeypatch.setattr(coding, "coding_client", AsyncMock(return_value=client))
    monkeypatch.setattr("mato_loop_backend.sessions.service.validate_execution_preferences", AsyncMock(return_value=("/project", "auto")))
    db = AsyncMock()
    db.get.return_value = ExecutionAgent(home_directory="/project", home_filesystem_policy="auto", external_filesystem_policy="auto")
    db.scalars.return_value = [session]
    result = await inspect_repository(db, user.id, agent_id, "/project", worktree_details=True)
    assert [(item.dirty, [entry.id for entry in item.sessions]) for item in result.worktrees] == [(True, [session.id]), (None, [])]
