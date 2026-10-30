from unittest.mock import AsyncMock, Mock
from uuid import uuid4

import pytest

from mato_loop_backend.agent.projection import _summary_source, message_dict
from mato_loop_backend.api.routes.projects import ProjectRequest
from mato_loop_backend.messaging.telegram import telegram_commands
from mato_loop_backend.models import ChatSession, ExecutionAgent, Message, Project, UserAccount
from mato_loop_backend.sessions.coding import (
    RepositoryResult,
    WorktreeResult,
    inspect_repository,
    prepare_session_worktree,
)
from mato_loop_backend.sessions.commands import (
    parse_command,
    prepare_command,
    registered_commands,
)
from mato_loop_backend.sessions.git_commands import check_workspace_admission
from mato_loop_backend.sessions.serialization import message_payload
from mato_loop_backend.sessions.service import SessionServiceError
from mato_loop_backend.settings import TrustLevel
from mato_loop_backend.tools.sessions import _message_item


def test_browser_and_telegram_share_portable_coding_commands():
    assert {command.command for command in telegram_commands() if command.command.startswith("git_")} == {
        item.name.replace("-", "_") for item in registered_commands()
    }
    assert (parse_command("/git-review").name, parse_command("/git_review@my_bot").name) == ("git-review", "git-review")
    assert parse_command("Explain /git-merge") is None


def test_git_merge_and_pr_follow_explicit_commit_message_rules():
    rules = (
        "Follow supplied repository commit-message rules for every commit, including merge commits. "
        "Read relevant referenced guidelines before committing. Apply required format, language, and trailers. "
        "Use recent history only where no explicit rule applies. Verify the resulting commit message."
    )
    assert all(rules in parse_command(f"/{name}").instructions for name in ("git-merge", "git-pr"))


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
    assert (parsed, [item["source"] for item in catalog], prepare.await_args.args[-1]) == (command, ["example"], "/example-task some arguments")


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
    assert message_payload(message)["commandPrompt"] == definition.instructions + "\n\nonly parser changes"


@pytest.mark.parametrize("changed", ["branch", "computer"])
async def test_command_rejects_changed_worktree_binding(monkeypatch, changed):
    agent_id = uuid4()
    session = ChatSession(id=uuid4(), user_id=uuid4(), preferred_execution_agent_id=agent_id, current_directory="/worktree", workspace_mode="worktree", coding_workspace={
        "mode": "worktree", "executionAgentId": str(uuid4() if changed == "computer" else agent_id), "repository": "/project", "path": "/worktree", "branch": "session",
        "remote": "origin", "targetBranch": "develop",
    })
    monkeypatch.setattr("mato_loop_backend.sessions.git_commands.inspect_repository", AsyncMock(return_value=RepositoryResult(
        repository="/worktree", remotes=["origin"], defaultRemote="origin", defaultBranch="main", branch="other" if changed == "branch" else "session",
    )))
    with pytest.raises(SessionServiceError, match="git.worktreeChanged"):
        await prepare_command(AsyncMock(), session, parse_command("/git-merge"))


async def test_local_command_rejects_a_changed_publication_target(monkeypatch):
    session = ChatSession(id=uuid4(), user_id=uuid4(), current_directory="/project", workspace_mode="local")
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
    agent_id = uuid4()
    project = Project(id=uuid4(), default_execution_agent_id=agent_id,
                      default_current_directory="/project", git_remote="origin", git_target_branch="main")
    session = ChatSession(id=uuid4(), user_id=uuid4(), preferred_execution_agent_id=agent_id,
                          current_directory="/project", workspace_mode="worktree")
    client = AsyncMock()
    monkeypatch.setattr("mato_loop_backend.sessions.coding.coding_client", AsyncMock(return_value=client))
    db = AsyncMock()
    db.get.return_value = ExecutionAgent(home_directory="/workspace", external_filesystem_policy="deny")
    monkeypatch.setattr("mato_loop_backend.sessions.coding.inspect_repository", AsyncMock(return_value=RepositoryResult(
        repository="/project", remotes=[], defaultRemote=None, defaultBranch="main", branch="main", worktreeRoot="/data/worktrees", worktreeLayout="short-session",
    )))
    with pytest.raises(SessionServiceError, match="permission.denied"):
        await prepare_session_worktree(db, session, project)
    client.request.assert_not_awaited()


@pytest.mark.parametrize(("layout", "path_kind"), [
    ("short-session", "current"), ("project-session", "current"),
    ("short-session", "outside"), ("short-session", "legacy"), ("short-session", "legacy_denied"),
])
async def test_session_prepares_a_worktree_without_a_remote(monkeypatch, layout, path_kind):
    agent_id = uuid4()
    project = Project(id=uuid4(), default_execution_agent_id=agent_id, default_current_directory="/project", git_target_branch="develop")
    session = ChatSession(id=uuid4(), user_id=uuid4(), preferred_execution_agent_id=agent_id,
                          current_directory="/project", workspace_mode="worktree")
    current_path = f"/data/worktrees/{str(session.id)[:8]}"
    destination = f"/data/worktrees/{project.id}/{session.id}" if layout == "project-session" or path_kind.startswith("legacy") else current_path
    client = AsyncMock()
    client.request.return_value = WorktreeResult(
        repository="/project", path="/outside" if path_kind == "outside" else destination,
        branch=f"worktree-{str(session.id)[:8]}" if layout != "project-session" else f"matoloop/{session.user_id}/{session.id}",
        baseCommit="a" * 40, remote=None, targetBranch="develop", remoteIdentity=None, commonDirectory="/project/.git",
        gitDirectory="/project/.git/worktrees/session",
    )
    monkeypatch.setattr("mato_loop_backend.sessions.coding.coding_client", AsyncMock(return_value=client))
    monkeypatch.setattr("mato_loop_backend.sessions.coding.inspect_repository", AsyncMock(return_value=RepositoryResult(
        repository="/project", commonDirectory="/project/.git", remotes=[], defaultRemote=None, defaultBranch="main", branch="main", worktreeRoot="/data/worktrees",
        **({"worktreeLayout": layout} if layout != "project-session" else {}),
    )))
    monkeypatch.setattr("mato_loop_backend.sessions.service.validate_execution_preferences", AsyncMock(return_value=(destination, "auto")))
    db = AsyncMock()
    db.get.return_value = ExecutionAgent(
        home_directory=current_path if path_kind == "legacy_denied" else "/workspace",
        home_filesystem_policy="auto", external_filesystem_policy="deny" if path_kind == "legacy_denied" else "auto",
    )
    if path_kind in {"outside", "legacy_denied"}:
        with pytest.raises(SessionServiceError, match="executionAgent.protocolError"):
            await prepare_session_worktree(db, session, project)
        if path_kind == "legacy_denied":
            assert client.request.await_args.args[1]["destinations"] == [current_path]
        return
    await prepare_session_worktree(db, session, project)
    assert (session.current_directory, session.coding_workspace["remote"]) == (destination, None)


async def test_worktree_removal_rejects_a_directory_still_selected_by_a_session(monkeypatch):
    from mato_loop_backend.sessions import coding

    user, agent_id, project_id, session_id = UserAccount(id=uuid4()), uuid4(), uuid4(), uuid4()
    session = ChatSession(id=session_id, user_id=user.id, coding_workspace={
        "mode": "worktree", "executionAgentId": str(agent_id), "projectId": str(project_id), "path": "/data/worktrees/project/session",
    })
    client, db = AsyncMock(), AsyncMock()
    monkeypatch.setattr(coding, "coding_client", AsyncMock(return_value=client))
    db.get.return_value = ExecutionAgent(home_directory="/workspace", external_filesystem_policy="auto")
    db.scalars.return_value = Mock(all=Mock(return_value=[ChatSession(current_directory=session.coding_workspace["path"])]))
    with pytest.raises(SessionServiceError) as error:
        await coding.remove_session_worktree(db, session, [session_id])
    assert (error.value.code, client.request.await_count) == ("git.worktreeInUse", 0)


async def test_repository_checks_the_actual_git_metadata_destination(monkeypatch):
    validate = AsyncMock(side_effect=[("/repo", "auto"), SessionServiceError(403, "permission.denied")])
    monkeypatch.setattr("mato_loop_backend.sessions.service.validate_execution_preferences", validate)
    client = AsyncMock()
    client.request.return_value = RepositoryResult(
        repository="/repo", commonDirectory="/outside/metadata", remotes=[],
        defaultRemote=None, defaultBranch="main", branch="main",
    )
    monkeypatch.setattr("mato_loop_backend.sessions.coding.coding_client", AsyncMock(return_value=client))
    with pytest.raises(SessionServiceError, match="permission.denied"):
        await inspect_repository(AsyncMock(), uuid4(), uuid4(), "/repo")
    assert validate.await_args.args[-1] == "/outside/metadata"


@pytest.mark.parametrize("incoming_command", [False, True])
@pytest.mark.parametrize("directory", ["/repo", "/repo/src", "C:/Repo/src"])
async def test_admission_blocks_overlap_in_both_directions(incoming_command, directory):
    root = "C:/repo" if directory.startswith("C:") else "/repo"
    command = {"name": "git-sync", "resources": [root]}
    db = AsyncMock()
    db.execute.return_value = [(directory, {} if incoming_command else {"_matoloop": {"codingCommand": command}}, None)]
    with pytest.raises(SessionServiceError, match="git.projectBusy"):
        await check_workspace_admission(db, uuid4(), uuid4(), directory, command if incoming_command else None)


async def test_admission_keeps_a_relocated_command_directory_reserved():
    db = AsyncMock()
    db.execute.return_value = [("/relocated", {"_matoloop": {"codingCommand": {
        "name": "git-sync", "resources": ["/original"],
    }}}, None)]
    with pytest.raises(SessionServiceError, match="git.projectBusy"):
        await check_workspace_admission(db, uuid4(), uuid4(), "/relocated")


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


async def test_init_is_prepared_for_a_workspace_without_a_repository(monkeypatch):
    session = ChatSession(id=uuid4(), user_id=uuid4(), current_directory="/project", workspace_mode="local")
    monkeypatch.setattr("mato_loop_backend.sessions.git_commands.inspect_repository", AsyncMock(return_value=RepositoryResult(
        repository=None, remotes=[], defaultRemote=None, defaultBranch=None, branch=None,
    )))
    task = await prepare_command(AsyncMock(), session, parse_command("/git-init"))
    assert task["context"]["workspaceDirectory"] == "/project"


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
    monkeypatch.setattr("mato_loop_backend.api.routes.git.check_workspace_admission", AsyncMock())
    monkeypatch.setattr("mato_loop_backend.api.routes.git.inspect_repository", AsyncMock(return_value=RepositoryResult(
        repository="/project", commonDirectory="/project/.git", remotes=["origin"], defaultRemote="origin",
        defaultBranch="main", branch="main", remoteIdentities={"origin": "a" * 64},
    )))
    db = AsyncMock()
    db.scalar.return_value = project
    result = await set_target(session.id, GitTargetRequest(remote="origin", remote_identity="a" * 64), user, db)
    assert (result["projectId"], project.git_remote, session.coding_workspace["remote"]) == (None, "upstream", "origin")
