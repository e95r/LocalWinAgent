from __future__ import annotations

from pathlib import Path
from typing import Callable

import pytest

from intent_router import AgentSession, IntentRouter, SessionState


@pytest.fixture()
def router_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    allow = tmp_path / "allow"
    allow.mkdir()

    monkeypatch.setattr("intent_router.load_config", lambda name: {"whitelist": [str(allow)]})

    router = IntentRouter()
    session = AgentSession()
    state = {"session_state": SessionState()}

    return router, session, state, allow, monkeypatch


def _patch_intent(
    router: IntentRouter,
    monkeypatch: pytest.MonkeyPatch,
    factory: Callable[[str], dict[str, str]],
) -> None:
    monkeypatch.setattr(router.intent_inferencer, "infer", factory)


def test_handle_message_without_optional_flags(router_env) -> None:
    router, session, state, allow, monkeypatch = router_env
    target = allow / "simple.txt"

    def make_intent(_: str) -> dict[str, str]:
        return {"intent": "create_file", "path": str(target), "content": "ping"}

    _patch_intent(router, monkeypatch, make_intent)
    session.auto_confirm = True

    response = router.handle_message("создай", session, state)
    assert isinstance(response, dict)
    assert response["requires_confirmation"] is False
    assert "Создан файл" in response["reply"]


def test_requires_confirmation_response(router_env) -> None:
    router, session, state, allow, monkeypatch = router_env
    outside = allow.parent / "outside.txt"

    def make_intent(_: str) -> dict[str, str]:
        return {"intent": "write_file", "path": str(outside), "content": "данные"}

    _patch_intent(router, monkeypatch, make_intent)
    session.auto_confirm = False

    response = router.handle_message("записать", session, state)
    assert response["requires_confirmation"] is True
    assert "Нужно подтверждение" in response["reply"]


def test_force_confirm_allows_operation(router_env) -> None:
    router, session, state, allow, monkeypatch = router_env
    outside = allow.parent / "force.txt"

    def make_intent(_: str) -> dict[str, str]:
        return {"intent": "write_file", "path": str(outside), "content": "force"}

    _patch_intent(router, monkeypatch, make_intent)

    response = router.handle_message("записать", session, state, force_confirm=True)
    assert response["requires_confirmation"] is False
    assert "exists=True" in response["reply"]
    assert outside.exists()


def test_session_auto_confirm(router_env) -> None:
    router, session, state, allow, monkeypatch = router_env
    outside = allow.parent / "auto.txt"

    def make_intent(_: str) -> dict[str, str]:
        return {"intent": "write_file", "path": str(outside), "content": "auto"}

    _patch_intent(router, monkeypatch, make_intent)
    session.auto_confirm = True

    response = router.handle_message("записать", session, state)
    assert response["requires_confirmation"] is False
    assert outside.exists()


def test_handle_message_accepts_websocket_flags(router_env) -> None:
    router, session, state, allow, monkeypatch = router_env
    target = allow / "ws.txt"

    def make_intent(_: str) -> dict[str, str]:
        return {"intent": "write_file", "path": str(target), "content": "ws"}

    _patch_intent(router, monkeypatch, make_intent)

    response = router.handle_message(
        "записать",
        session,
        state,
        auto_confirm=True,
        force_confirm=False,
    )
    assert response["requires_confirmation"] is False
    assert target.exists()

