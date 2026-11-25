import base64
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest
from pydantic import ValidationError
from pywebpush import WebPushException
from sqlalchemy.ext.asyncio import AsyncSession

from mato_loop_backend import web_push
from mato_loop_backend.config import RuntimeConfig
from mato_loop_backend.models import WebPushSubscription
from mato_loop_backend.web_push import (
    EffectiveWebPushSettings,
    VapidKeyStore,
    WebPushNotificationPayload,
    WebPushPayload,
    WebPushRegistrationError,
    WebPushService,
    WebPushSessionTarget,
    WebPushSubscriptionInput,
    _NoRedirectSession,
)


def _encoded(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode()


def _subscription(endpoint: str = "https://push.example.test/send/secret"):
    return WebPushSubscriptionInput(
        endpoint=endpoint,
        keys={
            "p256dh": _encoded(b"\x04" + b"p" * 64),
            "auth": _encoded(b"a" * 16),
        },
    )


def _config(tmp_path: Path, *, enabled: bool = True) -> RuntimeConfig:
    return RuntimeConfig(
        data_dir=tmp_path,
        web_push_enabled=enabled,
        web_push_vapid_subject="mailto:admin@example.test",
        web_push_allowed_origins="https://push.example.test",
    )


def _effective() -> EffectiveWebPushSettings:
    return EffectiveWebPushSettings(
        enabled=True,
        subject="mailto:admin@example.test",
        allowed_origins=frozenset({"https://push.example.test"}),
    )


def test_apple_push_subdomains_are_allowed_without_arbitrary_ports() -> None:
    config = EffectiveWebPushSettings(
        enabled=True,
        subject="mailto:admin@example.test",
        allowed_origins=frozenset({"https://web.push.apple.com"}),
        allow_apple_subdomains=True,
    )

    assert web_push._endpoint_allowed("https://example.push.apple.com/path", config)
    assert not web_push._endpoint_allowed("https://example.push.apple.com:8443/path", config)
    assert not web_push._endpoint_allowed("https://push.apple.com.example.test/path", config)


def test_vapid_key_is_created_explicitly_and_remains_stable(tmp_path: Path) -> None:
    store = VapidKeyStore(_config(tmp_path, enabled=False))

    assert store.public_key() is None
    assert not store.path.exists()

    first = store.ensure_key()
    second = VapidKeyStore(_config(tmp_path)).public_key()

    assert first == second
    assert first is not None
    assert len(base64.urlsafe_b64decode(first + "=" * (-len(first) % 4))) == 65
    assert store.path.stat().st_mode & 0o777 == 0o600


def test_invalid_vapid_key_is_rejected(tmp_path: Path) -> None:
    store = VapidKeyStore(_config(tmp_path))
    store.path.parent.mkdir(parents=True)
    store.path.write_text("not a key")

    with pytest.raises(web_push.WebPushConfigurationError):
        store.public_key()


def test_subscription_model_has_owner_cascade_and_required_uniqueness() -> None:
    user_key = next(iter(WebPushSubscription.__table__.c.user_id.foreign_keys))

    assert user_key.ondelete == "CASCADE"
    assert WebPushSubscription.__table__.c.endpoint_hash.unique is True
    assert any(
        constraint.name == "uq_web_push_subscriptions_user_device"
        for constraint in WebPushSubscription.__table__.constraints
    )


def test_subscription_input_strictly_validates_https_endpoint_and_key_material() -> None:
    with pytest.raises(ValidationError):
        _subscription("http://push.example.test/send/secret")
    with pytest.raises(ValidationError):
        WebPushSubscriptionInput(
            endpoint="https://push.example.test/send/secret",
            keys={"p256dh": _encoded(b"short"), "auth": _encoded(b"a" * 16)},
        )


async def test_registration_encrypts_secrets_and_sets_owner(monkeypatch, tmp_path: Path) -> None:
    config = _config(tmp_path)
    captured = {}

    class Cipher:
        def encrypt(self, payload):
            captured.update(payload)
            return b"encrypted"

    monkeypatch.setattr(web_push, "get_runtime_config", lambda: config)
    monkeypatch.setattr(web_push, "load_effective_web_push_settings", AsyncMock(return_value=_effective()))
    monkeypatch.setattr(VapidKeyStore, "public_key", lambda _self: "public-key")
    monkeypatch.setattr(web_push, "CredentialCipher", Cipher)
    db = AsyncMock(spec=AsyncSession)
    db.scalar.return_value = None
    user_id = uuid4()
    device_id = uuid4()

    await WebPushService.register(
        db,
        user_id=user_id,
        device_id=device_id,
        subscription=_subscription(),
    )

    item = db.add.call_args.args[0]
    assert item.user_id == user_id
    assert item.device_id == device_id
    assert item.encrypted_payload == b"encrypted"
    assert captured == _subscription().secret_payload()
    assert b"push.example.test" not in item.encrypted_payload
    db.flush.assert_awaited_once()


async def test_registration_transfers_existing_endpoint_to_current_owner(
    monkeypatch, tmp_path: Path
) -> None:
    config = _config(tmp_path)
    endpoint_item = WebPushSubscription(
        id=uuid4(),
        user_id=uuid4(),
        device_id=uuid4(),
        endpoint_hash="a" * 64,
        encrypted_payload=b"old",
    )
    monkeypatch.setattr(web_push, "get_runtime_config", lambda: config)
    monkeypatch.setattr(web_push, "load_effective_web_push_settings", AsyncMock(return_value=_effective()))
    monkeypatch.setattr(VapidKeyStore, "public_key", lambda _self: "public-key")
    monkeypatch.setattr(
        web_push,
        "CredentialCipher",
        lambda: type("Cipher", (), {"encrypt": lambda _self, _payload: b"encrypted"})(),
    )
    db = AsyncMock(spec=AsyncSession)
    db.scalar.side_effect = [endpoint_item, None]
    user_id = uuid4()
    device_id = uuid4()

    await WebPushService.register(
        db,
        user_id=user_id,
        device_id=device_id,
        subscription=_subscription(),
    )

    db.delete.assert_awaited_once_with(endpoint_item)
    replacement = db.add.call_args.args[0]
    assert replacement.user_id == user_id
    assert replacement.device_id == device_id
    assert db.flush.await_count == 2


async def test_registration_rejects_endpoint_outside_exact_allowlist(
    monkeypatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(web_push, "load_effective_web_push_settings", AsyncMock(return_value=_effective()))
    monkeypatch.setattr(VapidKeyStore, "public_key", lambda _self: "public-key")

    with pytest.raises(WebPushRegistrationError):
        await WebPushService.register(
            AsyncMock(spec=AsyncSession),
            user_id=uuid4(),
            device_id=uuid4(),
            subscription=_subscription("https://sub.push.example.test/send/secret"),
        )


async def test_subscription_delete_is_owner_and_device_scoped() -> None:
    db = AsyncMock(spec=AsyncSession)
    user_id = uuid4()

    await WebPushService.delete(db, user_id=user_id, device_id=uuid4())

    statement = db.execute.await_args.args[0]
    sql = str(statement)
    assert "web_push_subscriptions.user_id =" in sql
    assert "web_push_subscriptions.device_id =" in sql


async def test_prepare_delivery_only_loads_active_subscriptions_for_owner(monkeypatch) -> None:
    user_id = uuid4()
    db = AsyncMock(spec=AsyncSession)
    db.__aenter__.return_value = db
    db.scalars.return_value = MagicMock(all=MagicMock(return_value=[]))
    monkeypatch.setattr(web_push, "AsyncSessionFactory", lambda: db)
    monkeypatch.setattr(web_push, "load_effective_web_push_settings", AsyncMock(return_value=_effective()))
    monkeypatch.setattr(VapidKeyStore, "public_key", lambda _self: "public-key")

    assert await WebPushService.prepare_delivery(user_id) is None
    statement = db.scalars.await_args.args[0]
    assert user_id in statement.compile().params.values()
    assert "user_accounts.is_active IS true" in str(statement)


@pytest.mark.parametrize("status_code, stale", [(410, True), (503, False)])
async def test_delivery_removes_only_stale_subscriptions(
    monkeypatch, tmp_path: Path, status_code: int, stale: bool
) -> None:
    item = WebPushSubscription(
        id=uuid4(),
        user_id=uuid4(),
        device_id=uuid4(),
        endpoint_hash="a" * 64,
        encrypted_payload=b"encrypted",
    )
    cipher = type("Cipher", (), {"decrypt": lambda _self, _value: _subscription().secret_payload()})()
    monkeypatch.setattr(
        web_push,
        "webpush_async",
        AsyncMock(
            side_effect=WebPushException(
                "controlled failure", response=type("Response", (), {"status": status_code})()
            )
        ),
    )
    monkeypatch.setattr(WebPushService, "_delete_stale", AsyncMock())

    await WebPushService._deliver_one(
        item,
        WebPushPayload(
            notification=WebPushNotificationPayload(title="Fertig"),
        ),
        _effective(),
        cipher,
    )

    if stale:
        WebPushService._delete_stale.assert_awaited_once_with(item.id, item.endpoint_hash)
    else:
        WebPushService._delete_stale.assert_not_awaited()


async def test_web_push_http_adapter_disables_redirects() -> None:
    session = type("Session", (), {"post": AsyncMock(return_value=object())})()

    await _NoRedirectSession(session).post("https://push.example.test/send")

    assert session.post.await_args.kwargs["allow_redirects"] is False


async def test_stale_cleanup_is_guarded_by_endpoint_hash(monkeypatch) -> None:
    db = AsyncMock(spec=AsyncSession)
    context = MagicMock()
    context.__aenter__ = AsyncMock(return_value=db)
    context.__aexit__ = AsyncMock(return_value=None)
    monkeypatch.setattr(web_push, "AsyncSessionFactory", lambda: context)

    await WebPushService._delete_stale(uuid4(), "a" * 64)

    sql = str(db.execute.await_args.args[0])
    assert "web_push_subscriptions.id =" in sql
    assert "web_push_subscriptions.endpoint_hash =" in sql
    db.commit.assert_awaited_once()


def test_payload_version_is_stable_and_rejects_arbitrary_urls() -> None:
    payload = WebPushPayload(
        notification=WebPushNotificationPayload(
            title="Fertig",
            body="Die Aufgabe ist abgeschlossen.",
            tag="automation",
            target=WebPushSessionTarget(type="session", sessionId=uuid4()),
        )
    )

    assert payload.model_dump(mode="json")["version"] == 1
    with pytest.raises(ValidationError):
        WebPushPayload.model_validate(
            {"version": 1, "notification": {"title": "Fertig", "url": "/sessions/1"}}
        )
