from pathlib import Path

from intent_router import AgentSession, IntentRouter
from tools.files import ConfirmationRequiredError


def test_open_app_intent(monkeypatch):
    router = IntentRouter()
    session = AgentSession(auto_confirm=True)

    monkeypatch.setattr(router.app_manager, "launch", lambda app: f"Запуск {app}")

    response = router.handle_message("Открой текстовый редактор", session)
    assert "Запуск" in response["reply"]
    assert not response["requires_confirmation"]


def test_web_search(monkeypatch):
    router = IntentRouter()
    session = AgentSession(auto_confirm=True)

    monkeypatch.setattr(router.web_automation, "search_and_open", lambda query: f"Поиск: {query}")

    response = router.handle_message("Найди страницу fastapi background tasks и открой", session)
    assert "fastapi background tasks" in response["reply"]


def test_search_file(monkeypatch):
    router = IntentRouter()
    session = AgentSession()

    monkeypatch.setattr("intent_router.search_files", lambda query: ["C:/Reports/report.txt"])

    response = router.handle_message("Найди файл отчёт по продажам 2023", session)
    assert "report.txt" in response["reply"]


def test_file_read_confirmation(monkeypatch):
    router = IntentRouter()
    session = AgentSession(auto_confirm=False)

    def fake_read(path: str, confirmed: bool = False):
        if not confirmed:
            raise ConfirmationRequiredError(Path(path), "чтение")
        return "секретные данные"

    monkeypatch.setattr(router.file_manager, "read_text", fake_read)

    message = "Прочитай файл C:\\Users\\User\\Secret.txt"
    first = router.handle_message(message, session)
    assert first["requires_confirmation"]

    confirmed = router.handle_message("да", session, force_confirm=True)
    assert "секретные данные" in confirmed["reply"]


def test_llm_fallback(monkeypatch):
    router = IntentRouter()
    session = AgentSession()

    monkeypatch.setattr(router.llm_client, "chat", lambda msg, model=None: f"LLM:{msg}")

    response = router.handle_message("Какая сегодня погода?", session)
    assert response["reply"].startswith("LLM:")


def test_switch_model():
    router = IntentRouter()
    session = AgentSession()

    result = router.handle_message("Переключи модель qwen2:7b", session)
    assert "qwen2:7b" in result["reply"]
    assert session.model == "qwen2:7b"
