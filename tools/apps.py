"""Управление приложениями Windows и вспомогательные методы запуска."""
from __future__ import annotations

import logging
import os
import platform
import subprocess
from dataclasses import dataclass
from typing import Dict, Optional

logger = logging.getLogger(__name__)

try:  # pragma: no cover - rapidfuzz может отсутствовать в окружении тестов
    from rapidfuzz import fuzz  # type: ignore
except ModuleNotFoundError:  # pragma: no cover
    from difflib import SequenceMatcher

    class _FallbackFuzz:
        @staticmethod
        def partial_ratio(a: str, b: str) -> float:
            return SequenceMatcher(None, a, b).ratio() * 100

    fuzz = _FallbackFuzz()  # type: ignore


DEFAULT_APPLICATIONS: Dict[str, Dict[str, str]] = {
    "calc": {
        "title": "Калькулятор",
        "command": "calc.exe",
        "process_name": "Calculator.exe",
    }
}

DEFAULT_ALIASES: Dict[str, tuple[str, ...]] = {
    "calc": ("калькулятор", "посчитать", "calculator", "calc"),
    "notepad": ("блокнот", "заметки", "notepad", "текстовый редактор"),
    "excel": ("excel", "ексель", "таблицы", "таблицу"),
    "word": ("word", "ворд", "документы", "редактор"),
    "chrome": ("chrome", "хром", "браузер", "google"),
    "vscode": ("vscode", "vs code", "редактор кода", "код", "visual studio code"),
}


@dataclass
class Application:
    key: str
    title: str
    command: str
    process_name: str


class ApplicationManager:
    """Запуск и завершение приложений."""

    def __init__(self, applications: Dict[str, Dict[str, str]]):
        merged: Dict[str, Dict[str, str]] = {**DEFAULT_APPLICATIONS, **applications}
        self._apps: Dict[str, Application] = {
            key: Application(key, data["title"], data["command"], data["process_name"]) for key, data in merged.items()
        }
        self._alias_map: Dict[str, str] = {}
        self._build_alias_map()

    def _build_alias_map(self) -> None:
        alias_map: Dict[str, str] = {}
        for key, app in self._apps.items():
            alias_map[app.title.lower()] = key
            alias_map[key.lower()] = key
        for key, aliases in DEFAULT_ALIASES.items():
            if key not in self._apps:
                continue
            for alias in aliases:
                alias_map.setdefault(alias.lower(), key)
        self._alias_map = alias_map

    @property
    def alias_map(self) -> Dict[str, str]:
        return dict(self._alias_map)

    def resolve(self, name: str) -> Optional[str]:
        lowered = name.strip().lower()
        if not lowered:
            return None
        if lowered in self._alias_map:
            return self._alias_map[lowered]
        best_key: Optional[str] = None
        best_score = 0.0
        for alias, key in self._alias_map.items():
            score = fuzz.partial_ratio(lowered, alias) / 100.0
            if score > best_score:
                best_score = score
                best_key = key
        if best_score >= 0.65:
            return best_key
        return None

    def _ensure_windows(self) -> None:
        if platform.system() != "Windows":
            raise EnvironmentError("Операции с приложениями доступны только на Windows")

    def launch(self, key: str) -> str:
        self._ensure_windows()
        resolved = self.resolve(key) or key
        if resolved not in self._apps:
            raise KeyError(f"Неизвестное приложение: {key}")
        app = self._apps[resolved]
        logger.info("Запуск приложения %s (%s)", app.title, app.command)
        expanded_command = os.path.expandvars(app.command)
        subprocess.Popen(expanded_command, shell=True)  # noqa: S603
        return f"Приложение '{app.title}' запущено"

    def close(self, key: str) -> str:
        self._ensure_windows()
        resolved = self.resolve(key) or key
        if resolved not in self._apps:
            raise KeyError(f"Неизвестное приложение: {key}")
        app = self._apps[resolved]
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
