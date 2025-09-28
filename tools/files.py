"""Инструменты для работы с файловой системой."""
from __future__ import annotations

import logging
import os
import platform
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, List

import config
from tools.apps import open_with_shell

logger = logging.getLogger(__name__)


def normalize_path(path: str | os.PathLike[str]) -> Path:
    """Нормализовать путь с учётом переменных окружения и пользователя."""

    expanded = os.path.expandvars(str(path))
    expanded = os.path.expanduser(expanded)
    return Path(expanded).resolve(strict=False)


def get_desktop_path() -> Path:
    """Вернуть путь к рабочему столу пользователя."""

    desktop = config._KNOWN.get("DESKTOP")  # pylint: disable=protected-access
    if desktop:
        return Path(desktop).resolve(strict=False)
    home = Path.home()
    return (home / "Desktop").resolve(strict=False)


def _is_hidden_or_system(path: Path) -> bool:
    """Проверить, является ли путь скрытым или системным."""

    name = path.name
    if not name:
        return False
    if name.lower() == "desktop.ini":
        return True
    if name.startswith("."):
        return True

    if platform.system() != "Windows":
        return False

    try:
        get_attrs = getattr(os, "getfileattributes", None)
        if get_attrs is not None:  # pragma: no cover - редкое использование
            attributes = get_attrs(str(path))
        else:
            import ctypes  # noqa: PLC0415

            attrs = ctypes.windll.kernel32.GetFileAttributesW(str(path))  # type: ignore[attr-defined]
            if attrs == -1:
                return False
            attributes = attrs
        return bool(attributes & 0x2) or bool(attributes & 0x4)
    except Exception:  # pragma: no cover - системные ошибки не критичны
        return False


def is_path_hidden(path: Path) -> bool:
    """Публичная обёртка для проверки скрытых путей."""

    return _is_hidden_or_system(path)


def _resolve_shortcut(target: Path) -> Path:
    """Разрешить путь ярлыка Windows."""

    if platform.system() != "Windows" or target.suffix.lower() != ".lnk":
        return target
    try:
        import win32com.client  # type: ignore
    except ModuleNotFoundError:  # pragma: no cover - win32com может отсутствовать
        logger.warning("Не удалось импортировать win32com для обработки ярлыка %s", target)
        return target
    shell = win32com.client.Dispatch("WScript.Shell")  # type: ignore[attr-defined]
    shortcut = shell.CreateShortCut(str(target))
    resolved = Path(shortcut.Targetpath)
    return resolved.resolve(strict=False)


def open_path(path: str) -> dict:
    """Открыть файл или каталог при помощи стандартного приложения ОС."""

    target = normalize_path(path)
    if not target.exists():
        reply = f"Путь не найден: {target}"
        return {"ok": False, "path": str(target), "error": "Путь не существует", "reply": reply}

    to_open = target
    if to_open.suffix.lower() == ".lnk":
        to_open = _resolve_shortcut(to_open)

    try:
        os.startfile(str(to_open))  # type: ignore[attr-defined]
    except AttributeError:  # pragma: no cover - не Windows
        opened = open_with_shell(str(to_open))
        if opened is None:
            reply = f"Не удалось открыть: {to_open}"
            return {"ok": False, "path": str(to_open), "error": "Не удалось открыть путь", "reply": reply}
        resolved = to_open.resolve(strict=False)
        reply = f"Открыто: {resolved}"
        return {"ok": True, "path": str(resolved), "reply": reply}
    except Exception as exc:  # pragma: no cover - системные ошибки Windows
        reply = f"Не удалось открыть: {to_open}"
        return {"ok": False, "path": str(to_open), "error": str(exc), "reply": reply}

    resolved = to_open.resolve(strict=False)
    reply = f"Открыто: {resolved}"
    return {"ok": True, "path": str(resolved), "reply": reply}


class ConfirmationRequiredError(PermissionError):
    """Ошибка, сигнализирующая о необходимости подтверждения."""

    def __init__(self, path: Path, action: str):
        super().__init__(f"Операция '{action}' требует подтверждения для пути: {path}")
        self.path = path
        self.action = action


@dataclass
class FileManager:
    whitelist: Iterable[str]

    def __post_init__(self) -> None:
        self._allowed_paths: List[Path] = [self._prepare_path(item) for item in self.whitelist]
        logger.debug("Белый список директорий: %s", self._allowed_paths)

    @staticmethod
    def _prepare_path(path_str: str) -> Path:
        return normalize_path(path_str)

    def _normalize(self, path: str | os.PathLike[str]) -> Path:
        return normalize_path(path)

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
            logger.warning("Операция %s для %s требует подтверждения", action, path)
            raise ConfirmationRequiredError(path, action)

    def _sync_write(self, target: Path, content: str, mode: str, encoding: str) -> None:
        with target.open(mode, encoding=encoding) as handler:
            handler.write(content)
            handler.flush()
            os.fsync(handler.fileno())

    def _operation_log(self, action: str, target: Path, confirmed: bool, suffix: str = "") -> None:
        logger.info("%s: %s (confirmed=%s)%s", action, target, confirmed, suffix)

    def read_text(self, path: str, confirmed: bool = False, encoding: str = "utf-8") -> str:
        target = self._normalize(path)
        self.ensure_allowed(target, "чтение", confirmed)
        self._operation_log("Чтение файла", target, confirmed)
        return target.read_text(encoding=encoding)

    def create_file(self, path: str, content: str = "", confirmed: bool = False, encoding: str = "utf-8") -> dict:
        target = self._normalize(path)
        self.ensure_allowed(target, "создание", confirmed)
        self._operation_log("Создание файла", target, confirmed)
        target.parent.mkdir(parents=True, exist_ok=True)
        existed_before = target.exists()
        if existed_before and not content:
            logger.debug("Файл %s уже существует, контент не передан", target)
        else:
            mode = "a" if existed_before else "w"
            self._sync_write(target, content, mode=mode, encoding=encoding)
        exists = target.exists()
        size = target.stat().st_size if exists else 0
        logger.info("Создание файла завершено: %s (exists=%s, size=%s)", target, exists, size)
        return {"ok": exists, "path": str(target), "exists": exists, "size": size}

    def write_text(self, path: str, content: str, confirmed: bool = False, encoding: str = "utf-8") -> dict:
        target = self._normalize(path)
        self.ensure_allowed(target, "запись", confirmed)
        self._operation_log("Запись в файл", target, confirmed)
        target.parent.mkdir(parents=True, exist_ok=True)
        self._sync_write(target, content, mode="w", encoding=encoding)
        exists = target.exists() and target.is_file()
        size = target.stat().st_size if exists else 0
        logger.info("Запись завершена: %s (exists=%s, size=%s)", target, exists, size)
        if not exists:
            raise FileNotFoundError(f"Файл {target} не найден после записи")
        return {"ok": True, "path": str(target), "exists": exists, "size": size}

    def append_text(self, path: str, content: str, confirmed: bool = False, encoding: str = "utf-8") -> dict:
        target = self._normalize(path)
        self.ensure_allowed(target, "добавление", confirmed)
        self._operation_log("Добавление в файл", target, confirmed)
        target.parent.mkdir(parents=True, exist_ok=True)
        existed_before = target.exists()
        self._sync_write(target, content, mode="a", encoding=encoding)
        exists = target.exists() and target.is_file()
        size = target.stat().st_size if exists else 0
        logger.info("Добавление завершено: %s (exists=%s, size=%s)", target, exists, size)
        if not exists:
            raise FileNotFoundError(f"Файл {target} не найден после добавления")
        if not existed_before:
            logger.warning("Файл %s был создан во время добавления", target)
        return {"ok": True, "path": str(target), "exists": exists, "size": size}

    def copy_path(self, src: str, dst: str, confirmed: bool = False) -> dict:
        source = self._normalize(src)
        destination = self._normalize(dst)
        self.ensure_allowed(destination, "копирование", confirmed)
        self._operation_log("Копирование", destination, confirmed, suffix=f" из {source}")
        if source.is_dir():
            shutil.copytree(source, destination, dirs_exist_ok=True)
        else:
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, destination)
        exists = destination.exists()
        logger.info("Копирование завершено: %s (exists=%s)", destination, exists)
        if not exists:
            raise FileNotFoundError(f"Путь {destination} не найден после копирования")
        return {"ok": exists, "path": str(destination), "exists": exists}

    def move_path(self, src: str, dst: str, confirmed: bool = False) -> dict:
        source = self._normalize(src)
        destination = self._normalize(dst)
        self.ensure_allowed(destination, "перемещение", confirmed)
        self._operation_log("Перемещение", destination, confirmed, suffix=f" из {source}")
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(source, destination)
        exists = destination.exists()
        logger.info("Перемещение завершено: %s (exists=%s)", destination, exists)
        if not exists:
            raise FileNotFoundError(f"Путь {destination} не найден после перемещения")
        return {"ok": exists, "path": str(destination), "exists": exists}

    def delete_path(self, path: str, confirmed: bool = False) -> dict:
        target = self._normalize(path)
        self.ensure_allowed(target, "удаление", confirmed)
        self._operation_log("Удаление", target, confirmed)
        if target.is_dir():
            shutil.rmtree(target, ignore_errors=False)
        elif target.exists():
            target.unlink()
        exists = target.exists()
        logger.info("Удаление завершено: %s (exists=%s)", target, exists)
        if exists:
            raise FileExistsError(f"Путь {target} не удалён")
        return {"ok": True, "path": str(target), "exists": False}

    def list_directory(self, path: str | None = None, confirmed: bool = False) -> dict:
        directory = Path.cwd().resolve(strict=False) if path is None else self._normalize(path)
        self.ensure_allowed(directory, "просмотр", confirmed)
        self._operation_log("Список каталога", directory, confirmed)
        if not directory.exists() or not directory.is_dir():
            raise FileNotFoundError(f"Каталог {directory} не существует")
        items = [
            item.name
            for item in sorted(directory.iterdir(), key=lambda candidate: candidate.name.lower())
            if not is_path_hidden(item)
        ]
        logger.info("Каталог %s содержит %d элементов", directory, len(items))
        return {"ok": True, "path": str(directory), "items": items}

    def open_path(self, path: str) -> dict:
        target = self._normalize(path)
        self._operation_log("Открытие пути", target, confirmed=True)
        result = open_path(str(target))
        if result.get("ok"):
            logger.info("Путь открыт: %s", result["path"])
        else:
            logger.error("Не удалось открыть путь %s: %s", target, result.get("error"))
        return result
