import pytest

from intent_router import AgentSession, IntentRouter, SessionState


@pytest.fixture()
def router(monkeypatch):
    router = IntentRouter()
    # Отключаем реальные обращения к Everything и приложениям в тестах
    monkeypatch.setattr(router.llm, "generate", lambda prompt, model=None, stream=True: "Тестовый ответ")
    return router


def test_smalltalk_uses_llm(router):
    session = AgentSession()
    state = {"session_state": SessionState()}
    response = router.handle_message("кто ты?", session, state)
    assert response["reply"].startswith("Тестовый ответ")


def test_open_browser_requires_choice(monkeypatch):
    router = IntentRouter()
    session = AgentSession()
    state = {"session_state": SessionState()}

    monkeypatch.setattr(router.llm, "generate", lambda prompt, model=None, stream=True: "Тестовый ответ")

    available = {"chrome", "edge"}
    launches: list[str] = []

    monkeypatch.setattr(
        "intent_router.apps_module.is_installed",
        lambda app_id: app_id in available,
    )
    monkeypatch.setattr(
        "intent_router.apps_module.launch",
        lambda app_id: launches.append(app_id) or {"ok": True, "message": "ok", "path": app_id},
    )

    response = router.handle_message("открой браузер", session, state)
    assert "Какой браузер открыть?" in response["reply"]
    assert not launches
    assert session.awaiting_browser_choice is True
    assert set(session.available_browsers) == available

    response_choice = router.handle_message("хром", session, state)
    assert launches == ["chrome"]
    assert session.preferred_browser == "chrome"
    assert session.awaiting_browser_choice is False
    assert response_choice["ok"] is True
