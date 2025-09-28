"""Проверка нового маршрута интентов без ключевых слов."""
from __future__ import annotations

import os
import platform
from pathlib import Path
from typing import List, Tuple

import pytest

import config
from intent_router import AgentSession, IntentRouter, SessionState


@pytest.fixture()
def intent_router(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Tuple[IntentRouter, AgentSession, SessionState]:
    allow_dir = tmp_path / "allow"
    allow_dir.mkdir()

    def fake_load_config(name: str) -> dict:
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
    session = AgentSession()
    state = SessionState()

    if platform.system() != "Windows":
        monkeypatch.setattr("intent_router.open_path", lambda path: {"ok": True, "path": path, "reply": f"Открыто: {path}"})
        monkeypatch.setattr(os, "startfile", lambda *_args, **_kwargs: None, raising=False)

    return router, session, state


def test_infer_open_file(
    intent_router: Tuple[IntentRouter, AgentSession, SessionState],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    router, session, state = intent_router
    found = [str(tmp_path / "screen.png")]
    search_calls: List[Tuple[str, Tuple[str, ...]]] = []

    def fake_search(query: str, **kwargs) -> List[str]:
        extensions = tuple(kwargs.get("extensions") or ())
        search_calls.append((query, extensions))
        return found

    opened: List[str] = []

    def fake_open(path: str) -> dict:
        opened.append(path)
        return {"ok": True, "path": path, "reply": f"Открыто: {path}"}

    monkeypatch.setattr("intent_router.search_local", fake_search)
    monkeypatch.setattr("intent_router.open_path", fake_open)

    response = router.handle_message("посмотреть скриншот", session, state)

    assert response["ok"] is True
    assert opened == found
    assert search_calls and "скриншот" in search_calls[0][0]
    assert state.get_results(kind="file") == found


def test_infer_open_app(
    intent_router: Tuple[IntentRouter, AgentSession, SessionState],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    router, session, state = intent_router
    called: List[str] = []

    monkeypatch.setattr(router.app_manager, "launch", lambda key: called.append(key) or "Готово")

    response = router.handle_message("запусти калькулятор", session, state)

    assert response["ok"] is True
    assert called and called[0]
    assert state.last_kind == "app"


def test_infer_open_web(
    intent_router: Tuple[IntentRouter, AgentSession, SessionState],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    router, session, state = intent_router
    results = [
        ("FastAPI — Docs", "https://fastapi.tiangolo.com"),
        ("FastAPI Tutorial", "https://example.com/tutorial"),
    ]
    opened: List[str] = []

    monkeypatch.setattr("intent_router.search_web", lambda query: results)
    monkeypatch.setattr("intent_router.open_site", lambda url: opened.append(url) or url)

    response = router.handle_message("нужна документация fastapi", session, state)

    assert response["ok"] is True
    assert opened == [results[0][1]]
    assert state.last_kind == "web"
    assert state.get_results(kind="web") == [item[1] for item in results]


def test_context_pronoun_after_search(
    intent_router: Tuple[IntentRouter, AgentSession, SessionState],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    router, session, state = intent_router
    found = [str(tmp_path / "report.pdf")]
    monkeypatch.setattr("intent_router.search_local", lambda *args, **kwargs: found)
    opened: List[str] = []

    def fake_open(path: str) -> dict:
        opened.append(path)
        return {"ok": True, "path": path, "reply": f"Открыто: {path}"}

    monkeypatch.setattr("intent_router.open_path", fake_open)

    router.handle_message("мне нужен вчерашний отчёт", session, state)
    assert opened == [found[0]]

    router.handle_message("открой его", session, state)
    assert opened == [found[0], found[0]]


def test_reset_context(
    intent_router: Tuple[IntentRouter, AgentSession, SessionState],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    router, session, state = intent_router
    found = [str(tmp_path / "video.mp4")]
    monkeypatch.setattr("intent_router.search_local", lambda *args, **kwargs: found)
    monkeypatch.setattr("intent_router.open_path", lambda path: {"ok": True, "path": path, "reply": f"Открыто: {path}"})

    router.handle_message("запусти видео", session, state)
    assert state.last_kind == "file"

    response = router.handle_message("сбрось контекст", session, state)
    assert response["ok"] is True
    assert state.last_kind == "none"
    assert state.get_results() == []
