"""Управление приложениями Windows и вспомогательные методы запуска."""
from __future__ import annotations

import logging
import os
import platform
import subprocess
from dataclasses import dataclass
from typing import Dict, Optional

logger = logging.getLogger(__name__)


@dataclass
class Application:
    key: str
    title: str
    command: str
    process_name: str


class ApplicationManager:
    """Запуск и завершение приложений."""

    def __init__(self, applications: Dict[str, Dict[str, str]]):
        self._apps: Dict[str, Application] = {
            key: Application(key, data["title"], data["command"], data["process_name"]) for key, data in applications.items()
        }

    def _ensure_windows(self) -> None:
        if platform.system() != "Windows":
            raise EnvironmentError("Операции с приложениями доступны только на Windows")

    def launch(self, key: str) -> str:
        self._ensure_windows()
        if key not in self._apps:
            raise KeyError(f"Неизвестное приложение: {key}")
        app = self._apps[key]
        logger.info("Запуск приложения %s (%s)", app.title, app.command)
        expanded_command = os.path.expandvars(app.command)
        subprocess.Popen(expanded_command, shell=True)  # noqa: S603
        return f"Приложение '{app.title}' запущено"

    def close(self, key: str) -> str:
        self._ensure_windows()
        if key not in self._apps:
            raise KeyError(f"Неизвестное приложение: {key}")
        app = self._apps[key]
        logger.info("Закрытие приложения %s", app.title)
        result = subprocess.run(
            ["taskkill", "/IM", app.process_name, "/F"],  # noqa: S603,S607
            capture_output=True,
            text=True,
            encoding="utf-8",
        )
        if result.returncode != 0:
            logger.warning("taskkill вернул код %s: %s", result.returncode, result.stderr)
            return f"Не удалось завершить {app.title}: {result.stderr.strip()}"
        return f"Приложение '{app.title}' закрыто"

    def list_known(self) -> Dict[str, Application]:
        return self._apps


def open_with_shell(path: str) -> Optional[subprocess.Popen[bytes]]:
    """Открыть путь с помощью системных средств на POSIX-платформах.

    На Windows следует использовать :func:`os.startfile`, поэтому эта функция
    применяется только в средах Linux/macOS. Возвращает объект процесса или
    ``None``, если команда запуска не поддерживается.
    """

    system = platform.system()
    if system == "Darwin":
        command = ["open", path]
    elif system == "Linux":
        command = ["xdg-open", path]
    else:
        logger.debug("open_with_shell не поддерживает платформу %s", system)
        return None

    logger.info("Открытие пути через оболочку: %s", command)
    try:
        process = subprocess.Popen(command)  # noqa: S603
    except FileNotFoundError:
        logger.error("Команда для открытия пути не найдена: %s", command[0])
        return None
    return process
