"""Запуск настольных приложений."""

from __future__ import annotations

import logging
import os
import platform
import subprocess
from dataclasses import dataclass
from typing import Dict, Iterable, List, Optional, Tuple

try:  # pragma: no cover - rapidfuzz может быть недоступен в окружении
    from rapidfuzz import process as fuzz_process  # type: ignore
except ModuleNotFoundError:  # pragma: no cover
    fuzz_process = None  # type: ignore

import config

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class Application:
    key: str
    title: str
    command: str
    process_name: str
    aliases: Tuple[str, ...]


DEFAULT_APPLICATIONS: Dict[str, Dict[str, object]] = {
    "calc": {
        "title": "Калькулятор",
        "command": "calc.exe",
        "process_name": "ApplicationFrameHost.exe",
        "aliases": ("калькулятор", "посчитать", "calculator", "calc"),
    },
    "notepad": {
        "title": "Блокнот",
        "command": "notepad.exe",
        "process_name": "notepad.exe",
        "aliases": ("блокнот", "заметки", "notepad", "текстовый редактор"),
    },
    "word": {
        "title": "Microsoft Word",
        "command": "C:\\Program Files\\Microsoft Office\\root\\Office16\\WINWORD.EXE",
        "process_name": "WINWORD.EXE",
        "aliases": ("word", "ворд", "документ", "wordpad"),
    },
    "excel": {
        "title": "Microsoft Excel",
        "command": "C:\\Program Files\\Microsoft Office\\root\\Office16\\EXCEL.EXE",
        "process_name": "EXCEL.EXE",
        "aliases": ("excel", "ексель", "таблица", "таблицу"),
    },
    "chrome": {
        "title": "Google Chrome",
        "command": "C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe",
        "process_name": "chrome.exe",
        "aliases": ("chrome", "хром", "браузер", "google"),
    },
    "vscode": {
        "title": "Visual Studio Code",
        "command": "C:\\Users\\%USERNAME%\\AppData\\Local\\Programs\\Microsoft VS Code\\Code.exe",
        "process_name": "Code.exe",
        "aliases": ("vscode", "vs code", "visual studio code", "редактор кода", "код"),
    },
}

_APPLICATIONS: Dict[str, Application] = {}
_ALIAS_MAP: Dict[str, str] = {}


def _load_configured_apps() -> Dict[str, Dict[str, object]]:
    try:
        data = config.load_config("apps")
    except Exception:  # pragma: no cover - конфиг может отсутствовать
        return {}
    apps = data.get("apps") if isinstance(data, dict) else None
    return apps if isinstance(apps, dict) else {}


def _expand_command(command: str) -> str:
    return os.path.expandvars(command)


def _initialise() -> None:
    global _APPLICATIONS, _ALIAS_MAP
    merged: Dict[str, Dict[str, object]] = {**DEFAULT_APPLICATIONS, **_load_configured_apps()}
    applications: Dict[str, Application] = {}
    aliases: Dict[str, str] = {}
    for key, raw in merged.items():
        title = str(raw.get("title", key))
        command = str(raw.get("command", key))
        process_name = str(raw.get("process_name", ""))
        alias_values: Iterable[str] = raw.get("aliases", ()) if isinstance(raw, dict) else ()
        cleaned_aliases = [alias.strip().lower() for alias in alias_values if isinstance(alias, str) and alias.strip()]
        default_aliases = DEFAULT_APPLICATIONS.get(key, {}).get("aliases", ())  # type: ignore[arg-type]
        alias_set = {*(alias.lower() for alias in default_aliases or ()), *cleaned_aliases}
        alias_set.add(key.lower())
        alias_set.add(title.lower())
        applications[key] = Application(
            key=key,
            title=title,
            command=command,
            process_name=process_name,
            aliases=tuple(sorted(alias_set)),
        )
        for alias in applications[key].aliases:
            aliases[alias] = key
    _APPLICATIONS = applications
    _ALIAS_MAP = aliases


_initialise()


def reload() -> None:
    _initialise()


def get_known_apps() -> Dict[str, Application]:
    return dict(_APPLICATIONS)


def get_aliases() -> Dict[str, str]:
    return dict(_ALIAS_MAP)


def _match_alias(name: str) -> Optional[str]:
    lowered = name.strip().lower()
    if not lowered:
        return None
    if lowered in _ALIAS_MAP:
        return _ALIAS_MAP[lowered]
    if not fuzz_process:
        return None
    choices = list(_ALIAS_MAP.keys())
    if not choices:
        return None
    best = fuzz_process.extractOne(lowered, choices)
    if not best:
        return None
    alias, score, *_ = best
    return _ALIAS_MAP.get(alias) if score >= 75 else None


def launch(name_or_alias: str) -> dict:
    key = _match_alias(name_or_alias)
    if not key:
        message = f"Не знаю, как открыть '{name_or_alias}'."
        return {"ok": False, "message": message}
    app = _APPLICATIONS.get(key)
    if not app:
        message = f"Не удалось найти маппинг приложения '{name_or_alias}'."
        return {"ok": False, "message": message}
    command = _expand_command(app.command)
    system = platform.system()
    if system == "Windows":
        try:
            subprocess.Popen(command, shell=False)  # noqa: S603
        except FileNotFoundError:
            return {"ok": False, "message": f"Файл программы не найден: {command}"}
        except Exception as exc:  # pragma: no cover - системные ошибки Windows
            return {"ok": False, "message": str(exc)}
    else:  # pragma: no cover - тестовые окружения
        logger.info("Имитируем запуск '%s' на платформе %s", app.title, system)
    return {"ok": True, "message": f"Приложение '{app.title}' запущено", "command": command}


def close(name_or_alias: str) -> dict:
    key = _match_alias(name_or_alias)
    if not key:
        return {"ok": False, "message": f"Не знаю, как закрыть '{name_or_alias}'"}
    app = _APPLICATIONS.get(key)
    if not app or not app.process_name:
        return {"ok": False, "message": f"Нет информации о процессе для '{name_or_alias}'"}
    system = platform.system()
    if system != "Windows":  # pragma: no cover - на тестовых ОС имитируем поведение
        logger.info("Имитируем закрытие '%s'", app.title)
        return {"ok": True, "message": f"Закрытие '{app.title}' инициировано"}
    result = subprocess.run(  # noqa: S603
        ["taskkill", "/IM", app.process_name, "/F"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="ignore",
    )
    if result.returncode != 0:
        return {"ok": False, "message": result.stderr.strip() or "Не удалось завершить процесс"}
    return {"ok": True, "message": f"Приложение '{app.title}' закрыто"}


def open_with_shell(path: str) -> Optional[subprocess.Popen[bytes]]:
    system = platform.system()
    if system == "Darwin":
        command = ["open", path]
    elif system == "Linux":
        command = ["xdg-open", path]
    else:
        return None
    try:
        return subprocess.Popen(command)  # noqa: S603
    except FileNotFoundError:  # pragma: no cover - редкий случай
        return None
