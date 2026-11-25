from uuid import uuid4

from mato_loop_backend.sessions import presence


def test_presence_is_user_scoped_renewable_and_expires(monkeypatch) -> None:
    now = 100.0
    monkeypatch.setattr(presence.time, "monotonic", lambda: now)
    monkeypatch.setattr(presence, "_presence", {})
    user_id = uuid4()

    presence.mark_user_present(user_id)

    assert presence.user_is_present(user_id)
    assert not presence.user_is_present(uuid4())
    now += presence.PRESENCE_TTL_SECONDS - 1
    presence.mark_user_present(user_id)
    now += 1
    assert presence.user_is_present(user_id)
    now += presence.PRESENCE_TTL_SECONDS - 1
    assert not presence.user_is_present(user_id)


def test_session_presence_tracks_tabs_and_releases_only_the_leaving_tab(monkeypatch) -> None:
    now = 100.0
    monkeypatch.setattr(presence, "_presence", {})
    monkeypatch.setattr(presence.time, "monotonic", lambda: now)
    user_id, session_id, first, second = uuid4(), uuid4(), uuid4(), uuid4()
    presence.mark_user_present(user_id, client_id=first, session_id=session_id)
    presence.mark_user_present(user_id, client_id=second, session_id=session_id)
    presence.mark_user_present(user_id, client_id=first, active=False)
    assert presence.session_is_present(user_id, session_id)
    presence.mark_user_present(user_id, client_id=second, session_id=None)
    assert not presence.session_is_present(user_id, session_id)
    assert presence.user_is_present(user_id)
    assert presence.presence_remaining(user_id) == presence.PRESENCE_TTL_SECONDS
    presence.mark_user_present(user_id, client_id=second, active=False)
    assert not presence.user_is_present(user_id)
    assert presence.presence_remaining(user_id) == 0
