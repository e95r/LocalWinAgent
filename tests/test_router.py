"""Тесты маршрутизатора интентов."""
from __future__ import annotations

import os
import platform
from pathlib import Path
from typing import Dict

import pytest

import config
from intent_router import AgentSession, IntentRouter, SessionState


@pytest.fixture()
def router_with_tmp(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Dict[str, object]:
    allow_dir = tmp_path / "allow"
    allow_dir.mkdir()

    def fake_load_config(name: str) -> Dict[str, object]:
        if name == "paths":
            return {
                "whitelist": [str(allow_dir)],
                "default_downloads": str(allow_dir),
            }
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
    monkeypatch.chdir(allow_dir)

    if platform.system() != "Windows":
        from tools import apps as apps_module
        from tools import files as files_module

        monkeypatch.setattr(apps_module, "open_with_shell", lambda p: p)
        monkeypatch.setattr(files_module, "open_with_shell", lambda p: p)
        monkeypatch.setattr("tools.files.open_path", lambda path: {"ok": True, "reply": f"Открыто: {path}"})
    else:
        monkeypatch.setattr(os, "startfile", lambda p: p, raising=False)
        monkeypatch.setattr("tools.files.open_path", lambda path: {"ok": True, "reply": f"Открыто: {path}"})

    return {"router": router, "allow_dir": allow_dir, "monkeypatch": monkeypatch}


def test_full_file_flow(router_with_tmp: Dict[str, object]) -> None:
    router: IntentRouter = router_with_tmp["router"]  # type: ignore[assignment]
    allow_dir: Path = router_with_tmp["allow_dir"]  # type: ignore[assignment]
    session = AgentSession(auto_confirm=True)
    state = SessionState()

    response_create = router.handle_message("создай файл test.txt", session, state)
    assert "exists=True" in response_create["reply"]
    full_path = allow_dir / "test.txt"
    assert full_path.exists()

    response_write = router.handle_message("запиши в test.txt: привет", session, state)
    assert "exists=True" in response_write["reply"]
    assert full_path.read_text(encoding="utf-8") == "привет"

    response_append = router.handle_message("добавь к test.txt: ещё", session, state)
    assert "exists=True" in response_append["reply"]
    assert full_path.read_text(encoding="utf-8") == "приветещё"

    response_open = router.handle_message("открой файл test.txt", session, state)
    assert response_open["reply"].startswith("Открыто: ")
    assert str(full_path.resolve()) in response_open["reply"]

    response_list = router.handle_message("покажи каталог .", session, state)
    assert "Каталог:" in response_list["reply"]
    assert "test.txt" in response_list["reply"]


def test_router_desktop_listing(router_with_tmp: Dict[str, object]) -> None:
    router: IntentRouter = router_with_tmp["router"]  # type: ignore[assignment]
    allow_dir: Path = router_with_tmp["allow_dir"]  # type: ignore[assignment]
    monkeypatch: pytest.MonkeyPatch = router_with_tmp["monkeypatch"]  # type: ignore[assignment]

    (allow_dir / "visible.txt").write_text("ok", encoding="utf-8")
    (allow_dir / "desktop.ini").write_text("hidden", encoding="utf-8")

    monkeypatch.setattr("intent_router.get_desktop_path", lambda: allow_dir)

    session = AgentSession(auto_confirm=True)
    state = SessionState()
    response = router.handle_message("Какие файлы есть на рабочем столе?", session, state)
    assert "Рабочий стол" in response["reply"]
    assert "visible.txt" in response["reply"]
    assert "desktop.ini" not in response["reply"]


def test_router_desktop_path(router_with_tmp: Dict[str, object]) -> None:
    router: IntentRouter = router_with_tmp["router"]  # type: ignore[assignment]
    allow_dir: Path = router_with_tmp["allow_dir"]  # type: ignore[assignment]
    monkeypatch: pytest.MonkeyPatch = router_with_tmp["monkeypatch"]  # type: ignore[assignment]

    monkeypatch.setattr("intent_router.get_desktop_path", lambda: allow_dir)

    session = AgentSession(auto_confirm=True)
    state = SessionState()
    response = router.handle_message("напиши путь до рабочего стола", session, state)
    assert str(allow_dir.resolve(strict=False)) in response["reply"]
