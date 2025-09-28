"""Инструменты для работы с файловой системой."""
from __future__ import annotations

import logging
import os
import platform
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, List

from tools.apps import open_with_shell

logger = logging.getLogger(__name__)


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
        expanded = os.path.expandvars(path_str)
        expanded = os.path.expanduser(expanded)
        return Path(expanded).resolve(strict=False)

    def _normalize(self, path: str | os.PathLike[str]) -> Path:
        raw = str(path)
        expanded = os.path.expandvars(raw)
        expanded = os.path.expanduser(expanded)
        return Path(expanded).resolve(strict=False)

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

    def list_directory(self, path: str, confirmed: bool = False) -> dict:
        directory = self._normalize(path)
        self.ensure_allowed(directory, "просмотр", confirmed)
        self._operation_log("Список каталога", directory, confirmed)
        if not directory.exists() or not directory.is_dir():
            raise FileNotFoundError(f"Каталог {directory} не существует")
        items = sorted(item.name for item in directory.iterdir())
        logger.info("Каталог %s содержит %d элементов", directory, len(items))
        return {"ok": True, "path": str(directory), "items": items}

    def _resolve_shortcut(self, target: Path) -> Path:
        if platform.system() != "Windows" or target.suffix.lower() != ".lnk":
            return target
        try:
            import win32com.client  # type: ignore
        except ModuleNotFoundError:
            logger.warning("Не удалось импортировать win32com для обработки ярлыка %s", target)
            return target
        shell = win32com.client.Dispatch("WScript.Shell")  # type: ignore[attr-defined]
        shortcut = shell.CreateShortCut(str(target))
        resolved = Path(shortcut.Targetpath)
        return resolved.resolve(strict=False)

    def open_path(self, path: str) -> dict:
        target = self._normalize(path)
        self._operation_log("Открытие пути", target, confirmed=True)
        if not target.exists():
            logger.error("Путь %s не найден для открытия", target)
            return {"ok": False, "path": str(target), "error": "Путь не найден"}

        system = platform.system()
        to_open: Path = self._resolve_shortcut(target)
        if system == "Windows" and hasattr(os, "startfile"):
            os.startfile(str(to_open))  # type: ignore[attr-defined]
            logger.info("Путь открыт через os.startfile: %s", to_open)
        else:
            opened = open_with_shell(str(to_open))
            if opened is None:
                logger.error("Не удалось открыть путь %s на платформе %s", to_open, system)
                return {"ok": False, "path": str(to_open), "error": "Не удалось открыть путь"}
            logger.info("Путь открыт через оболочку: %s", to_open)
        return {"ok": True, "path": str(to_open.resolve(strict=False))}
