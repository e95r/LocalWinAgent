"""Утилиты работы с файловой системой для LocalWinAgent."""

from __future__ import annotations

import logging
import os
import platform
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, List, Optional

import config
from tools.apps import open_with_shell
from tools import docx_writer

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
    expanded = os.path.expandvars(str(path))
    expanded = os.path.expanduser(expanded)
    target = Path(expanded).resolve(strict=False)

    if not target.exists():
        return {
            "ok": False,
            "path": str(target),
            "exists": False,
            "error": "Путь не существует",
            "verified": False,
        }

    actual = _resolve_shortcut(target)
    resolved = actual.resolve(strict=False)

    try:
        if hasattr(os, "startfile"):
            os.startfile(str(actual))  # type: ignore[attr-defined]
        else:  # pragma: no cover - не Windows
            open_with_shell(str(actual))
    except Exception as exc:  # pragma: no cover - системные ошибки Windows
        exists = resolved.exists()
        return {
            "ok": False,
            "path": str(resolved),
            "exists": exists,
            "error": str(exc),
            "verified": exists,
        }

    return {"ok": True, "path": str(resolved), "exists": True, "verified": True}


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

    def ensure_allowed(self, path: Path, action: str, confirmed: bool) -> Optional[dict]:
        if self.requires_confirmation(path) and not confirmed:
            absolute = path.resolve(strict=False)
            logger.warning("Требуется подтверждение для %s: %s", action, absolute)
            return {
                "ok": False,
                "path": str(absolute),
                "requires_confirmation": True,
                "error": "Требуется подтверждение для операции вне белого списка",
                "verified": False,
            }
        return None

    def _sync_write(self, path: Path, content: str, mode: str, encoding: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open(mode, encoding=encoding) as handler:
            handler.write(content)
            handler.flush()
            os.fsync(handler.fileno())

    def _write_content(self, path: Path, content: str, *, append: bool, encoding: str) -> None:
        suffix = path.suffix.lower()
        if suffix == ".docx":
            if append:
                docx_writer.append_docx(path, content)
            else:
                docx_writer.write_docx(path, content)
            return
        mode = "a" if append else "w"
        self._sync_write(path, content, mode=mode, encoding=encoding)

    def _collect_info(self, path: Path) -> dict:
        absolute = path.resolve(strict=False)
        exists = path.exists()
        size = path.stat().st_size if exists and path.is_file() else 0
        return {
            "path": str(absolute),
            "exists": exists,
            "size": size,
            "verified": exists,
        }

    def create_file(
        self,
        path: str,
        *,
        content: str = "",
        confirmed: bool = False,
        encoding: str = "utf-8",
    ) -> dict:
        target = self._normalize(path)
        denied = self.ensure_allowed(target, "создание", confirmed)
        if denied:
            return denied
        try:
            existed = target.exists()
            self._write_content(target, content, append=False, encoding=encoding)
            info = self._collect_info(target)
            info.update(
                {
                    "ok": bool(info["verified"]),
                    "requires_confirmation": False,
                    "status": "updated" if existed else "created",
                }
            )
            return info
        except Exception as exc:
            logger.exception("Не удалось создать файл %s", target)
            return {
                "ok": False,
                "path": str(target.resolve(strict=False)),
                "requires_confirmation": False,
                "error": str(exc),
                "verified": False,
            }

    def write_text(self, path: str, content: str, *, confirmed: bool = False, encoding: str = "utf-8") -> dict:
        target = self._normalize(path)
        denied = self.ensure_allowed(target, "запись", confirmed)
        if denied:
            return denied
        try:
            self._write_content(target, content, append=False, encoding=encoding)
            info = self._collect_info(target)
            info.update(
                {
                    "ok": bool(info["verified"]),
                    "requires_confirmation": False,
                    "status": "overwritten",
                }
            )
            return info
        except Exception as exc:
            logger.exception("Не удалось выполнить запись в %s", target)
            return {
                "ok": False,
                "path": str(target.resolve(strict=False)),
                "requires_confirmation": False,
                "error": str(exc),
                "verified": False,
            }

    def append_text(self, path: str, content: str, *, confirmed: bool = False, encoding: str = "utf-8") -> dict:
        target = self._normalize(path)
        denied = self.ensure_allowed(target, "добавление", confirmed)
        if denied:
            return denied
        try:
            existed = target.exists()
            self._write_content(target, content, append=True, encoding=encoding)
            info = self._collect_info(target)
            info.update(
                {
                    "ok": bool(info["verified"]),
                    "requires_confirmation": False,
                    "status": "appended" if existed else "created",
                }
            )
            return info
        except Exception as exc:
            logger.exception("Не удалось дополнить файл %s", target)
            return {
                "ok": False,
                "path": str(target.resolve(strict=False)),
                "requires_confirmation": False,
                "error": str(exc),
                "verified": False,
            }

    def list_directory(self, path: str | None = None, *, confirmed: bool = False) -> dict:
        directory = self._default_root if path is None else self._normalize(path)
        denied = self.ensure_allowed(directory, "просмотр", confirmed)
        if denied:
            return denied
        try:
            if not directory.exists() or not directory.is_dir():
                raise FileNotFoundError(f"Каталог {directory} не найден")
            items = [
                item.name
                for item in sorted(directory.iterdir(), key=lambda candidate: candidate.name.lower())
                if not is_path_hidden(item)
            ]
            return {
                "ok": True,
                "path": str(directory.resolve(strict=False)),
                "items": items,
                "requires_confirmation": False,
                "verified": True,
            }
        except Exception as exc:
            logger.exception("Не удалось получить список каталога %s", directory)
            return {
                "ok": False,
                "path": str(directory.resolve(strict=False)),
                "requires_confirmation": False,
                "error": str(exc),
                "verified": False,
            }

    def open_path(self, path: str) -> dict:
        target = self._normalize(path)
        return open_path(str(target))

    def copy_path(self, src: str, dst: str, *, confirmed: bool = False) -> dict:
        source = self._normalize(src)
        destination = self._normalize(dst)
        denied = self.ensure_allowed(destination, "копирование", confirmed)
        if denied:
            return denied
        try:
            if source.is_dir():
                shutil.copytree(source, destination, dirs_exist_ok=True)
            else:
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source, destination)
            exists = destination.exists()
            return {
                "ok": exists,
                "path": str(destination.resolve(strict=False)),
                "exists": exists,
                "requires_confirmation": False,
                "verified": exists,
            }
        except Exception as exc:
            logger.exception("Не удалось скопировать %s в %s", source, destination)
            return {
                "ok": False,
                "path": str(destination.resolve(strict=False)),
                "requires_confirmation": False,
                "error": str(exc),
                "verified": False,
            }

    def move_path(self, src: str, dst: str, *, confirmed: bool = False) -> dict:
        source = self._normalize(src)
        destination = self._normalize(dst)
        denied = self.ensure_allowed(destination, "перемещение", confirmed)
        if denied:
            return denied
        try:
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(source, destination)
            exists = destination.exists()
            return {
                "ok": exists,
                "path": str(destination.resolve(strict=False)),
                "exists": exists,
                "requires_confirmation": False,
                "verified": exists,
            }
        except Exception as exc:
            logger.exception("Не удалось переместить %s в %s", source, destination)
            return {
                "ok": False,
                "path": str(destination.resolve(strict=False)),
                "requires_confirmation": False,
                "error": str(exc),
                "verified": False,
            }

    def delete_path(self, path: str, *, confirmed: bool = False) -> dict:
        target = self._normalize(path)
        denied = self.ensure_allowed(target, "удаление", confirmed)
        if denied:
            return denied
        try:
            if target.is_dir():
                shutil.rmtree(target, ignore_errors=False)
            elif target.exists():
                target.unlink()
            exists = target.exists()
            return {
                "ok": not exists,
                "path": str(target.resolve(strict=False)),
                "exists": exists,
                "requires_confirmation": False,
                "verified": not exists,
            }
        except Exception as exc:
            logger.exception("Не удалось удалить %s", target)
            return {
                "ok": False,
                "path": str(target.resolve(strict=False)),
                "requires_confirmation": False,
                "error": str(exc),
                "verified": False,
            }
