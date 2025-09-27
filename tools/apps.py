"""Управление приложениями Windows."""
from __future__ import annotations

import logging
import os
import platform
import subprocess
from dataclasses import dataclass
from typing import Dict

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
