from __future__ import annotations

from pathlib import Path
from typing import Dict, Tuple

import pytest

import config
from intent_router import AgentSession, IntentRouter, SessionState


@pytest.fixture(autouse=True)
def inline_sandbox(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LOCALWINAGENT_INLINE_SANDBOX", "1")
    yield
    monkeypatch.delenv("LOCALWINAGENT_INLINE_SANDBOX", raising=False)


@pytest.fixture()
def router_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Tuple[IntentRouter, Path]:
    allow_dir = tmp_path / "allow"
    allow_dir.mkdir()

    def fake_load_config(name: str) -> Dict[str, object]:
        if name == "paths":
            return {"whitelist": [str(allow_dir)], "default_downloads": str(allow_dir)}
        if name == "apps":
            return {"apps": {}}
        if name == "web":
            return {"browser": "chromium", "headless": True, "implicit_wait_ms": 1000}
        raise KeyError(name)

    config.refresh_cache()
    monkeypatch.setattr(config, "load_config", fake_load_config)
    monkeypatch.setattr("intent_router.load_config", fake_load_config)

    router = IntentRouter()
    monkeypatch.chdir(allow_dir)
    return router, allow_dir


def test_create_file_task(router_env: Tuple[IntentRouter, Path]) -> None:
    router, allow_dir = router_env
    session = AgentSession(auto_confirm=True)
    state = SessionState()

    response = router.handle_message("создай файл report.txt с текстом привет", session, state)

    assert response["ok"] is True
    assert "exists=True" in response["reply"]
    created = allow_dir / "report.txt"
    assert created.exists()
    assert created.read_text(encoding="utf-8") == "привет"


def test_open_calculator(router_env: Tuple[IntentRouter, Path], monkeypatch: pytest.MonkeyPatch) -> None:
    router, _ = router_env
    session = AgentSession(auto_confirm=True)
    state = SessionState()

    launched: list[str] = []

    def fake_launch(name: str) -> dict:
        launched.append(name)
        return {"ok": True, "message": "Готово"}

    monkeypatch.setattr("tools.apps.launch", fake_launch)

    response = router.handle_message("открой калькулятор", session, state)

    assert response["ok"] is True
    assert launched == ["calc"]
    assert state.last_kind == "app"


def test_show_screenshot(router_env: Tuple[IntentRouter, Path], monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    router, allow_dir = router_env
    session = AgentSession(auto_confirm=True)
    state = SessionState()

    target = allow_dir / "screen.png"
    target.write_text("fake", encoding="utf-8")

    def fake_search(query: str, **kwargs) -> list[str]:
        return [str(target.resolve(strict=False))]

    opened: list[str] = []

    def fake_open(path: str) -> dict:
        opened.append(path)
        return {"ok": True, "reply": f"Открыто: {path}"}

    monkeypatch.setattr("tools.search.search_local", fake_search)
    monkeypatch.setattr("tools.files.open_path", fake_open)

    response = router.handle_message("покажи вчерашний скриншот", session, state)

    assert response["ok"] is True
    assert opened == [str(target.resolve(strict=False))]
    assert state.get_results(kind="file") == opened


def test_search_web_and_open(router_env: Tuple[IntentRouter, Path], monkeypatch: pytest.MonkeyPatch) -> None:
    router, _ = router_env
    session = AgentSession(auto_confirm=True)
    state = SessionState()

    results = [
        {"title": "FastAPI Docs", "url": "https://fastapi.tiangolo.com"},
        {"title": "FastAPI Tutorial", "url": "https://example.com/tutorial"},
    ]

    opened: list[str] = []

    def fake_search(query: str, max_results: int = 5) -> list[dict]:
        return results

    def fake_open(url: str) -> dict:
        opened.append(url)
        return {"ok": True, "title": url, "url": url}

    monkeypatch.setattr("tools.web.search_web", fake_search)
    monkeypatch.setattr("tools.web.open_site", fake_open)

    response = router.handle_message("найди сайт fastapi документация", session, state)

    assert response["ok"] is True
    assert opened == [results[0]["url"]]
    assert state.last_kind == "web"
    assert state.get_results(kind="web") == [item["url"] for item in results]


def test_context_open_last_result(router_env: Tuple[IntentRouter, Path], monkeypatch: pytest.MonkeyPatch) -> None:
    router, allow_dir = router_env
    session = AgentSession(auto_confirm=True)
    state = SessionState()

    file_path = allow_dir / "shot.png"
    file_path.write_text("fake", encoding="utf-8")
    absolute = str(file_path.resolve(strict=False))

    monkeypatch.setattr("tools.search.search_local", lambda *args, **kwargs: [absolute])

    opened: list[str] = []
    monkeypatch.setattr("tools.files.open_path", lambda path: opened.append(path) or {"ok": True, "reply": f"Открыто: {path}"})

    router.handle_message("найди файл скриншот", session, state)
    assert state.get_results(kind="file") == [absolute]

    response = router.handle_message("открой его", session, state)

    assert response["ok"] is True
    resolved = [str(Path(path).resolve(strict=False)) for path in opened]
    assert resolved == [str(Path(absolute).resolve(strict=False))]


def test_requires_confirmation(router_env: Tuple[IntentRouter, Path], tmp_path: Path) -> None:
    router, _ = router_env
    session = AgentSession(auto_confirm=False)
    state = SessionState()

    outside = tmp_path / "outside" / "report.txt"
    message = f"запиши в {outside}: тест"

    response = router.handle_message(message, session, state)

    assert response["ok"] is False
    assert response.get("requires_confirmation") is True
    assert "Нужно подтверждение" in response["reply"]
