from contextlib import aclosing, asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, call
from uuid import UUID, uuid4

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import ForeignKey, create_engine, delete, update
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from sqlalchemy.pool import NullPool

from mato_loop_backend.api.routes.events import router
from mato_loop_backend.change_events import ChangeEventBroker
from mato_loop_backend.config import get_runtime_config
from mato_loop_backend.db import changes
from mato_loop_backend.db.session import get_db_session
from mato_loop_backend.security import require_user


class Base(DeclarativeBase):
    pass


class Owner(Base):
    __tablename__ = "owners"
    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    user_id: Mapped[UUID]


class Child(Base):
    __tablename__ = "children"
    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    owner_id: Mapped[UUID] = mapped_column(ForeignKey("owners.id"))


@pytest.fixture
def db(monkeypatch):
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    monkeypatch.setattr(changes, "_sources", lambda: {
        Owner: ("projects", Owner, "id"), Child: ("messages", Owner, "owner_id"),
    })
    with changes.ChangeTrackingSession(engine, expire_on_commit=False) as session:
        yield session
    engine.dispose()


async def test_changes_are_coalesced_and_isolated_by_owner():
    broker = ChangeEventBroker()
    alice, bob, session_id = uuid4(), uuid4(), uuid4()
    async with aclosing(broker.subscribe(alice)) as first, aclosing(broker.subscribe(bob)) as second:
        assert await anext(first) == await anext(second) == {("all", None)}
        broker.publish(alice, {("sessions", session_id)})
        broker.publish(alice, {("permissions", session_id)})
        broker.publish(bob, {("notifications", None)})
        assert (await anext(first), await anext(second)) == (
            {("sessions", session_id), ("permissions", session_id)}, {("notifications", None)},
        )
    assert not broker._subscribers


def test_flush_and_rollback_do_not_publish_uncommitted_changes(db, monkeypatch):
    publish = Mock()
    monkeypatch.setattr(changes.change_events, "publish", publish)
    user_id = uuid4()
    db.add(Owner(user_id=user_id))
    db.flush()
    publish.assert_not_called()
    db.rollback()
    db.commit()
    publish.assert_not_called()
    db.add(Owner(user_id=user_id))
    db.commit()
    publish.assert_called_once_with(user_id, {("projects", None)})


@pytest.mark.parametrize("operation", [update, delete])
def test_bulk_writes_resolve_the_affected_owner(db, monkeypatch, operation):
    owner = Owner(user_id=uuid4())
    db.add(owner)
    db.flush()
    child = Child(owner_id=owner.id)
    db.add(child)
    db.commit()
    publish = Mock()
    monkeypatch.setattr(changes.change_events, "publish", publish)
    statement = operation(Child).where(Child.id == child.id)
    if operation is update:
        statement = statement.values(owner_id=owner.id)
    db.execute(statement)
    publish.assert_not_called()
    db.commit()
    publish.assert_called_once_with(owner.user_id, {("messages", None)})


async def test_stream_requires_authentication_and_subscribes_only_to_the_current_owner(monkeypatch):
    app = FastAPI()
    app.include_router(router)
    db = AsyncMock()

    async def get_db():
        yield db

    app.dependency_overrides[get_db_session] = get_db
    user_id = uuid4()
    subscribe = Mock()

    async def stream(owner_id):
        subscribe(owner_id)
        yield {("all", None)}

    monkeypatch.setattr("mato_loop_backend.api.routes.events.change_events.subscribe", stream)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://test") as client:
        assert (await client.get("/events")).status_code == 401
        app.dependency_overrides[require_user] = lambda: SimpleNamespace(id=user_id)
        response = await client.get("/events")
    subscribe.assert_called_once_with(user_id)
    assert response.status_code == 200
    assert '"resource": "all"' in response.text


@pytest.mark.parametrize("role", ["user", "tool"])
async def test_native_session_and_bulk_message_changes(request, monkeypatch, role):
    if not request.config.getoption("--memory-db"):
        pytest.skip("requires the migrated development PostgreSQL database")
    from mato_loop_backend.models import ChatSession, Message, UserAccount

    engine = create_async_engine(get_runtime_config().database_url, poolclass=NullPool)
    session_factory = async_sessionmaker(engine, expire_on_commit=False, sync_session_class=changes.ChangeTrackingSession)
    broker = ChangeEventBroker()
    publish = Mock(wraps=broker.publish)
    monkeypatch.setattr(broker, "publish", publish)
    monkeypatch.setattr(changes, "change_events", broker)
    user_id, session_id = uuid4(), uuid4()
    try:
        async with aclosing(broker.subscribe(user_id)) as stream, session_factory() as db:
            await anext(stream)
            db.add(UserAccount(id=user_id, username=f"events-{user_id}", password_hash="test"))
            await db.flush()
            db.add(ChatSession(id=session_id, user_id=user_id))
            await db.flush()
            db.add(Message(session_id=session_id, role=role, content="Test"))
            await db.commit()
            created = await anext(stream)
            publish.reset_mock()
            await db.execute(delete(Message).where(Message.session_id == session_id))
            await db.commit()
            assert (created, publish.call_args_list) == (
                {("sessions", session_id)} | ({("messages", session_id)} if role == "user" else set()),
                [call(user_id, {("messages", session_id)})] if role == "user" else [],
            )
    finally:
        async with session_factory() as db:
            await db.execute(delete(UserAccount).where(UserAccount.id == user_id))
            await db.commit()
        await engine.dispose()


@pytest.mark.parametrize("code", ["auth.sessionExpired", "auth.accountDisabled"])
async def test_stream_rechecks_auth_before_sending_more_changes(monkeypatch, code):
    from mato_loop_backend.api.routes import events as route

    user_id, secret_session_id = uuid4(), uuid4()
    clock = 0

    @asynccontextmanager
    async def session_factory():
        db = AsyncMock()
        db.scalar.return_value = None if code == "auth.sessionExpired" else SimpleNamespace(user_id=user_id)
        db.get.return_value = SimpleNamespace(is_active=False)
        yield db

    async def stream(_owner_id):
        nonlocal clock
        yield {("all", None)}
        clock = 16
        yield {("sessions", secret_session_id)}

    monkeypatch.setattr(route, "AsyncSessionFactory", session_factory)
    monkeypatch.setattr(route, "time", SimpleNamespace(monotonic=lambda: clock))
    monkeypatch.setattr(route.change_events, "subscribe", stream)
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[require_user] = lambda: SimpleNamespace(id=user_id)
    app.dependency_overrides[get_db_session] = lambda: AsyncMock()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app), base_url="http://test",
        cookies={get_runtime_config().session_cookie_name: "test-token"},
    ) as client:
        response = await client.get("/events")
    assert '"resource": "all"' in response.text and str(secret_session_id) not in response.text
