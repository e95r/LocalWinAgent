"""Запуск настольных приложений и управление индексом меню «Пуск»."""

from __future__ import annotations

import logging
import os
import platform
import shlex
import shutil
import subprocess
from dataclasses import dataclass, replace
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

try:  # pragma: no cover - psutil может отсутствовать в окружении тестов
    import psutil
except ModuleNotFoundError:  # pragma: no cover
    psutil = None  # type: ignore

try:  # pragma: no cover - rapidfuzz может отсутствовать в окружении
    from rapidfuzz import process as fuzz_process  # type: ignore
except ModuleNotFoundError:  # pragma: no cover
    fuzz_process = None  # type: ignore

import config
from tools.app_indexer import AppIndexer

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class Application:
    key: str
    title: str
    command: str
    process_name: str
    aliases: Tuple[str, ...]


@dataclass(slots=True)
class IndexedEntry:
    name: str
    path: str
    args: str
    shortcut: str
    source: str
    score_boost: int = 0
    key: Optional[str] = None
    aliases: Tuple[str, ...] = ()
    command: Optional[str] = None
    is_manual: bool = False
    score: float = 0.0


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
    "edge": {
        "title": "Microsoft Edge",
        "command": "C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe",
        "process_name": "msedge.exe",
        "aliases": ("edge", "эдж", "браузер", "microsoft edge", "msedge"),
    },
    "yandex_browser": {
        "title": "Яндекс Браузер",
        "command": "C:\\Users\\%USERNAME%\\AppData\\Local\\Yandex\\YandexBrowser\\Application\\browser.exe",
        "process_name": "browser.exe",
        "aliases": (
            "яндекс",
            "яндекс браузер",
            "yandex",
            "yandex browser",
            "ya browser",
            "браузер яндекс",
        ),
    },
    "firefox": {
        "title": "Mozilla Firefox",
        "command": "C:\\Program Files\\Mozilla Firefox\\firefox.exe",
        "process_name": "firefox.exe",
        "aliases": ("firefox", "фаерфокс", "mozilla", "мозилла", "браузер"),
    },
    "vscode": {
        "title": "Visual Studio Code",
        "command": "C:\\Users\\%USERNAME%\\AppData\\Local\\Programs\\Microsoft VS Code\\Code.exe",
        "process_name": "Code.exe",
        "aliases": ("vscode", "vs code", "visual studio code", "редактор кода", "код"),
    },
    "photos": {
        "title": "Фотографии",
        "command": "",
        "process_name": "ApplicationFrameHost.exe",
        "aliases": (
            "фотографии",
            "просмотр фотографий",
            "photos",
            "photo",
            "viewer",
        ),
    },
}


def _load_configured_apps() -> Dict[str, Dict[str, object]]:
    try:
        data = config.load_config("apps")
    except Exception:  # pragma: no cover - конфиг может отсутствовать
        return {}
    apps = data.get("apps") if isinstance(data, dict) else None
    return apps if isinstance(apps, dict) else {}


def _expand_command(command: str) -> str:
    return os.path.expandvars(command)


def _resolve_command_path(command: str) -> Optional[str]:
    if not command:
        return None
    expanded = _expand_command(command)
    path = Path(expanded)
    if path.exists():
        return str(path)
    located = shutil.which(expanded)
    if located:
        return str(Path(located))
    if os.path.isabs(expanded):
        return str(Path(expanded)) if Path(expanded).exists() else None
    return shutil.which(command)


def _startfile(path: str) -> bool:
    starter = getattr(os, "startfile", None)
    if callable(starter):  # pragma: no cover - отсутствует вне Windows
        starter(path)  # type: ignore[attr-defined]  # noqa: S606
        return True
    return False


class ApplicationsManager:
    """Менеджер приложений и индекса меню «Пуск»."""

    def __init__(self, indexer: Optional[AppIndexer] = None) -> None:
        self.indexer = indexer or AppIndexer()
        self.manual_apps: Dict[str, Application] = {}
        self.alias_map: Dict[str, str] = {}
        self.manual_entries: List[IndexedEntry] = []
        self.index_entries: List[IndexedEntry] = []
        self.index: List[IndexedEntry] = []
        self.index_by_name: Dict[str, List[IndexedEntry]] = {}
        self._load_manual_config()
        self._init_index()

    # ------------------- публичные методы -------------------
    def reload(self) -> None:
        self._load_manual_config()
        cached = self.indexer.load_cache()
        if cached:
            self._apply_index_items(cached)
        else:
            self._apply_index_items([])

    def refresh_index(self) -> Dict[str, object]:
        try:
            items = self.indexer.scan()
        except Exception as exc:  # pragma: no cover - системные ошибки
            logger.exception("Ошибка сканирования меню 'Пуск': %s", exc)
            return {"ok": False, "error": str(exc)}
        self.indexer.save_cache(items)
        self._apply_index_items(items)
        return {"ok": True, "count": len(self.index_entries)}

    def list_indexed(self, limit: int = 20) -> List[str]:
        if limit is None or limit <= 0:
            entries = self.index_entries
        else:
            entries = self.index_entries[:limit]
        return [entry.name for entry in entries if entry.name]

    def get_known_apps(self) -> Dict[str, Application]:
        return dict(self.manual_apps)

    def get_aliases(self) -> Dict[str, str]:
        return dict(self.alias_map)

    def is_installed(self, app_id: str) -> bool:
        app = self.manual_apps.get(app_id)
        if not app:
            return False
        resolved = _resolve_command_path(app.command)
        return bool(resolved)

    def candidates(self, query: str, limit: int = 10) -> List[IndexedEntry]:
        token = query.strip().lower()
        if not token:
            return []
        if not self.index_by_name:
            return []
        keys = list(self.index_by_name.keys())
        matches: List[Tuple[str, float]] = []
        if fuzz_process:
            matches = [
                (name, float(score))
                for name, score, _ in fuzz_process.extract(token, keys, limit=limit)
            ]
        else:
            for key in keys:
                base = 100.0 if key == token else 0.0
                if not base and token in key:
                    base = 85.0
                if base:
                    matches.append((key, base))
            matches.sort(key=lambda item: item[1], reverse=True)
        combined: Dict[Tuple[str, str, str, str], IndexedEntry] = {}
        for key, score in matches:
            entries = self.index_by_name.get(key, [])
            for entry in entries:
                adjusted = score + float(entry.score_boost)
                identifier = (entry.name, entry.path, entry.shortcut, entry.command or "")
                candidate = replace(entry, score=adjusted)
                current = combined.get(identifier)
                if current is None or candidate.score > current.score:
                    combined[identifier] = candidate
        ranked = sorted(combined.values(), key=lambda item: item.score, reverse=True)
        if limit:
            return ranked[:limit]
        return ranked

    def launch(self, query_or_id: str) -> Dict[str, object]:
        if not query_or_id:
            return {"ok": False, "error": "Не указано приложение."}
        query = str(query_or_id)
        manual_key = self._match_manual(query)
        if manual_key:
            return self._launch_manual(manual_key)
        ranked = self.candidates(query, limit=5)
        if not ranked:
            return {"ok": False, "error": f"Не удалось найти приложение '{query}'."}
        best = ranked[0]
        if best.score < 80 and not best.is_manual:
            return {"ok": False, "error": f"Не удалось подобрать приложение под запрос '{query}'."}
        if len(ranked) > 1 and not best.is_manual:
            second = ranked[1]
            if second.score >= best.score - 5:
                return {
                    "ok": False,
                    "error": "ambiguous",
                    "candidates": [entry.name for entry in ranked[:5]],
                }
        return self.launch_entry(best)

    def launch_entry(self, entry: IndexedEntry) -> Dict[str, object]:
        if entry.is_manual and entry.key:
            return self._launch_manual(entry.key)
        return self._launch_indexed(entry)

    def close(self, name_or_alias: str) -> Dict[str, object]:
        if psutil is None:
            return {"ok": False, "message": "Управление процессами недоступно"}
        identifier = name_or_alias.strip().lower() if name_or_alias else ""
        if not identifier:
            return {"ok": False, "message": "Не указано приложение"}

        manual_key = self._match_manual(identifier) or (
            identifier if identifier in self.manual_apps else None
        )
        if manual_key:
            result = self._close_manual(manual_key)
            if result.get("ok"):
                return result

        entry_hints: List[str] = []
        ranked = self.candidates(identifier, limit=5)
        if ranked:
            entry_hints.extend(self._derive_process_hints(ranked[0]))
            if len(ranked) > 1:
                for entry in ranked[1:3]:
                    entry_hints.extend(self._derive_process_hints(entry))
        if entry_hints:
            result = self._close_by_process_names(entry_hints, ranked[0].name if ranked else name_or_alias)
            if result.get("ok"):
                return result

        result = self._close_by_process_query(identifier)
        if result.get("ok"):
            return result

        return {"ok": False, "message": "Не удалось найти запущенное приложение"}

    # ------------------- внутренние методы -------------------
    def _init_index(self) -> None:
        cached = self.indexer.load_cache()
        if not cached:
            cached = self.indexer.scan()
            if cached:
                self.indexer.save_cache(cached)
        self._apply_index_items(cached)

    def _load_manual_config(self) -> None:
        merged: Dict[str, Dict[str, object]] = {
            **DEFAULT_APPLICATIONS,
            **_load_configured_apps(),
        }
        applications: Dict[str, Application] = {}
        aliases: Dict[str, str] = {}
        for key, raw in merged.items():
            title = str(raw.get("title", key))
            command = str(raw.get("command", key))
            process_name = str(raw.get("process_name", ""))
            alias_values: Iterable[str] = raw.get("aliases", ()) if isinstance(raw, dict) else ()
            cleaned_aliases = [
                alias.strip().lower()
                for alias in alias_values
                if isinstance(alias, str) and alias.strip()
            ]
            default_aliases = DEFAULT_APPLICATIONS.get(key, {}).get("aliases", ())  # type: ignore[arg-type]
            alias_set = {
                *(alias.lower() for alias in default_aliases or ()),
                *cleaned_aliases,
            }
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
        self.manual_apps = applications
        self.alias_map = aliases
        self.manual_entries = self._build_manual_entries()
        self._rebuild_index_map()

    def _apply_index_items(self, items: Iterable[Dict[str, object]]) -> None:
        unique: Dict[Tuple[str, str, str], IndexedEntry] = {}
        for item in items or []:
            if not isinstance(item, dict):
                continue
            entry = self._make_index_entry(item)
            if not entry:
                continue
            key = (entry.name.lower(), entry.path, entry.shortcut)
            unique[key] = entry
        self.index_entries = list(unique.values())
        self.index = list(self.index_entries)
        self._rebuild_index_map()

    def _build_manual_entries(self) -> List[IndexedEntry]:
        entries: List[IndexedEntry] = []
        for app in self.manual_apps.values():
            resolved = _resolve_command_path(app.command) or ""
            entries.append(
                IndexedEntry(
                    name=app.title,
                    path=resolved,
                    args="",
                    shortcut="",
                    source="manual",
                    score_boost=25,
                    key=app.key,
                    aliases=app.aliases,
                    command=app.command,
                    is_manual=True,
                )
            )
        return entries

    def _make_index_entry(self, item: Dict[str, object]) -> Optional[IndexedEntry]:
        name = str(item.get("name", "")).strip()
        if not name:
            return None
        path = str(item.get("path", "") or "")
        args = str(item.get("args", "") or "")
        shortcut = str(item.get("shortcut", "") or "")
        source = str(item.get("source", "") or "user")
        score_boost = item.get("score_boost", 0)
        try:
            boost_value = int(score_boost)
        except (TypeError, ValueError):
            boost_value = 0
        return IndexedEntry(
            name=name,
            path=path,
            args=args,
            shortcut=shortcut,
            source=source,
            score_boost=boost_value,
        )

    def _rebuild_index_map(self) -> None:
        mapping: Dict[str, List[IndexedEntry]] = {}
        all_entries = [*self.manual_entries, *self.index_entries]
        for entry in all_entries:
            for key in self._search_keys(entry):
                mapping.setdefault(key, []).append(entry)
        self.index_by_name = mapping

    def _search_keys(self, entry: IndexedEntry) -> List[str]:
        keys = {entry.name.lower()}
        normalized = entry.name.lower().replace("-", " ")
        keys.add(normalized)
        compact = normalized.replace(" ", "")
        if compact:
            keys.add(compact)
        for alias in entry.aliases:
            key = alias.strip().lower()
            if key:
                keys.add(key)
                keys.add(key.replace(" ", ""))
        return [key for key in keys if key]

    def _match_manual(self, name: str) -> Optional[str]:
        lowered = name.strip().lower()
        if not lowered:
            return None
        if lowered in self.alias_map:
            return self.alias_map[lowered]
        if not fuzz_process:
            return None
        choices = list(self.alias_map.keys())
        if not choices:
            return None
        best = fuzz_process.extractOne(lowered, choices)
        if not best:
            return None
        alias, score, *_ = best
        return self.alias_map.get(alias) if score >= 75 else None

    def _launch_manual(self, key: str) -> Dict[str, object]:
        app = self.manual_apps.get(key)
        if not app:
            return {"ok": False, "error": "Приложение не найдено."}
        resolved = _resolve_command_path(app.command)
        if not resolved:
            message = f"Приложение '{app.title}' не установлено"
            return {"ok": False, "error": message, "message": message}
        system = platform.system()
        try:
            if system == "Windows" and os.path.isfile(resolved):
                _startfile(resolved)
            elif system == "Windows":
                subprocess.Popen([resolved])  # noqa: S603
            else:  # pragma: no cover - тестовые окружения
                logger.info("Имитируем запуск '%s' на платформе %s", app.title, system)
        except FileNotFoundError:
            message = f"Файл программы не найден: {resolved}"
            return {"ok": False, "error": message, "message": message}
        except Exception as exc:  # pragma: no cover - системные ошибки Windows
            return {"ok": False, "error": str(exc), "message": str(exc)}
        message = f"Приложение '{app.title}' запущено"
        return {"ok": True, "launched": app.title, "path": resolved, "message": message}

    def _launch_indexed(self, entry: IndexedEntry) -> Dict[str, object]:
        target_path = entry.path.strip()
        args = entry.args.strip()
        shortcut = entry.shortcut.strip()
        if target_path and not Path(target_path).exists():
            target_path = ""
        if target_path:
            return self._spawn_process(entry, target_path, args)
        if shortcut:
            return self._open_shortcut(entry.name, shortcut)
        return {
            "ok": False,
            "error": f"Не удалось запустить '{entry.name}'",
            "message": f"Не удалось запустить '{entry.name}'",
        }

    def _spawn_process(self, entry: IndexedEntry, executable: str, args: str) -> Dict[str, object]:
        command = [executable]
        if args:
            command.extend(shlex.split(args, posix=False))
        try:
            if platform.system() == "Windows":
                subprocess.Popen(command)  # noqa: S603
            else:  # pragma: no cover - для тестовых окружений
                logger.info("Имитируем запуск '%s': %s", entry.name, command)
        except FileNotFoundError:
            message = f"Файл программы не найден: {executable}"
            return {"ok": False, "error": message, "message": message}
        except Exception as exc:  # pragma: no cover
            return {"ok": False, "error": str(exc), "message": str(exc)}
        message = f"Приложение '{entry.name}' запущено"
        return {"ok": True, "launched": entry.name, "path": executable, "message": message}

    def _open_shortcut(self, name: str, shortcut: str) -> Dict[str, object]:
        try:
            if not _startfile(shortcut):
                subprocess.Popen([shortcut])  # noqa: S603
        except FileNotFoundError:
            message = f"Ярлык не найден: {shortcut}"
            return {"ok": False, "error": message, "message": message}
        except Exception as exc:  # pragma: no cover
            return {"ok": False, "error": str(exc), "message": str(exc)}

    def _close_manual(self, key: str) -> Dict[str, object]:
        app = self.manual_apps.get(key)
        if not app:
            return {"ok": False, "message": "Приложение не найдено"}
        process_name = app.process_name.strip()
        if not process_name:
            return {"ok": False, "message": "Не задан исполняемый файл для приложения"}
        result = self._close_by_process_names([process_name], app.title)
        if not result.get("ok"):
            message = result.get("message") or "Не удалось закрыть приложение"
            return {"ok": False, "message": message}
        return result

    def _close_by_process_names(self, process_names: Iterable[str], title: str) -> Dict[str, object]:
        assert psutil is not None  # для mypy/аннотаций
        targets = {name.lower() for name in process_names if name}
        if not targets:
            return {"ok": False, "message": "Не указаны процессы"}
        matched: List[Any] = []
        for process in psutil.process_iter(["name", "exe"]):
            try:
                candidates = self._process_tokens(process)
                if any(token in targets for token in candidates):
                    matched.append(process)
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue
        if not matched:
            return {"ok": False, "message": f"Не найден запущенный процесс для {title}"}
        return self._terminate_processes(matched, title)

    def _close_by_process_query(self, query: str) -> Dict[str, object]:
        assert psutil is not None
        matches = self._match_processes(query, limit=5)
        if not matches:
            return {"ok": False, "message": "Нет подходящих процессов"}
        top_score = matches[0][0]
        to_terminate = [process for score, process, _ in matches if score >= max(60.0, top_score - 10)]
        label = matches[0][2] or query
        return self._terminate_processes(to_terminate, label)

    def _terminate_processes(self, processes: Iterable[Any], title: str) -> Dict[str, object]:
        assert psutil is not None
        processes = list(processes)
        if not processes:
            return {"ok": False, "message": "Нет активных процессов"}
        to_wait: List[Any] = []
        for process in processes:
            try:
                process.terminate()
                to_wait.append(process)
            except (psutil.NoSuchProcess, psutil.AccessDenied) as exc:
                logger.warning("Не удалось завершить процесс %s: %s", getattr(process, "pid", "?"), exc)
        alive: List[Any]
        _, alive = psutil.wait_procs(to_wait, timeout=3)
        if alive:
            for process in alive:
                try:
                    process.kill()
                except (psutil.NoSuchProcess, psutil.AccessDenied) as exc:
                    logger.error("Не удалось принудительно завершить процесс %s: %s", getattr(process, "pid", "?"), exc)
            _, alive = psutil.wait_procs(alive, timeout=2)
        if alive:
            return {"ok": False, "message": f"Не удалось закрыть {title}"}
        return {"ok": True, "message": f"Закрыто: {title}"}

    def _derive_process_hints(self, entry: IndexedEntry) -> List[str]:
        hints: List[str] = []
        if entry.command:
            hints.append(Path(entry.command).name.lower())
        if entry.path:
            hints.append(Path(entry.path).name.lower())
        if entry.args:
            try:
                parts = shlex.split(entry.args, posix=False)
            except ValueError:
                parts = []
            for part in parts:
                if part.endswith(".exe"):
                    hints.append(Path(part).name.lower())
        hints.append(entry.name.lower())
        seen = set()
        ordered: List[str] = []
        for hint in hints:
            if hint and hint not in seen:
                ordered.append(hint)
                seen.add(hint)
        return ordered

    def _match_processes(self, query: str, limit: int = 5) -> List[Tuple[float, Any, str]]:
        assert psutil is not None
        needle = query.strip().lower()
        if not needle:
            return []
        matches: List[Tuple[float, Any, str]] = []
        for process in psutil.process_iter(["name", "exe", "cmdline"]):
            try:
                tokens = self._process_tokens(process)
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue
            best_score = 0.0
            best_token = ""
            for token in tokens:
                score = self._score_token(needle, token)
                if score > best_score:
                    best_score = score
                    best_token = token
            if best_score >= 50.0:
                matches.append((best_score, process, best_token))
        matches.sort(key=lambda item: item[0], reverse=True)
        if limit and len(matches) > limit:
            return matches[:limit]
        return matches

    def _process_tokens(self, process: Any) -> List[str]:
        assert psutil is not None
        tokens: List[str] = []
        info = getattr(process, "info", {}) or {}
        try:
            name = info.get("name") or process.name()
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            name = ""
        exe_path = info.get("exe")
        cmdline = info.get("cmdline")
        for value in (name, exe_path):
            if value:
                tokens.append(Path(str(value)).name.lower())
        if exe_path:
            tokens.append(Path(str(exe_path)).stem.lower())
        if isinstance(cmdline, (list, tuple)) and cmdline:
            first = cmdline[0]
            if first:
                tokens.append(Path(str(first)).name.lower())
        tokens = [token for token in tokens if token]
        return list(dict.fromkeys(tokens))

    def _score_token(self, query: str, token: str) -> float:
        if token == query:
            return 100.0
        if token.startswith(query):
            return 95.0
        if query in token:
            return 85.0
        if fuzz_process:
            match = fuzz_process.extractOne(query, [token])
            if match:
                return float(match[1])
        return SequenceMatcher(None, query, token).ratio() * 100.0


_MANAGER = ApplicationsManager()


def reload() -> None:
    _MANAGER.reload()


def refresh_index() -> Dict[str, object]:
    return _MANAGER.refresh_index()


def list_indexed(limit: int = 20) -> List[str]:
    return _MANAGER.list_indexed(limit=limit)


def get_known_apps() -> Dict[str, Application]:
    return _MANAGER.get_known_apps()


def get_aliases() -> Dict[str, str]:
    return _MANAGER.get_aliases()


def candidates(query: str, limit: int = 10) -> List[IndexedEntry]:
    return _MANAGER.candidates(query, limit=limit)


def launch(name_or_alias: str) -> Dict[str, object]:
    return _MANAGER.launch(name_or_alias)


def launch_entry(entry: IndexedEntry) -> Dict[str, object]:
    return _MANAGER.launch_entry(entry)


def is_installed(app_id: str) -> bool:
    return _MANAGER.is_installed(app_id)


def close(name_or_alias: str) -> Dict[str, object]:
    return _MANAGER.close(name_or_alias)


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


__all__ = [
    "ApplicationsManager",
    "Application",
    "IndexedEntry",
    "launch",
    "launch_entry",
    "candidates",
    "refresh_index",
    "list_indexed",
    "reload",
    "get_known_apps",
    "get_aliases",
    "is_installed",
    "close",
    "open_with_shell",
]
