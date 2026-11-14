import asyncio
import json
import os
import shlex
import subprocess
import sys
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from functools import partial
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4

from matoloop_agent import git as git_module
from matoloop_agent.config import AgentConfig
from matoloop_agent.errors import AgentError
from matoloop_agent.filesystem import Filesystem
from matoloop_agent.git import (
    MAX_TEXT_BYTES,
    changes,
    prepare_worktree,
    remove_worktree,
    repository,
)
from matoloop_agent.protocol import (
    GitBranchArguments,
    GitWorktreeArguments,
    GitWorktreeRemoveArguments,
)


class GitChangesTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.config = AgentConfig.model_validate({"homeDirectory": self.root})
        self.config._data_directory = self.root / "state"
        self.config.data_dir.mkdir()
        self.fs = Filesystem(self.config)

    def git(self, *args, cwd=None):
        return subprocess.check_output(
            ["git", "-c", "user.name=Test", "-c", "user.email=test@example.com", *args],
            cwd=cwd or self.root,
            stderr=subprocess.DEVNULL,
        )

    def arguments(self, source):
        project, session = str(uuid4()), str(uuid4())
        return GitWorktreeArguments(
            path=str(source),
            commonDirectory=str(source / ".git"),
            ownerId=str(uuid4()),
            projectId=project,
            sessionId=session,
            destinations=[
                (
                    git_module.project_worktree_directory(self.config, self.fs, project) / name
                ).as_posix()
                for name in [session[:8], uuid4().hex[:8]]
            ],
            targetBranch="develop",
        )

    async def managed_source(self):
        source = self.root / "source"
        self.git("init", "-b", "develop", str(source))
        (source / "file.txt").write_text("old\n")
        (source / ".gitattributes").write_text("file.txt filter=probe\n")
        self.git("add", ".", cwd=source)
        self.git("commit", "-m", "Initial", cwd=source)
        return source, self.arguments(source)

    async def test_plain_directory_and_unborn_repository(self):
        self.assertIsNone((await changes(self.fs, str(self.root)))["repository"])
        self.git("init")
        (self.root / "new.txt").write_text("new\n")
        self.assertEqual(
            (await changes(self.fs, str(self.root)))["files"],
            [{"path": "new.txt", "status": "added", "additions": 1, "deletions": 0}],
        )

    def local_source(self):
        source = self.root / "source"
        self.git("init", "-b", "develop", str(source))
        self.git("commit", "--allow-empty", "-m", "Initial", cwd=source)
        return source

    async def branch(self, directory, action, name, target=None):
        await git_module.branch_action(
            self.config,
            self.fs,
            GitBranchArguments(path=str(directory), action=action, name=name, targetBranch=target),
        )

    async def test_management_uses_external_worktrees_and_keeps_paths_when_renaming(self):
        source = self.local_source()
        path = self.root / "external"
        self.git("worktree", "add", "-b", "external-task", str(path), cwd=source)
        await self.branch(path, "rename", "feature/renamed")
        result = await repository(self.fs, str(source))
        self.assertEqual(
            next(item for item in result["worktrees"] if item["path"] == str(path))["branch"],
            "feature/renamed",
        )

    async def test_management_creates_an_independent_worktree_without_fetching(self):
        source = self.local_source()
        self.git("remote", "add", "origin", "https://127.0.0.1:1/unavailable.git", cwd=source)
        arguments = self.arguments(source)
        result = await prepare_worktree(self.config, self.fs, arguments)
        current = await repository(self.fs, result["path"])
        self.assertEqual(
            (result["path"], result["branch"], current["branch"], current["localBranches"]),
            (arguments.destinations[0], None, None, ["develop"]),
        )

    async def test_worktree_refreshes_the_selected_remote_before_freezing_its_base(self):
        source, args = await self.managed_source()
        remote = self.root / "remote.git"
        self.git("clone", "--bare", str(source), str(remote))
        self.git("remote", "add", "selected", str(remote), cwd=source)
        publisher = self.root / "publisher"
        self.git("clone", str(remote), str(publisher))
        (publisher / "file.txt").write_text("remote update\n")
        self.git("commit", "-am", "Remote update", cwd=publisher)
        self.git("push", "origin", "develop", cwd=publisher)
        latest = self.git("rev-parse", "HEAD", cwd=publisher).decode().strip()
        original = git_module._git

        async def local_remote(directory, *arguments, **kwargs):
            if kwargs.get("protocols"):
                kwargs["protocols"] = "file"
            return await original(directory, *arguments, **kwargs)

        with patch.object(git_module, "_git", side_effect=local_remote):
            result = await prepare_worktree(
                self.config, self.fs, args.model_copy(update={"remote": "selected"})
            )
        self.assertEqual(
            (
                result["baseCommit"],
                self.git("rev-parse", "HEAD", cwd=source).decode().strip(),
                (source / "file.txt").read_text(),
                (Path(result["path"]) / "file.txt").read_text(),
            ),
            (latest, latest, "remote update\n", "remote update\n"),
        )

    async def test_worktree_remote_refresh_preserves_local_work_on_failure(self):
        source, args = await self.managed_source()
        remote = self.root / "remote.git"
        self.git("clone", "--bare", str(source), str(remote))
        self.git("remote", "add", "origin", str(remote), cwd=source)
        args = args.model_copy(update={"remote": "origin"})
        original = git_module._git

        async def local_remote(directory, *arguments, **kwargs):
            if kwargs.get("protocols"):
                kwargs["protocols"] = "file"
            return await original(directory, *arguments, **kwargs)

        with patch.object(git_module, "_git", side_effect=local_remote):
            (source / "file.txt").write_text("uncommitted work\n")
            with self.assertRaises(AgentError) as dirty:
                await prepare_worktree(self.config, self.fs, args)
            self.git("commit", "-am", "Local work", cwd=source)
            local = self.git("rev-parse", "HEAD", cwd=source).decode().strip()
            self.git("checkout", "--detach", "HEAD~1", cwd=source)
            self.git("commit", "--allow-empty", "-m", "Divergent remote", cwd=source)
            self.git("push", "origin", "HEAD:develop", cwd=source)
            self.git("checkout", "develop", cwd=source)
            with self.assertRaises(AgentError) as divergent:
                await prepare_worktree(self.config, self.fs, args)
            self.git("remote", "set-url", "origin", str(self.root / "missing.git"), cwd=source)
            with self.assertRaises(AgentError) as unavailable:
                await prepare_worktree(self.config, self.fs, args)
        self.assertEqual(
            (
                dirty.exception.code,
                divergent.exception.code,
                unavailable.exception.code,
                self.git("rev-parse", "HEAD", cwd=source).decode().strip(),
                (source / "file.txt").read_text(),
                Path(args.destinations[0]).exists(),
            ),
            (
                "git.dirty",
                "git.actionFailed",
                "git.fetchFailed",
                local,
                "uncommitted work\n",
                False,
            ),
        )

    async def test_project_folders_reuse_short_ids_and_disambiguate_collisions(self):
        source = self.local_source()
        args = self.arguments(source)
        result = await prepare_worktree(self.config, self.fs, args)
        reused = git_module.project_worktree_directory(self.config, self.fs, args.project_id)
        duplicate = git_module.project_worktree_directory(
            self.config, self.fs, args.project_id[:8] + str(uuid4())[8:]
        )
        self.assertEqual(
            (Path(result["path"]).parent.name, reused.name, duplicate.name),
            (args.project_id[:8], args.project_id[:8], f"{args.project_id[:8]}-2"),
        )

    async def test_project_folder_selection_does_not_follow_a_symlink(self):
        source, args = await self.managed_source()
        folder = Path(args.destinations[0]).parent
        folder.parent.mkdir()
        outside = self.root / "outside"
        outside.mkdir()
        (outside / ".matoloop-project").write_text(args.project_id)
        folder.symlink_to(outside, target_is_directory=True)
        with self.assertRaises(AgentError) as error:
            await prepare_worktree(self.config, self.fs, args)
        self.assertEqual(error.exception.code, "git.worktreeChanged")

    async def test_status_only_distinguishes_clean_and_untracked_work(self):
        source = self.local_source()
        clean = await changes(self.fs, str(source), status_only=True)
        (source / "new.txt").write_text("new")
        dirty = await changes(self.fs, str(source), status_only=True)
        self.assertEqual((clean["dirty"], dirty["dirty"]), (False, True))

    async def test_management_removal_requires_consent_for_ignored_files_and_keeps_branch(self):
        source = self.local_source()
        path = self.root / "external"
        self.git("worktree", "add", "-b", "task", str(path), cwd=source)
        (path / ".gitignore").write_text(".env\n")
        self.git("add", ".gitignore", cwd=path)
        self.git("commit", "-m", "Ignore credentials", cwd=path)
        (path / ".env").write_text("secret")
        arguments = GitWorktreeRemoveArguments(
            path=str(path),
            repository=str(source),
            commonDirectory=str(source / ".git"),
        )
        with self.assertRaises(AgentError) as error:
            await remove_worktree(self.config, self.fs, arguments)
        self.assertEqual(error.exception.code, "git.dirty")
        await remove_worktree(self.config, self.fs, arguments.model_copy(update={"force": True}))
        self.assertEqual(
            self.git("show-ref", "--verify", "--quiet", "refs/heads/task", cwd=source), b""
        )

    async def test_management_rejects_unregistered_and_primary_checkout_removal(self):
        source = self.local_source()
        unrelated = self.root / "unrelated"
        unrelated.mkdir()
        for directory, code in (
            (str(unrelated), "git.worktreeChanged"),
            (str(source), "git.mainWorktree"),
        ):
            with self.subTest(directory=directory):
                with self.assertRaises(AgentError) as error:
                    await remove_worktree(
                        self.config,
                        self.fs,
                        GitWorktreeRemoveArguments(
                            path=directory,
                            repository=str(source),
                            commonDirectory=str(source / ".git"),
                            force=True,
                        ),
                    )
                self.assertEqual(error.exception.code, code)

    async def test_management_removes_detached_work_without_requiring_a_safety_branch(self):
        source = self.local_source()
        path = self.root / "external"
        self.git("worktree", "add", "--detach", str(path), cwd=source)
        self.git("commit", "--allow-empty", "-m", "Detached work", cwd=path)
        cache = path / "cache"
        cache.mkdir()
        (cache / "bytecode").touch()
        (source / ".git" / "info" / "exclude").write_text("cache/\n")
        access = os.access
        with patch.object(
            git_module.os,
            "access",
            side_effect=lambda target, mode, **kw: False
            if os.fspath(target) == str(cache)
            else access(target, mode, **kw),
        ):
            with self.assertRaises(AgentError) as error:
                await remove_worktree(
                    self.config,
                    self.fs,
                    GitWorktreeRemoveArguments(
                        path=str(path),
                        repository=str(source),
                        commonDirectory=str(source / ".git"),
                        force=True,
                    ),
                )
        self.assertEqual(
            (
                error.exception.code,
                error.exception.params,
                str(path) in self.git("worktree", "list", "--porcelain", cwd=source).decode(),
            ),
            ("git.worktreePermissionDenied", {"path": str(cache)}, True),
        )
        (cache / "bytecode").unlink()
        with patch.object(
            git_module.os,
            "access",
            side_effect=lambda target, mode, **kw: False
            if os.fspath(target) == str(cache) and mode & os.W_OK
            else access(target, mode, **kw),
        ):
            git_module._check_removal_permissions(str(path))
        await remove_worktree(
            self.config,
            self.fs,
            GitWorktreeRemoveArguments(
                path=str(path),
                repository=str(source),
                commonDirectory=str(source / ".git"),
                force=True,
            ),
        )
        self.assertFalse(path.exists())

    def test_removal_checks_sticky_entries_and_metadata_parent(self):
        path = self.root / "worktree"
        path.mkdir()
        cache = path / "cache"
        cache.mkdir()
        cache.chmod(0o1777)
        blocked = cache / "foreign"
        blocked.touch()
        metadata = self.root / "metadata" / "worktree"
        metadata.mkdir(parents=True)
        access = os.access
        for scenario in ("sticky", "metadata") if os.name == "posix" else ("metadata",):
            with self.subTest(scenario=scenario):
                with (
                    patch.object(git_module.os, "geteuid", return_value=123456, create=True),
                    patch.object(
                        git_module.os,
                        "access",
                        side_effect=lambda target, mode, scenario=scenario, **kw: False
                        if scenario == "metadata" and Path(target) == metadata.parent
                        else access(target, mode, **kw),
                    ),
                ):
                    if scenario == "metadata":
                        cache.chmod(0o777)
                    with self.assertRaises(AgentError) as error:
                        git_module._check_removal_permissions(str(path), str(metadata))
                self.assertEqual(
                    error.exception.params,
                    {
                        "path": str(blocked if scenario == "sticky" else metadata.parent),
                    },
                )

    async def test_management_does_not_overwrite_ignored_files_when_switching_branches(self):
        source = self.local_source()
        (source / ".gitignore").write_text(".env\n")
        self.git("add", ".gitignore", cwd=source)
        self.git("commit", "-m", "Ignore", cwd=source)
        self.git("checkout", "-b", "other", cwd=source)
        (source / ".env").write_text("tracked")
        self.git("add", "-f", ".env", cwd=source)
        self.git("commit", "-m", "Track file", cwd=source)
        self.git("checkout", "develop", cwd=source)
        (source / ".env").write_text("local secret")
        with self.assertRaises(AgentError):
            await self.branch(source, "switch", "other")
        self.assertEqual((source / ".env").read_text(), "local secret")

    async def test_branch_actions_create_switch_and_delete_local_branches(self):
        source = self.local_source()
        self.git("branch", "lagging", cwd=source)
        for action, name in (("create", "task"), ("switch", "task")):
            await self.branch(source, action, name)
        self.git("commit", "--allow-empty", "-m", "Task", cwd=source)
        self.git("branch", "--set-upstream-to=lagging", "task", cwd=source)
        self.assertEqual((await repository(self.fs, str(source)))["branch"], "task")
        with self.assertRaises(AgentError) as error:
            await self.branch(source, "delete", "task", "develop")
        self.assertEqual(error.exception.code, "git.branchInUse")
        await self.branch(source, "switch", "develop")
        self.git("merge", "--ff-only", "task", cwd=source)
        await self.branch(source, "delete", "task", "develop")
        self.assertEqual(
            (await repository(self.fs, str(source)))["localBranches"], ["develop", "lagging"]
        )

    async def test_branch_deletion_preserves_unmerged_commits(self):
        source = self.local_source()
        self.git("checkout", "-b", "task", cwd=source)
        self.git("commit", "--allow-empty", "-m", "Task", cwd=source)
        self.git("checkout", "develop", cwd=source)
        with self.assertRaises(AgentError) as error:
            await self.branch(source, "delete", "task", "develop")
        self.assertEqual(error.exception.code, "git.branchUnmerged")

    async def test_repository_created_later_is_detected(self):
        await repository(self.fs, str(self.root))
        self.git("init", "-b", "develop")
        self.git("remote", "add", "origin", "https://example.com/project.git")
        result = await repository(self.fs, str(self.root))
        self.assertEqual(
            (result["repository"], result["defaultRemote"], result["defaultBranch"]),
            (str(self.root), "origin", "develop"),
        )

    async def test_repository_does_not_expose_credentials_in_remote_addresses(self):
        self.git("init")
        self.git("remote", "add", "origin", "https://user:secret@example.com/repo.git")
        result = await repository(self.fs, str(self.root))
        self.assertIsNone(result["remoteUrls"]["origin"])

    async def test_repository_lists_local_branches_without_tags_or_remote_refs(self):
        self.git("init", "-b", "develop")
        self.git("commit", "--allow-empty", "-m", "Initial")
        self.git("branch", "feature/release")
        self.git("tag", "feature/release")
        self.git("update-ref", "refs/remotes/origin/remote-only", "HEAD")
        result = await repository(self.fs, str(self.root))
        self.assertEqual(result["localBranches"], ["develop", "feature/release"])

    async def test_repository_truncates_branch_choices_without_failing_inspection(self):
        self.git("init", "-b", "develop")
        self.git("commit", "--allow-empty", "-m", "Initial")
        commit = self.git("rev-parse", "HEAD").decode().strip()
        subprocess.run(
            ["git", "update-ref", "--stdin"],
            cwd=self.root,
            input="".join(f"create refs/heads/branch-{index} {commit}\n" for index in range(1000)),
            text=True,
            check=True,
            capture_output=True,
        )
        result = await repository(self.fs, str(self.root))
        self.assertEqual(
            (len(result["localBranches"]), result["localBranchesTruncated"]), (1000, True)
        )

    async def test_repository_identity_includes_additional_push_destinations(self):
        self.git("init")
        self.git("remote", "add", "origin", "https://example.com/project.git")
        self.git("config", "remote.origin.pushurl", "https://example.com/project.git")
        before = (await repository(self.fs, str(self.root)))["remoteIdentities"]["origin"]
        self.git(
            "config", "--add", "remote.origin.pushurl", "https://other.example.com/project.git"
        )
        self.assertNotEqual(
            (await repository(self.fs, str(self.root)))["remoteIdentities"]["origin"], before
        )

    async def test_repository_target_uses_the_branch_name_when_a_tag_has_the_same_name(self):
        self.git("init", "-b", "release")
        self.git("commit", "--allow-empty", "-m", "Initial")
        self.git("tag", "release")
        self.assertEqual((await repository(self.fs, str(self.root)))["defaultBranch"], "release")

    async def test_repository_reports_resolved_git_metadata_for_policy_checks(self):
        source = self.root / "source"
        self.git("init", str(source))
        metadata = self.root / "metadata"
        (source / ".git").rename(metadata)
        (source / ".git").symlink_to(metadata, target_is_directory=True)
        result = await repository(self.fs, str(source))
        self.assertEqual(result["commonDirectory"], str(metadata))

    async def test_worktree_preparation_reuses_existing_work(self):
        source, args = await self.managed_source()
        result = await prepare_worktree(self.config, self.fs, args)
        worktree = Path(result["path"])
        (worktree / "file.txt").write_text("session work\n")
        await prepare_worktree(self.config, self.fs, args)
        self.assertEqual((worktree / "file.txt").read_text(), "session work\n")

    async def test_worktree_does_not_reserve_a_technical_branch_name(self):
        source, args = await self.managed_source()
        self.git("branch", f"worktree-{args.session_id[:8]}", cwd=source)
        result = await prepare_worktree(self.config, self.fs, args)
        self.assertEqual(result["path"], args.destinations[0])

    async def test_worktree_chooses_another_name_when_the_directory_exists(self):
        source, args = await self.managed_source()
        folder = Path(args.destinations[0]).parent
        folder.mkdir(parents=True)
        (folder / ".matoloop-project").write_text(args.project_id)
        Path(args.destinations[0]).mkdir(parents=True)
        await prepare_worktree(self.config, self.fs, args)
        self.assertEqual(
            (await prepare_worktree(self.config, self.fs, args))["path"], args.destinations[1]
        )

    async def test_worktree_stops_when_all_candidate_names_are_occupied(self):
        source = self.local_source()
        args = self.arguments(source)
        folder = Path(args.destinations[0]).parent
        folder.mkdir(parents=True)
        (folder / ".matoloop-project").write_text(args.project_id)
        for destination in args.destinations:
            Path(destination).mkdir(parents=True)
        with self.assertRaises(AgentError) as error:
            await prepare_worktree(self.config, self.fs, args)
        self.assertEqual(error.exception.code, "git.worktreeFailed")

    async def test_preparation_requires_an_authorized_destination(self):
        source, args = await self.managed_source()
        with self.assertRaises(AgentError) as error:
            await prepare_worktree(
                self.config,
                self.fs,
                args.model_copy(update={"destinations": [str(self.root / "outside")]}),
            )
        self.assertEqual(error.exception.code, "git.worktreeChanged")

    async def test_worktree_without_remote_uses_the_committed_local_target(self):
        source = self.root / "source"
        self.git("init", "-b", "develop", str(source))
        (source / "file.txt").write_text("local target\n")
        self.git("add", ".", cwd=source)
        self.git("commit", "-m", "Local target", cwd=source)
        self.git("checkout", "-b", "other", cwd=source)
        (source / "file.txt").write_text("other branch\n")
        self.git("commit", "-am", "Other branch", cwd=source)
        (source / "file.txt").write_text("uncommitted work\n")
        result = await prepare_worktree(self.config, self.fs, self.arguments(source))
        self.assertEqual(
            (
                (Path(result["path"]) / "file.txt").read_text(),
                (source / "file.txt").read_text(),
            ),
            ("local target\n", "uncommitted work\n"),
        )

    @unittest.skipUnless(os.geteuid() == 0, "Requires ownership changes")
    async def test_worktree_checkout_uses_process_ownership_with_foreign_owned_agent_data(self):
        source, _ = await self.managed_source()
        os.chown(self.config.data_dir, 1000, 1000)
        result = await prepare_worktree(self.config, self.fs, self.arguments(source))
        self.assertEqual((Path(result["path"]) / "file.txt").read_text(), "old\n")

    async def test_local_worktree_requires_a_committed_target(self):
        source = self.root / "source"
        self.git("init", "-b", "develop", str(source))
        with self.assertRaises(AgentError) as error:
            await prepare_worktree(self.config, self.fs, self.arguments(source))
        self.assertEqual(error.exception.code, "git.localBranchRequired")
        self.assertFalse(
            (await repository(self.fs, str(source), target_branch="develop"))["targetHasCommit"]
        )

    async def test_preparation_does_not_execute_hooks_or_checkout_filters(self):
        source, args = await self.managed_source()
        marker = self.root / "executed"
        script = shlex.join(
            [sys.executable, "-c", f"from pathlib import Path; Path({str(marker)!r}).touch()"]
        )
        hook = source / ".git" / "hooks" / "reference-transaction"
        hook.write_text(f"#!/bin/sh\n{script}\n")
        hook.chmod(0o700)
        filters = self.root / "branch-filters"
        for option in ("smudge", "process"):
            self.git("config", "--file", str(filters), f"filter.probe.{option}", script)
        self.git("config", "--file", str(filters), "filter.probe.required", "true")
        self.git("config", "include.path", str(filters), cwd=source)
        await prepare_worktree(self.config, self.fs, args)
        self.assertFalse(marker.exists())

    async def test_preparation_finishes_when_filesystem_requests_fill_the_shared_executor(self):
        source, args = await self.managed_source()
        loop = asyncio.get_running_loop()
        loop.set_default_executor(ThreadPoolExecutor(max_workers=1))
        entered = asyncio.Event()
        blocked = None
        original = git_module._git

        def stat():
            loop.call_soon_threadsafe(entered.set)
            return self.fs.stat(str(source))

        async def checkout_then_stat(directory, *arguments, **kwargs):
            nonlocal blocked
            result = await original(directory, *arguments, **kwargs)
            if arguments[:2] == ("worktree", "add"):
                blocked = asyncio.create_task(asyncio.to_thread(stat))
                await entered.wait()
            return result

        try:
            with patch.object(git_module, "_git", checkout_then_stat):
                result = await asyncio.wait_for(prepare_worktree(self.config, self.fs, args), 10)
            self.assertEqual((Path(result["path"]) / "file.txt").read_text(), "old\n")
        finally:
            if blocked:
                await blocked

    async def test_preparation_skips_linked_destination_without_writing_through_it(self):
        source, args = await self.managed_source()
        folder = Path(args.destinations[0]).parent
        folder.mkdir(parents=True)
        (folder / ".matoloop-project").write_text(args.project_id)
        destination = Path(args.destinations[0])
        outside = self.root / "outside"
        outside.mkdir()
        destination.symlink_to(outside, target_is_directory=True)
        await prepare_worktree(self.config, self.fs, args)
        self.assertEqual(list(outside.iterdir()), [])

    async def test_preparation_rejects_redirected_objects(self):
        source, args = await self.managed_source()
        objects = source / ".git" / "objects"
        outside = self.root / "outside-objects"
        objects.rename(outside)
        objects.symlink_to(outside, target_is_directory=True)
        with self.assertRaises(AgentError) as error:
            await prepare_worktree(self.config, self.fs, args)
        self.assertEqual(error.exception.code, "git.worktreeFailed")

    async def test_recovery_rejects_changed_preparation_inputs(self):
        source, args = await self.managed_source()
        await prepare_worktree(self.config, self.fs, args)
        with self.assertRaises(AgentError) as error:
            await prepare_worktree(
                self.config, self.fs, args.model_copy(update={"target_branch": "other"})
            )
        self.assertEqual(error.exception.code, "git.worktreeChanged")

    async def test_recovery_reads_completed_records_with_long_unicode_repository_paths(self):
        source, _ = await self.managed_source()
        destination = self.root.joinpath(*["ä" * 100] * 10)
        destination.parent.mkdir(parents=True)
        source.rename(destination)
        args = self.arguments(destination)
        result = await prepare_worktree(self.config, self.fs, args)
        self.assertEqual(
            (await prepare_worktree(self.config, self.fs, args))["path"], result["path"]
        )

    async def test_recovery_rejects_replaced_repository(self):
        source, args = await self.managed_source()
        result = await prepare_worktree(self.config, self.fs, args)
        destination = Path(result["path"])
        destination.rename(self.root / "saved-work")
        self.git("init", "-b", "replacement", str(destination))
        with self.assertRaises(AgentError) as error:
            await prepare_worktree(self.config, self.fs, args)
        self.assertEqual(error.exception.code, "git.worktreeChanged")

    async def test_recovery_does_not_adopt_incomplete_checkout(self):
        source, args = await self.managed_source()
        result = await prepare_worktree(self.config, self.fs, args)
        state_path = (
            self.config.data_dir / "worktree-state" / f"{args.owner_id}-{args.session_id}.json"
        )
        state = json.loads(state_path.read_text())
        state["ready"] = False
        state_path.write_text(json.dumps(state))
        with self.assertRaises(AgentError):
            await prepare_worktree(self.config, self.fs, args)
        self.assertEqual((Path(result["path"]) / "file.txt").read_text(), "old\n")

    async def test_retry_uses_the_frozen_base_after_failed_checkout(self):
        source, args = await self.managed_source()
        base = self.git("rev-parse", "HEAD", cwd=source)
        original = git_module._git

        async def fail_checkout(directory, *arguments, **kwargs):
            if arguments[:2] == ("worktree", "add"):
                return 1, b"", b"checkout failed"
            return await original(directory, *arguments, **kwargs)

        with patch.object(git_module, "_git", fail_checkout), self.assertRaises(AgentError):
            await prepare_worktree(self.config, self.fs, args)
        self.git("commit", "--allow-empty", "-m", "New target", cwd=source)
        result = await prepare_worktree(self.config, self.fs, args)
        self.assertEqual(self.git("rev-parse", "HEAD", cwd=result["path"]), base)

    async def test_recovery_keeps_existing_branch_based_worktrees(self):
        source, args = await self.managed_source()
        result = await prepare_worktree(self.config, self.fs, args)
        branch = f"worktree-{Path(result['path']).name}"
        self.git("checkout", "-b", branch, cwd=result["path"])
        state_path = (
            self.config.data_dir / "worktree-state" / f"{args.owner_id}-{args.session_id}.json"
        )
        state = json.loads(state_path.read_text())
        state["branch"] = branch
        state_path.write_text(json.dumps(state))
        self.assertEqual((await prepare_worktree(self.config, self.fs, args))["branch"], branch)

    async def test_git_timeout_terminates_children_and_finishes_cleanup(self):
        self.git("init")
        pid_file = self.root / "child.pid"
        script = (
            "import subprocess,sys,time; from pathlib import Path; "
            "child=subprocess.Popen([sys.executable,'-c','import time; time.sleep(60)']); "
            f"Path({str(pid_file)!r}).write_text(str(child.pid)); time.sleep(60)"
        )
        command = shlex.join([sys.executable, "-c", script])
        with self.assertRaises(AgentError):
            await asyncio.wait_for(
                git_module._git(
                    str(self.root), "-c", f"alias.probe=!{command}", "probe", timeout_seconds=0.4
                ),
                5,
            )
        status = Path(f"/proc/{pid_file.read_text()}/stat")
        self.assertTrue(not status.exists() or status.read_text().split()[2] == "Z")

    async def test_combines_index_working_tree_new_and_deleted_without_touching_index(self):
        self.git("init")
        (self.root / "edit.txt").write_text("old\n")
        (self.root / "deleted.txt").write_text("gone\n")
        (self.root / "untrack.txt").write_text("keep on disk\n")
        (self.root / ".gitignore").write_text("ignored.txt\n")
        self.git("add", ".")
        self.git("commit", "-m", "Initial")
        self.git("rm", "--cached", "untrack.txt")
        (self.root / "edit.txt").write_text("staged\n")
        self.git("add", "edit.txt")
        (self.root / "edit.txt").write_text("working\nextra\n")
        (self.root / "deleted.txt").unlink()
        (self.root / "new [1].txt").write_text("new\n")
        (self.root / "ignored.txt").write_text("ignored\n")
        index = (self.root / ".git/index").read_bytes()
        result = await changes(self.fs, str(self.root))
        self.assertEqual(
            result["files"],
            [
                {"path": "deleted.txt", "status": "deleted", "additions": 0, "deletions": 1},
                {"path": "edit.txt", "status": "modified", "additions": 2, "deletions": 1},
                {"path": "new [1].txt", "status": "added", "additions": 1, "deletions": 0},
                {"path": "untrack.txt", "status": "deleted", "additions": 0, "deletions": 1},
            ],
        )
        (self.root / "unrelated\nfile").write_text("not selected\n")
        diff = await changes(self.fs, str(self.root), "edit.txt")
        self.assertEqual((diff["before"], diff["after"]), ("old\n", "working\nextra\n"))
        self.assertEqual((self.root / ".git/index").read_bytes(), index)

    async def test_inspection_does_not_execute_clean_or_process_filters(self):
        self.git("init")
        (self.root / "sample.txt").write_text("old\n")
        self.git("add", ".")
        self.git("commit", "-m", "Initial")
        marker = self.root / "filter-executed"
        command = shlex.join(
            [
                sys.executable,
                "-c",
                f"from pathlib import Path; Path({str(marker)!r}).touch(); raise SystemExit(1)",
            ]
        )
        for option in ("clean", "process"):
            self.git("config", f"filter.probe.{option}", command)
        self.git("config", "filter.probe.required", "true")
        (self.root / ".gitattributes").write_text("sample.txt filter=probe\n")
        (self.root / "sample.txt").write_text("new\n")
        await changes(self.fs, str(self.root))
        await changes(self.fs, str(self.root), "sample.txt")
        self.assertTrue((await changes(self.fs, str(self.root), status_only=True))["dirty"])
        for action in ("switch", "rename"):
            with self.assertRaises(AgentError):
                await self.branch(self.root, action, "other")
        config_path = self.root / ".git/config"
        late_config = config_path.read_text().replace('[filter "probe"]', '[filter "late"]')
        inspect_filters = git_module._disabled_filters
        writer = None

        def introduce_filter():
            self.fs.upsert(str(config_path), late_config)
            self.fs.upsert(str(self.root / ".gitattributes"), "sample.txt filter=late\n")

        async def snapshot_then_write(directory):
            nonlocal writer
            config = await inspect_filters(directory)
            writer = asyncio.create_task(asyncio.to_thread(introduce_filter))
            await asyncio.wait({writer}, timeout=0.1)
            return config

        with patch.object(git_module, "_disabled_filters", snapshot_then_write):
            try:
                await changes(self.fs, str(self.root))
            finally:
                if writer:
                    await writer
        self.assertFalse(marker.exists())

    @unittest.skipUnless(os.geteuid() == 0, "Requires ownership changes")
    async def test_unsafe_repository_is_an_access_error_not_a_missing_repository(self):
        self.git("init")
        os.chown(self.root, 1000, -1)
        # Ignore sudo ownership exemptions and runner-wide safe.directory entries.
        with (
            patch.dict(os.environ, {"SUDO_UID": "0"}),
            patch.object(
                git_module, "_git", partial(git_module._git, config={"safe.directory": ""})
            ),
            self.assertRaises(AgentError) as error,
        ):
            await changes(self.fs, str(self.root))
        self.assertEqual(error.exception.code, "path.accessDenied")

    async def test_worktree_is_detected_from_subdirectory_and_isolated(self):
        self.git("init")
        (self.root / "file.txt").write_text("base\n")
        self.git("add", ".")
        self.git("commit", "-m", "Initial")
        worktree = self.root / "worktree"
        self.git("worktree", "add", "-b", "other", str(worktree))
        (self.root / "file.txt").write_text("main\n")
        (worktree / "file.txt").write_text("other\n")
        (worktree / "subdir").mkdir()
        diff = await changes(self.fs, str(worktree / "subdir"), "file.txt")
        self.assertEqual((diff["repository"], diff["after"]), (str(worktree), "other\n"))

    async def test_preview_limits_and_symlinks_do_not_expose_external_contents(self):
        self.git("init")
        before = "a" * (256 * 1024)
        after = "b" * (256 * 1024)
        (self.root / "boundary.txt").write_text(before)
        self.git("add", "boundary.txt")
        self.git("commit", "-m", "Initial")
        (self.root / "boundary.txt").write_text(after)
        result = await changes(self.fs, str(self.root), "boundary.txt")
        self.assertEqual(
            (result["reason"], result["before"], result["after"]), (None, before, after)
        )
        (self.root / "large.txt").write_bytes(b"a" * (MAX_TEXT_BYTES + 1))
        (self.root / "escaped.txt").write_bytes(b"\x01" * MAX_TEXT_BYTES)
        (self.root / "binary").write_bytes(b"a\0b")
        (self.root / "link").symlink_to("/etc/passwd")
        for file, reason in [
            ("large.txt", "tooLarge"),
            ("escaped.txt", "tooLarge"),
            ("binary", "binary"),
            ("link", "unsupported"),
        ]:
            with self.subTest(file=file):
                result = await changes(self.fs, str(self.root), file)
                self.assertEqual(
                    (result["reason"], result["before"], result["after"]), (reason, "", "")
                )
