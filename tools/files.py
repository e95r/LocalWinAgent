"""Инструменты для работы с файловой системой."""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, List

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
        target = Path(os.path.expandvars(str(path))).expanduser()
        return target.resolve(strict=False)

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

    def read_text(self, path: str, confirmed: bool = False, encoding: str = "utf-8") -> str:
        target = self._normalize(path)
        self.ensure_allowed(target, "чтение", confirmed)
        logger.info("Чтение файла %s", target)
        return target.read_text(encoding=encoding)

    def write_text(self, path: str, content: str, confirmed: bool = False, encoding: str = "utf-8") -> None:
        target = self._normalize(path)
        self.ensure_allowed(target, "запись", confirmed)
        logger.info("Запись в файл %s", target)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding=encoding)

    def append_text(self, path: str, content: str, confirmed: bool = False, encoding: str = "utf-8") -> None:
        target = self._normalize(path)
        self.ensure_allowed(target, "добавление", confirmed)
        logger.info("Добавление текста в файл %s", target)
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("a", encoding=encoding) as handler:
            handler.write(content)

    def delete_path(self, path: str, confirmed: bool = False) -> None:
        target = self._normalize(path)
        self.ensure_allowed(target, "удаление", confirmed)
        if target.is_dir():
            logger.info("Удаление каталога %s", target)
            for child in target.glob("**/*"):
                if child.is_file():
                    child.unlink(missing_ok=True)
            target.rmdir()
        elif target.exists():
            logger.info("Удаление файла %s", target)
            target.unlink()
        else:
            logger.warning("Путь %s не найден для удаления", target)

    def list_directory(self, path: str, confirmed: bool = False) -> List[str]:
        target = self._normalize(path)
        self.ensure_allowed(target, "просмотр", confirmed)
        logger.info("Список каталога %s", target)
        if not target.exists() or not target.is_dir():
            raise FileNotFoundError(f"Каталог {target} не существует")
        return sorted(item.name for item in target.iterdir())
