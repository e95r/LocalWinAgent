from __future__ import annotations

from pathlib import Path

import os

import pytest
from docx import Document

import config
from intent_router import AgentSession, IntentRouter, SessionState


@pytest.fixture()
def router_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
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

    if not hasattr(os, "startfile"):
        monkeypatch.setattr(os, "startfile", lambda *_args, **_kwargs: None, raising=False)

    return router, session, state, allow_dir


def _read_docx_text(path: Path) -> str:
    document = Document(path)
    return "\n".join(paragraph.text for paragraph in document.paragraphs)


def test_create_word_document_with_content(router_env) -> None:
    router, session, state, allow_dir = router_env
    message = 'создай документ word отчет с текстом "Привет, мир"'
    response = router.handle_message(message, session, state)

    target = (allow_dir / "отчет.docx").resolve(strict=False)
    assert target.exists()
    assert _read_docx_text(target).strip() == "Привет, мир"

    info = response["data"]["result"]
    assert info["path"] == str(target)
    assert info["verified"] is True
    assert info["status"] == "created"
    assert response["ok"] is True


def test_create_text_file_with_content(router_env) -> None:
    router, session, state, allow_dir = router_env
    message = "создай текстовый файл заметка: сделать план"
    response = router.handle_message(message, session, state)

    target = (allow_dir / "заметка.txt").resolve(strict=False)
    assert target.exists()
    assert target.read_text(encoding="utf-8") == "сделать план"

    info = response["data"]["result"]
    assert info["path"] == str(target)
    assert info["status"] == "created"
    assert info["verified"] is True


def test_overwrite_and_append(router_env) -> None:
    router, session, state, allow_dir = router_env
    router.handle_message('создай текстовый файл записка: первая строка', session, state)

    overwrite = router.handle_message('перезапиши в записка.txt текст "Вторая"', session, state)
    target = (allow_dir / "записка.txt").resolve(strict=False)
    assert target.read_text(encoding="utf-8") == "Вторая"
    assert overwrite["data"]["result"]["status"] == "overwritten"
    assert overwrite["data"]["result"]["verified"] is True

    append = router.handle_message('допиши в записка.txt текст " +доп"', session, state)
    assert target.read_text(encoding="utf-8") == "Вторая +доп"
    assert append["data"]["result"]["status"] == "appended"
    assert append["data"]["result"]["verified"] is True


def test_confirmation_required_outside(router_env, tmp_path: Path) -> None:
    router, session, state, _ = router_env
    outside = (tmp_path / "outside" / "data.txt").resolve(strict=False)
    message = f'запиши в {outside} текст "секрет"'
    response = router.handle_message(message, session, state)

    assert response["requires_confirmation"] is True
    info = response["data"]["result"]
    assert info["requires_confirmation"] is True
    assert info["verified"] is False
    assert info["path"] == str(outside)
    assert not outside.exists()
