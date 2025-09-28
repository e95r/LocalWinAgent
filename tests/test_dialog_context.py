from __future__ import annotations

import os
import platform
from pathlib import Path
from typing import List

import pytest

import config
from intent_router import AgentSession, IntentRouter, SessionState


@pytest.fixture()
def dialog_router(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> IntentRouter:
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
    monkeypatch.chdir(allow_dir)

    if platform.system() != "Windows":
        from tools import apps as apps_module
        from tools import files as files_module

        monkeypatch.setattr(apps_module, "open_with_shell", lambda p: p)
        monkeypatch.setattr(files_module, "open_with_shell", lambda p: p)
    else:
        monkeypatch.setattr(os, "startfile", lambda p: p, raising=False)

    return router


def test_dialog_context(dialog_router: IntentRouter, monkeypatch: pytest.MonkeyPatch) -> None:
    router = dialog_router
    session = AgentSession()
    state = SessionState()

    fake_results = [
        "C:/Users/Test/Desktop/screen1.png",
        "C:/Users/Test/Pictures/screen2.png",
        "D:/Shots/screen3.png",
    ]
    expected_paths = [str(Path(path).expanduser().resolve(strict=False)) for path in fake_results]

    monkeypatch.setattr("intent_router.search_files", lambda query: fake_results)

    opened: List[str] = []

    def fake_open(path: str) -> dict:
        opened.append(path)
        return {"ok": True, "path": path}

    monkeypatch.setattr("intent_router.open_path", fake_open)

    search_response = router.handle_message("найди файл скриншот", session, state)
    assert search_response["ok"] is True
    assert search_response.get("items") == expected_paths
    assert "1)" in search_response["reply"]
    assert state.get_results() == expected_paths

    open_second = router.handle_message("открой 2", session, state)
    assert open_second["ok"] is True
    assert expected_paths[1] in open_second["reply"]
    assert opened[-1] == expected_paths[1]

    open_pronoun = router.handle_message("открой его", session, state)
    assert open_pronoun["ok"] is True
    assert expected_paths[0] in open_pronoun["reply"]
    assert opened[-1] == expected_paths[0]

    out_of_range = router.handle_message("открой 100", session, state)
    assert out_of_range["ok"] is False
    assert "Выберите число от 1 до 3" in out_of_range["reply"]

    reset_response = router.handle_message("сбрось контекст", session, state)
    assert reset_response["ok"] is True
    assert state.get_results() == []

    no_context = router.handle_message("открой первый", session, state)
    assert no_context["ok"] is False
    assert "Нет сохранённых результатов" in no_context["reply"]

    assert opened.count(expected_paths[1]) == 1
    assert expected_paths[0] in opened
