"""Утилиты работы с файловой системой для LocalWinAgent."""

from __future__ import annotations

import logging
import os
import platform
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, List

import config
from tools.apps import open_with_shell

logger = logging.getLogger(__name__)


def _expand(path: str | os.PathLike[str]) -> Path:
    expanded = os.path.expandvars(str(path))
    expanded = os.path.expanduser(expanded)
    return Path(expanded)


def normalize_path(path: str | os.PathLike[str], *, base: str | os.PathLike[str] | None = None) -> Path:
    """Нормализовать путь: раскрыть переменные и привести к абсолютному виду."""

    candidate = _expand(path)
    if candidate.is_absolute():
        return candidate.resolve(strict=False)
    base_path = _expand(base) if base is not None else Path.cwd()
    if not base_path.is_absolute():
        base_path = (Path.cwd() / base_path).resolve(strict=False)
    return (base_path / candidate).resolve(strict=False)


def get_desktop_path() -> Path:
    desktop = config._KNOWN.get("DESKTOP")  # pylint: disable=protected-access
    if desktop:
        return Path(desktop).resolve(strict=False)
    return (Path.home() / "Desktop").resolve(strict=False)


def _get_file_attributes(path: Path) -> int:
    if platform.system() != "Windows":
        return 0
    try:
        import ctypes  # type: ignore

        attrs = ctypes.windll.kernel32.GetFileAttributesW(str(path))  # type: ignore[attr-defined]
    except Exception:  # pragma: no cover - системные ошибки
        return 0
    return attrs if attrs != -1 else 0


def _is_hidden_or_system(path: Path) -> bool:
    name = path.name
    if not name:
        return False
    lowered = name.lower()
    if lowered == "desktop.ini" or lowered.startswith("~$"):
        return True
    if name.startswith("."):
        return True
    attributes = _get_file_attributes(path)
    return bool(attributes & 0x2) or bool(attributes & 0x4)


def is_path_hidden(path: Path) -> bool:
    return _is_hidden_or_system(path)


def _resolve_shortcut(path: Path) -> Path:
    if platform.system() != "Windows" or path.suffix.lower() != ".lnk":
        return path
    try:
        import win32com.client  # type: ignore

        shell = win32com.client.Dispatch("WScript.Shell")  # type: ignore[attr-defined]
        shortcut = shell.CreateShortCut(str(path))
        target = Path(shortcut.Targetpath)
        return target.resolve(strict=False)
    except Exception:  # pragma: no cover - зависимости win32com могут отсутствовать
        logger.warning("Не удалось разрешить ярлык %s", path)
        return path


def open_path(path: str) -> dict:
    target = normalize_path(path)
    if not target.exists():
        reply = f"Путь не найден: {target}"
        return {"ok": False, "path": str(target), "exists": False, "reply": reply, "error": "Путь не существует"}

    actual = _resolve_shortcut(target)
    try:
        os.startfile(str(actual))  # type: ignore[attr-defined]
    except AttributeError:  # pragma: no cover - не Windows
        process = open_with_shell(str(actual))
        if process is None:
            reply = f"Не удалось открыть: {actual}"
            return {"ok": False, "path": str(actual), "reply": reply, "error": "Не удалось открыть"}
    except Exception as exc:  # pragma: no cover - системные ошибки Windows
        reply = f"Не удалось открыть: {actual}"
        return {"ok": False, "path": str(actual), "exists": actual.exists(), "reply": reply, "error": str(exc)}

    resolved = actual.resolve(strict=False)
    reply = f"Открыто: {resolved}"
    return {"ok": True, "path": str(resolved), "exists": True, "reply": reply}


class ConfirmationRequiredError(PermissionError):
    def __init__(self, path: Path, action: str):
        super().__init__(f"Операция '{action}' требует подтверждения для пути: {path}")
        self.path = path
        self.action = action


@dataclass(slots=True)
class FileManager:
    whitelist: Iterable[str]
    _allowed_paths: List[Path] = field(init=False, default_factory=list)
    _default_root: Path = field(init=False)

    def __post_init__(self) -> None:
        self._allowed_paths = [normalize_path(item) for item in self.whitelist]
        if self._allowed_paths:
            self._default_root = self._allowed_paths[0]
        else:
            self._default_root = get_desktop_path().resolve(strict=False)
        logger.debug("Белый список: %s", self._allowed_paths)

    def _normalize(self, path: str | os.PathLike[str]) -> Path:
        return normalize_path(path, base=self._default_root)

    def normalize(self, path: str | os.PathLike[str]) -> Path:
        return self._normalize(path)

    @property
    def default_root(self) -> Path:
        return self._default_root

    def _is_allowed(self, path: Path) -> bool:
        for allowed in self._allowed_paths:
            try:
                path.relative_to(allowed)
                return True
            except ValueError:
                continue
        return False

    def requires_confirmation(self, path: Path) -> bool:
        return not self._is_allowed(path)

    def ensure_allowed(self, path: Path, action: str, confirmed: bool) -> None:
        if self.requires_confirmation(path) and not confirmed:
            raise ConfirmationRequiredError(path, action)

    def _sync_write(self, path: Path, content: str, mode: str, encoding: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open(mode, encoding=encoding) as handler:
            handler.write(content)
            handler.flush()
            os.fsync(handler.fileno())

    def create_file(self, path: str, *, content: str = "", confirmed: bool = False, encoding: str = "utf-8") -> dict:
        target = self._normalize(path)
        self.ensure_allowed(target, "создание", confirmed)
        existed = target.exists()
        mode = "a" if existed else "w"
        self._sync_write(target, content, mode=mode, encoding=encoding)
        exists = target.exists()
        size = target.stat().st_size if exists else 0
        return {"ok": exists, "path": str(target.resolve(strict=False)), "exists": exists, "size": size}

    def write_text(self, path: str, content: str, *, confirmed: bool = False, encoding: str = "utf-8") -> dict:
        target = self._normalize(path)
        self.ensure_allowed(target, "запись", confirmed)
        self._sync_write(target, content, mode="w", encoding=encoding)
        exists = target.exists()
        size = target.stat().st_size if exists else 0
        if not exists:
            raise FileNotFoundError(f"Не удалось записать файл {target}")
        return {"ok": True, "path": str(target.resolve(strict=False)), "exists": exists, "size": size}

    def append_text(self, path: str, content: str, *, confirmed: bool = False, encoding: str = "utf-8") -> dict:
        target = self._normalize(path)
        self.ensure_allowed(target, "добавление", confirmed)
        self._sync_write(target, content, mode="a", encoding=encoding)
        exists = target.exists()
        size = target.stat().st_size if exists else 0
        if not exists:
            raise FileNotFoundError(f"Не удалось добавить в файл {target}")
        return {"ok": True, "path": str(target.resolve(strict=False)), "exists": exists, "size": size}

    def list_directory(self, path: str | None = None, *, confirmed: bool = False) -> dict:
        directory = self._default_root if path is None else self._normalize(path)
        self.ensure_allowed(directory, "просмотр", confirmed)
        if not directory.exists() or not directory.is_dir():
            raise FileNotFoundError(f"Каталог {directory} не найден")
        items = [
            item.name
            for item in sorted(directory.iterdir(), key=lambda candidate: candidate.name.lower())
            if not is_path_hidden(item)
        ]
        return {"ok": True, "path": str(directory.resolve(strict=False)), "items": items}

    def open_path(self, path: str, *, confirmed: bool = False) -> dict:
        target = self._normalize(path)
        self.ensure_allowed(target, "открытие", confirmed)
        return open_path(str(target))

    def copy_path(self, src: str, dst: str, *, confirmed: bool = False) -> dict:
        source = self._normalize(src)
        destination = self._normalize(dst)
        self.ensure_allowed(destination, "копирование", confirmed)
        if source.is_dir():
            shutil.copytree(source, destination, dirs_exist_ok=True)
        else:
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, destination)
        exists = destination.exists()
        return {"ok": exists, "path": str(destination.resolve(strict=False)), "exists": exists}

    def move_path(self, src: str, dst: str, *, confirmed: bool = False) -> dict:
        source = self._normalize(src)
        destination = self._normalize(dst)
        self.ensure_allowed(destination, "перемещение", confirmed)
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(source, destination)
        exists = destination.exists()
        return {"ok": exists, "path": str(destination.resolve(strict=False)), "exists": exists}

    def delete_path(self, path: str, *, confirmed: bool = False) -> dict:
        target = self._normalize(path)
        self.ensure_allowed(target, "удаление", confirmed)
        if target.is_dir():
            shutil.rmtree(target, ignore_errors=False)
        elif target.exists():
            target.unlink()
        exists = target.exists()
        return {"ok": not exists, "path": str(target.resolve(strict=False)), "exists": exists}
