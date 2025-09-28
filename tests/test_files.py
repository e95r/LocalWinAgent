"""Тесты для FileManager."""
from __future__ import annotations

import os
import platform
from pathlib import Path

import pytest

from tools.files import ConfirmationRequiredError, FileManager


@pytest.fixture()
def file_manager(tmp_path: Path) -> FileManager:
    allow_dir = tmp_path / "allow"
    allow_dir.mkdir()
    manager = FileManager([str(allow_dir)])
    return manager


def test_create_write_append_list_delete(
    file_manager: FileManager, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    allow_dir = tmp_path / "allow"
    target = allow_dir / "sample.txt"

    result_create = file_manager.create_file(str(target), confirmed=True)
    assert result_create["ok"] is True
    assert Path(result_create["path"]).exists()

    result_write = file_manager.write_text(str(target), "привет", confirmed=True)
    assert result_write["ok"] is True
    assert Path(result_write["path"]).read_text(encoding="utf-8") == "привет"

    previous_size = result_write["size"]
    result_append = file_manager.append_text(str(target), " мир", confirmed=True)
    assert result_append["size"] > previous_size
    assert Path(result_append["path"]).read_text(encoding="utf-8") == "привет мир"

    listing = file_manager.list_directory(str(allow_dir), confirmed=True)
    assert listing["ok"] is True
    assert "sample.txt" in listing["items"]

    opened: dict[str, str] = {}

    if platform.system() != "Windows":
        from tools import apps as apps_module
        from tools import files as files_module

        monkeypatch.setattr(apps_module, "open_with_shell", lambda p: opened.setdefault("path", p))
        monkeypatch.setattr(files_module, "open_with_shell", lambda p: opened.setdefault("path", p))
    else:
        monkeypatch.setattr(os, "startfile", lambda p: opened.setdefault("path", p), raising=False)

    open_result = file_manager.open_path(str(allow_dir))
    assert open_result["ok"] is True
    assert Path(open_result["path"]).resolve(strict=False) == allow_dir.resolve(strict=False)
    if opened:
        assert Path(opened["path"]).resolve(strict=False) == allow_dir.resolve(strict=False)

    delete_result = file_manager.delete_path(str(target), confirmed=True)
    assert delete_result["ok"] is True
    assert not Path(delete_result["path"]).exists()


def test_requires_confirmation(file_manager: FileManager, tmp_path: Path) -> None:
    outside = tmp_path / "outside.txt"
    with pytest.raises(ConfirmationRequiredError):
        file_manager.write_text(str(outside), "данные", confirmed=False)
