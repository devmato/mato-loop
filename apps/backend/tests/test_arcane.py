import json
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import httpx
import pytest
from aiohttp import web
from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncSession

import mato_loop_backend.integrations.arcane.adapter as arcane_module
import mato_loop_backend.integrations.arcane.client as arcane_client
from mato_loop_backend.agent.run_support import _tool_policy
from mato_loop_backend.integrations.arcane.adapter import (
    ArcaneAction,
    ArcaneDelete,
    ArcaneGet,
    ArcaneList,
    ArcaneProjectConfig,
    ArcaneProjectSave,
    _binding,
    _delete_permission,
    _environments,
    _public,
    arcane_action,
    arcane_list,
    arcane_project_config,
    arcane_project_save,
)
from mato_loop_backend.integrations.arcane.client import ArcaneClient, ArcaneError, segment
from mato_loop_backend.integrations.arcane.project_config import read_snapshot
from mato_loop_backend.permissions.policy import PermissionDecision
from mato_loop_backend.privacy import RequestPseudonymizer
from mato_loop_backend.security import CredentialCipher
from mato_loop_backend.settings import (
    IntegrationInstance,
    IntegrationSettings,
    ToolExecutionPolicy,
    ToolSettings,
    TrustLevel,
)
from mato_loop_backend.settings.effective import apply_integration_permissions
from mato_loop_backend.settings.models import IntegrationPermissions, sysadmin_profile
from mato_loop_backend.tools.catalog import tool_registry


@pytest.mark.asyncio
async def test_arcane_nullable_list_data_is_empty_but_missing_data_is_invalid(monkeypatch):
    async with httpx.AsyncClient(transport=httpx.MockTransport(
        lambda _: httpx.Response(200, json={"success": True, "data": None, "pagination": {"total": 0}})
    )) as transport:
        async with ArcaneClient("https://arcane.example.test", "test-key", client=transport) as client:
            assert await _environments(client) == []

            @asynccontextmanager
            async def selected_client(_args, _context):
                yield client

            monkeypatch.setattr(arcane_module, "_client", selected_client)
            result = await arcane_list(
                ArcaneList(integration_id=uuid4(), environment_id="0", kind="containers"),
                SimpleNamespace(),
            )
            assert json.loads(result.content)["data"]["items"] == []

    async with httpx.AsyncClient(transport=httpx.MockTransport(
        lambda _: httpx.Response(200, json={"success": True, "pagination": {"total": 0}})
    )) as transport:
        async with ArcaneClient("https://arcane.example.test", "test-key", client=transport) as client:
            with pytest.raises(ArcaneError, match="integration.invalidResponse"):
                await client.request("GET", "/environments")


@pytest.mark.asyncio
async def test_arcane_request_limits_origin_and_requires_stream_completion():
    requests = []

    def handler(request):
        requests.append(request)
        if request.url.path.endswith("/up"):
            return httpx.Response(200, content=b'{"activityId":"run-1"}\n{"log":"secret"}\n{"done":true}\n')
        return httpx.Response(200, json={"success": True, "data": [{"id": "0"}], "pagination": {"total": 1}})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as transport:
        async with ArcaneClient("https://arcane.example.test/panel", "test-key", client=transport) as client:
            data, _ = await client.request("GET", "/environments", params={"limit": 1})
            assert data == [{"id": "0"}]
            assert await client.request("POST", "/environments/0/projects/x/up", streamed=True) == {
                "completed": True, "activityId": "run-1"
            }
        async with ArcaneClient("https://arcane.example.test", "test-key", client=transport) as client:
            await client.request("GET", "/environments")
    assert requests[0].url.path == "/panel/api/environments"
    assert requests[0].headers["X-Api-Key"] == "test-key"
    assert requests[1].url.path == "/panel/api/environments/0/projects/x/up"
    assert requests[2].url.path == "/api/environments"

    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda _: httpx.Response(200, content=b'{"log":"working"}\n'))) as transport:
        async with ArcaneClient("https://arcane.example.test", "test-key", client=transport) as client:
            with pytest.raises(ArcaneError, match="integration.outcomeUnknown"):
                await client.request("POST", "/environments/0/projects/x/up", streamed=True)


def test_arcane_actions_and_permissions_are_target_bound():
    instance = IntegrationInstance(name="Host", integration_type="infrastructure", adapter_type="arcane",
                                   config={"base_url": "https://arcane.example.test"}, credential_id=uuid4())
    revision = json.dumps(instance.binding_data(), ensure_ascii=True, separators=(",", ":"), sort_keys=True)
    context = SimpleNamespace(bound_options={"integration_id": str(instance.id), "binding_revision": revision},
                              integration_settings=IntegrationSettings(instances=[instance]))
    arguments = ArcaneDelete(integration_id=instance.id, environment_id="0", kind="volumes", resource_id="db/data")
    permission = _delete_permission(arguments, context)[0]
    assert permission.capability == "arcane.delete"
    assert permission.policy_resource_key == "0"
    assert "db/data" in permission.summary
    assert segment("db/data") == "db%2Fdata"
    context.tool_settings = ToolSettings(resource_labels={str(instance.id): {
        arcane_module._label_key(arguments, revision, "volumes", "db/data"): "Database",
    }})
    named = _delete_permission(arguments, context)[0]
    assert (named.resource_scope, named.session_grants[0].resource_scope) == (
        permission.resource_scope, permission.session_grants[0].resource_scope,
    )
    deploy = ArcaneAction(integration_id=instance.id, environment_id="0", action="up", resource_id="project")
    assert [request.capability for request in arcane_module._manage_permission(deploy, context)] == ["arcane.manage", "arcane.read"]
    context.bound_options["binding_revision"] = "stale"
    with pytest.raises(ValueError):
        _binding(context, instance.id)
    with pytest.raises(ValidationError):
        ArcaneAction(integration_id=instance.id, environment_id="0", action="up", name="other")
    with pytest.raises(ValidationError):
        ArcaneList(integration_id=instance.id, environment_id="../other", kind="containers")
    with pytest.raises(ValidationError):
        ArcaneGet(integration_id=instance.id, environment_id="0", kind="containers", resource_id="x", view="updates")
    with pytest.raises(ValidationError):
        ArcaneProjectSave(integration_id=instance.id, environment_id="0", project_id="x", compose_content="services: {}")


def test_arcane_does_not_release_remote_secrets():
    assert _public({"id": "x", "name": "app", "config": {"env": ["KEY=secret"]},
                    "apiKey": "secret", "mounts": [{"source": "/sensitive"}]}, "containers") == {
        "id": "x", "name": "app"
    }


@pytest.mark.asyncio
async def test_arcane_permissions_display_observed_environment_and_project_names(monkeypatch):
    instance = IntegrationInstance(name="Host", integration_type="infrastructure", adapter_type="arcane",
                                   config={"base_url": "https://arcane.example.test"})
    revision = json.dumps(instance.binding_data(), ensure_ascii=True, separators=(",", ":"), sort_keys=True)
    context = SimpleNamespace(db=AsyncMock(spec=AsyncSession), tool_settings=ToolSettings(),
        integration_settings=IntegrationSettings(instances=[instance]),
        bound_options={"integration_id": str(instance.id), "binding_revision": revision})
    context.db.scalar.return_value = None
    remote = AsyncMock(side_effect=[([{"id": "0", "name": "Docker host"}], None),
                                   ([{"id": "project-id", "name": "searxng"}], None)])

    @asynccontextmanager
    async def client(_args, _context):
        yield SimpleNamespace(request=remote)

    monkeypatch.setattr(arcane_module, "_client", client)
    await arcane_module.arcane_environments(arcane_module.ArcaneInstance(integration_id=instance.id), context)
    # The database returns the labels persisted by the preceding authorized call.
    context.db.scalar.return_value = context.db.add.call_args.args[0]
    await arcane_list(ArcaneList(integration_id=instance.id, environment_id="0", kind="projects"), context)
    request = arcane_module._project_config_permission(
        ArcaneProjectConfig(integration_id=instance.id, environment_id="0", project_id="project-id"), context,
    )[0]
    assert (request.resource_display, request.display_items[0].value) == (
        'Arcane "Host" · Docker host (0)', "searxng (project-id)",
    )


@pytest.mark.asyncio
async def test_arcane_actions_use_only_named_routes_and_explicit_fields(monkeypatch):
    requests = []

    async def request(method, path, *, body=None, streamed=False):
        requests.append((method, path, body, streamed))
        return ({"completed": True, "activityId": "a"} if streamed else ({"activityId": "a"}, None))

    @asynccontextmanager
    async def selected_client(_args, _context):
        yield SimpleNamespace(request=request)

    monkeypatch.setattr(arcane_module, "_client", selected_client)
    identifier = uuid4()
    context = SimpleNamespace()
    for arguments in (
        ArcaneAction(integration_id=identifier, environment_id="0", action="create_container", name="app", image="nginx:stable"),
        ArcaneAction(integration_id=identifier, environment_id="0", action="auto_update", resource_id="abc", enabled=False),
        ArcaneAction(integration_id=identifier, environment_id="0", action="build", resource_id="proj"),
    ):
        assert not (await arcane_action(arguments, context)).is_error
    assert requests == [
        ("POST", "/environments/0/containers", {"name": "app", "image": "nginx:stable"}, False),
        ("PUT", "/environments/0/containers/abc/auto-update", {"enabled": False}, False),
        ("POST", "/environments/0/projects/proj/build", None, True),
    ]


@pytest.mark.asyncio
async def test_arcane_refuses_redirects_and_unbounded_read_responses():
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda _: httpx.Response(302, headers={"Location": "https://other.test"}))) as transport:
        async with ArcaneClient("https://arcane.example.test", "key", client=transport) as client:
            with pytest.raises(ArcaneError, match="integration.unavailable"):
                await client.request("GET", "/environments")
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda _: httpx.Response(200, content=b"x" * (256 * 1024 + 1)))) as transport:
        async with ArcaneClient("https://arcane.example.test", "key", client=transport) as client:
            with pytest.raises(ArcaneError, match="integration.responseTooLarge"):
                await client.request("GET", "/environments")


@pytest.mark.parametrize("trust", list(TrustLevel))
@pytest.mark.parametrize("preset, expected", [
    ("ask_all", ("ask", "ask", "ask", "ask")),
    ("balanced", ("ask", "ask", "ask", "ask")),
    ("ask_changes", ("auto", "auto", "ask", "ask")),
    ("ask_delete", ("auto", "auto", "auto", "ask")),
    ("auto_all", ("auto", "auto", "auto", "auto")),
])
def test_arcane_actions_inherit_selected_preset(trust, preset, expected):
    settings = ToolSettings(private_permission_preset=preset, public_permission_preset=preset).select_trust(trust)
    assert tuple(action.default_policy(settings) for action in (
        arcane_module._ACCOUNTS, *arcane_module.ARCANE_ADAPTER.resource_actions,
    )) == expected


@pytest.mark.parametrize("trust", list(TrustLevel))
def test_sysadmin_arcane_uses_existing_profile_selection_and_resource_rules(trust):
    profile = sysadmin_profile()
    tools = ToolSettings(private_permission_preset="ask_all", public_permission_preset="ask_all").select_trust(trust)
    tools._agent_profile = profile
    tools._agent_permissions = getattr(profile, f"{trust.value}_permissions")
    instance = IntegrationInstance(name="Host", integration_type="infrastructure", adapter_type="arcane",
                                   config={"base_url": "https://arcane.example.test"}, credential_id=uuid4())
    integrations = IntegrationSettings(instances=[instance])
    initially_offered = "arcane_project_config" in tool_registry.effective(integrations, tool_settings=tools).tools
    instance.profile_permissions[profile.id] = IntegrationPermissions(available=True)
    apply_integration_permissions(tools, integrations, profile.id)
    selected_offered = "arcane_project_config" in tool_registry.effective(integrations, tool_settings=tools).tools
    context = SimpleNamespace(integration_settings=integrations, bound_options={
        "integration_id": str(instance.id),
        "binding_revision": json.dumps(instance.binding_data(), ensure_ascii=True, separators=(",", ":"), sort_keys=True),
    })
    request = arcane_module._project_config_permission(
        ArcaneProjectConfig(integration_id=instance.id, environment_id="0", project_id="app"), context,
    )[0]
    inherited = _tool_policy(tools, integrations, request)
    branch = getattr(instance.profile_permissions[profile.id], f"{trust.value}_permissions")
    branch.policies["arcane.read"] = ToolExecutionPolicy.AUTO
    apply_integration_permissions(tools, integrations, profile.id)
    overridden = _tool_policy(tools, integrations, request)
    instance.profile_permissions[profile.id].resource_availability["0"] = False
    apply_integration_permissions(tools, integrations, profile.id)
    disabled = _tool_policy(tools, integrations, request)
    assert (initially_offered, selected_offered, inherited, overridden, disabled) == (False, True, "ask", "auto", "disabled")


async def test_arcane_project_configuration_round_trip_and_update_guards(monkeypatch, tmp_path):
    import mato_loop_backend.integrations.arcane.project_config as config_module

    cipher = CredentialCipher(tmp_path / "key")
    monkeypatch.setattr(config_module, "CredentialCipher", lambda: cipher)
    instance = IntegrationInstance(name="Host", integration_type="infrastructure", adapter_type="arcane",
                                   config={"base_url": "https://arcane.example.test"})
    revision = json.dumps(instance.binding_data(), ensure_ascii=True, separators=(",", ":"), sort_keys=True)
    context = SimpleNamespace(session=SimpleNamespace(id=uuid4()), integration_settings=IntegrationSettings(instances=[instance]), one_time_secrets={},
                              db=AsyncMock(spec=AsyncSession), tool_settings=ToolSettings(),
                              bound_options={"integration_id": str(instance.id), "binding_revision": revision},
                              ask_handler=AsyncMock(return_value=PermissionDecision.ALLOW))
    context.db.scalar.return_value = None
    context.current_integrations = AsyncMock(return_value=context.integration_settings)
    config = {"id": "project", "name": "app", "composeContent": "services:\n  app:\n    image: nginx:stable\n    environment:\n      PASSWORD: compose-secret\n      SESSION: YWI=\n",
              "envContent": "PASSWORD=supersecret\nPORT=8080\n",
              "overrideContent": "", "includeFiles": []}
    writes = []

    def handle(request):
        if request.method != "GET":
            writes.append(request)
        data = config if request.method == "POST" else {**config, "services": [{"environment": {"INHERITED": "x" * 300_000}}]}
        return httpx.Response(200, json={"success": True, "data": data})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as transport:
        async with ArcaneClient("https://arcane.example.test", "key", client=transport) as client:
            @asynccontextmanager
            async def selected_client(_args, _context):
                yield client

            monkeypatch.setattr(arcane_module, "_client", selected_client)
            scope = {"integration_id": instance.id, "environment_id": "0"}
            created = await arcane_project_save(ArcaneProjectSave(**scope, name="app", compose_content="services: {}"), context)
            assert json.loads(created.content)["data"]["project"]["id"] == "project"
            assert writes[0].headers["content-type"].startswith("multipart/form-data;")
            assert b'"fileChanges": []' in writes[0].content
            secret_id = uuid4()
            context.one_time_secrets[secret_id] = "new-secret"
            protected = ArcaneProjectSave(**scope, name="protected", compose_content="services: {}",
                                          env_content={"parts": ["PASSWORD=", {"value_ref": f"run-secret:{secret_id}"}]})
            assert not (await arcane_project_save(protected, context)).is_error
            assert b"PASSWORD=new-secret" in writes[-1].content
            result = await arcane_project_config(ArcaneProjectConfig(**scope, project_id="project"), context)
            visible = json.loads(result.content)["data"]
            assert visible["envContent"] == config["envContent"]
            snapshot = cipher.decrypt(visible["snapshotToken"].encode("ascii"))
            assert set(snapshot) == {"version", "integrationId", "environmentId", "projectId", "sessionId", "bindingRevision", "configurationDigest"}
            pseudonymizer = RequestPseudonymizer()
            messages = pseudonymizer.protect_messages([{"role": "tool", "content": result.content}])
            assert "supersecret" not in messages[-1]["content"]
            pseudonymizer.retain_transmitted({"messages": messages})
            model_config = json.loads(messages[-1]["content"])["data"]
            save = ArcaneProjectSave(**pseudonymizer.restore_value({
                **scope, "project_id": "project", "snapshot_token": model_config["snapshotToken"],
                "compose_content": model_config["composeContent"].replace("nginx:stable", "nginx:alpine"),
                "env_content": model_config["envContent"],
            }))
            preview = arcane_module._project_save_permission(save, context)[0].display_items[1].value
            assert preview == save.compose_content
            assert next(request for request in arcane_module._project_save_permission(save, context)
                        if request.capability == "arcane.read").sensitive_access
            assert not (await arcane_project_save(save, context)).is_error
            assert json.loads(writes[-1].content) == {"composeContent": config["composeContent"].replace("nginx:stable", "nginx:alpine"), "envContent": config["envContent"]}
            oversized = await arcane_project_save(ArcaneProjectSave(**scope, name="oversized", compose_content="services: {}",
                                                                   env_content=("#" + "😀" * 1_000 + "\n") * 70), context)
            assert oversized.is_error and "tool.invalidArguments" in oversized.content and len(writes) == 3
            context.ask_handler.side_effect = lambda request, **_kwargs: (
                PermissionDecision.DENY if request.capability == "arcane.delete" else PermissionDecision.ALLOW
            )
            cleared = await arcane_project_save(ArcaneProjectSave(**scope, project_id="project",
                snapshot_token=visible["snapshotToken"], env_content=""), context)
            assert cleared.is_error and "permission.denied" in cleared.content and len(writes) == 3
            context.one_time_secrets[secret_id] = ""
            hidden_clear = await arcane_project_save(ArcaneProjectSave(**scope, project_id="project",
                snapshot_token=visible["snapshotToken"], env_content={"parts": [{"value_ref": f"run-secret:{secret_id}"}]}), context)
            assert hidden_clear.is_error and "tool.invalidArguments" in hidden_clear.content and len(writes) == 3
            context.ask_handler.side_effect = None
            config["envContent"] += "EXTERNAL=change\n"
            changed = await arcane_project_save(save, context)
            assert changed.is_error and "integration.changed" in changed.content and len(writes) == 3
            binding = arcane_module._project_snapshot_context(save, context, "project")
            with pytest.raises(ArcaneError, match="integration.staleReference"):
                read_snapshot(visible["snapshotToken"], **{**binding, "session_id": str(uuid4())})


@pytest.mark.parametrize("read_fails", [False, True])
async def test_arcane_deploy_reports_runtime_without_retrying_confirmed_mutations(monkeypatch, read_fails):
    calls = []

    async def request(method, path, **_kwargs):
        calls.append((method, path))
        if method == "POST":
            return {"completed": True, "activityId": "deploy"}
        if read_fails:
            raise ArcaneError("integration.unavailable")
        return {"runtimeServices": [{"name": "app", "status": "running", "health": "starting", "serviceConfig": {"secret": "private"}}]}, None

    @asynccontextmanager
    async def selected_client(_args, _context):
        yield SimpleNamespace(request=request)

    monkeypatch.setattr(arcane_module, "_client", selected_client)
    result = await arcane_action(ArcaneAction(integration_id=uuid4(), environment_id="0", action="up", resource_id="project"), SimpleNamespace())
    data = json.loads(result.content)["data"]
    assert not result.is_error and data["completed"] is True and calls == [
        ("POST", "/environments/0/projects/project/up"), ("GET", "/environments/0/projects/project/runtime"),
    ]
    assert data.get("runtimeError") == ("integration.unavailable" if read_fails else None)
    if not read_fails:
        assert data["runtime"] == {"runtimeServices": [{"name": "app", "status": "running", "health": "starting"}]}


@pytest.mark.parametrize("mode", ["large", "redirect", "deadline", "oversized"])
async def test_arcane_log_snapshot_uses_server_tail_and_never_follows_redirects(mode, monkeypatch):
    requests = []
    if mode == "deadline":
        monkeypatch.setattr(arcane_client, "LOG_CAPTURE_SECONDS", 0.05)

    async def logs(request):
        requests.append(request.path)
        assert request.headers["X-Api-Key"] == "key"
        if mode == "redirect":
            raise web.HTTPFound("/other")
        assert dict(request.query) == {"tail": "200", "follow": "false", "format": "json", "timestamps": "true"}
        socket = web.WebSocketResponse()
        await socket.prepare(request)
        if mode in {"large", "oversized"}:
            await socket.send_json({"message": "old " + "x" * (70_000 if mode == "large" else 270_000)})
        if mode != "oversized":
            await socket.send_json({"message": "newest"})
        if mode == "deadline":
            await socket.receive()
        await socket.close()
        return socket

    app = web.Application()
    app.router.add_get("/panel/api/environments/0/ws/containers/app/logs", logs)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    try:
        async with ArcaneClient(f"http://127.0.0.1:{port}/panel", "key") as client:
            if mode == "redirect":
                with pytest.raises(ArcaneError, match="integration.unavailable"):
                    await client.logs("0", "app", 200)
            else:
                result = await client.logs("0", "app", 200)
                assert result == {"logs": "" if mode == "oversized" else "newest\n", "truncated": mode != "deadline", "captureComplete": mode == "large"}
        assert requests == ["/panel/api/environments/0/ws/containers/app/logs"]
    finally:
        await runner.cleanup()
