from __future__ import annotations

from pathlib import Path
from typing import Tuple

import os
import platform

import pytest

import config
from intent_router import AgentSession, IntentRouter, SessionState
from tools.files import FileManager


@pytest.fixture()
def router_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Tuple[IntentRouter, AgentSession, SessionState, Path]:
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
    monkeypatch.setenv("LOCALWINAGENT_INLINE_SANDBOX", "1")
    monkeypatch.setattr(config, "load_config", fake_load_config)
    monkeypatch.setattr("intent_router.load_config", fake_load_config)

    router = IntentRouter()
    session = AgentSession()
    state = SessionState()

    if platform.system() != "Windows":
        monkeypatch.setattr(os, "startfile", lambda *_args, **_kwargs: None, raising=False)

    return router, session, state, allow_dir


def test_search_file_lists_without_open(router_env, monkeypatch: pytest.MonkeyPatch) -> None:
    router, session, state, allow_dir = router_env
    demo_path = str((allow_dir / "demo.txt").resolve(strict=False))

    search_called = {"count": 0}
    open_called = {"count": 0}

    def fake_search(query: str, **kwargs) -> list[str]:
        search_called["count"] += 1
        return [demo_path]

    def fake_open(self: FileManager, path: str) -> dict:
        open_called["count"] += 1
        return {"ok": True, "path": path}

    monkeypatch.setattr("tools.search.search_files", fake_search, raising=False)
    monkeypatch.setattr(FileManager, "open_path", fake_open, raising=False)

    response = router.handle_message("найди файл demo.txt", session, state)

    assert search_called["count"] == 1
    assert open_called["count"] == 0
    assert "Нашёл файлы" in response["reply"]
    assert demo_path in response.get("items", [])
    assert state.get_results(kind="file") == [demo_path]


def test_open_file_uses_path(router_env, monkeypatch: pytest.MonkeyPatch) -> None:
    router, session, state, allow_dir = router_env
    target = str((allow_dir / "demo.txt").resolve(strict=False))

    def fake_open(self: FileManager, path: str) -> dict:
        assert path == target
        return {"ok": True, "path": target}

    monkeypatch.setattr(FileManager, "open_path", fake_open, raising=False)

    response = router.handle_message(f"открой {target}", session, state)

    assert response["reply"] == f"Открыл: {target}"
    assert response.get("requires_confirmation") is False


def test_open_file_uses_last_result_pronoun(router_env, monkeypatch: pytest.MonkeyPatch) -> None:
    router, session, state, allow_dir = router_env
    first = str((allow_dir / "first.txt").resolve(strict=False))
    second = str((allow_dir / "second.txt").resolve(strict=False))

    monkeypatch.setattr(
        "tools.search.search_files",
        lambda query, **kwargs: [first, second],
        raising=False,
    )

    opened: list[str] = []

    def fake_open(self: FileManager, path: str) -> dict:
        opened.append(path)
        return {"ok": True, "path": path}

    monkeypatch.setattr(FileManager, "open_path", fake_open, raising=False)

    router.handle_message("найди файл отчёт", session, state)
    response = router.handle_message("открой его", session, state)

    assert opened == [first]
    assert response["reply"] == f"Открыл: {first}"


def test_open_file_by_index(router_env, monkeypatch: pytest.MonkeyPatch) -> None:
    router, session, state, allow_dir = router_env
    first = str((allow_dir / "first.txt").resolve(strict=False))
    second = str((allow_dir / "second.txt").resolve(strict=False))

    monkeypatch.setattr(
        "tools.search.search_files",
        lambda query, **kwargs: [first, second],
        raising=False,
    )

    opened: list[str] = []

    def fake_open(self: FileManager, path: str) -> dict:
        opened.append(path)
        return {"ok": True, "path": path}

    monkeypatch.setattr(FileManager, "open_path", fake_open, raising=False)

    router.handle_message("найди файл отчёт", session, state)
    response = router.handle_message("открой 2", session, state)

    assert opened == [second]
    assert response["reply"] == f"Открыл: {second}"


def test_open_file_error(router_env, monkeypatch: pytest.MonkeyPatch) -> None:
    router, session, state, allow_dir = router_env
    target = str((allow_dir / "missing.txt").resolve(strict=False))

    def fake_open(self: FileManager, path: str) -> dict:
        return {"ok": False, "path": path, "error": "Ошибка открытия"}

    monkeypatch.setattr(FileManager, "open_path", fake_open, raising=False)

    response = router.handle_message(f"открой {target}", session, state)

    assert response["reply"].startswith("Не удалось открыть: Ошибка открытия")
    assert response.get("requires_confirmation") is False
