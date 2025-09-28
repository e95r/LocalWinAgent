"""Маршрутизация пользовательских запросов и запуск задач."""

from __future__ import annotations

import logging
import re
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from config import load_config
from core.task_executor import compile_and_run
from core.task_schema import TaskRequest, TaskResult
from tools.apps import get_aliases

try:  # pragma: no cover - rapidfuzz может отсутствовать
    from rapidfuzz import fuzz  # type: ignore
except ModuleNotFoundError:  # pragma: no cover
    from difflib import SequenceMatcher

    class _FallbackFuzz:
        @staticmethod
        def partial_ratio(a: str, b: str) -> float:
            return SequenceMatcher(None, a, b).ratio() * 100

    fuzz = _FallbackFuzz()  # type: ignore
from tools.files import FileManager, get_desktop_path

logger = logging.getLogger(__name__)


CREATE_FILE_CODE = """
from tools.files import FileManager

def run(params):
    manager = FileManager(params["whitelist"])
    info = manager.create_file(params["path"], content=params.get("content", ""), confirmed=params.get("confirmed", False))
    stdout = f"Создан файл: {info[\"path\"]} (exists={info.get(\"exists\")}, size={info.get(\"size\")})"
    return {"ok": bool(info.get("ok")), "stdout": stdout, "stderr": "", "data": {"file": info}}
"""


WRITE_FILE_CODE = """
from tools.files import FileManager

def run(params):
    manager = FileManager(params["whitelist"])
    info = manager.write_text(params["path"], content=params.get("content", ""), confirmed=params.get("confirmed", False))
    stdout = f"Запись выполнена: {info[\"path\"]} (exists={info.get(\"exists\")}, size={info.get(\"size\")})"
    return {"ok": bool(info.get("ok")), "stdout": stdout, "stderr": "", "data": {"file": info}}
"""


APPEND_FILE_CODE = """
from tools.files import FileManager

def run(params):
    manager = FileManager(params["whitelist"])
    info = manager.append_text(params["path"], content=params.get("content", ""), confirmed=params.get("confirmed", False))
    stdout = f"Добавление выполнено: {info[\"path\"]} (exists={info.get(\"exists\")}, size={info.get(\"size\")})"
    return {"ok": bool(info.get("ok")), "stdout": stdout, "stderr": "", "data": {"file": info}}
"""


OPEN_PATH_CODE = """
from tools.files import FileManager

def run(params):
    manager = FileManager(params["whitelist"])
    info = manager.open_path(params["path"], confirmed=params.get("confirmed", False))
    stdout = info.get("reply", "")
    return {"ok": bool(info.get("ok")), "stdout": stdout, "stderr": info.get("error", ""), "data": {"result": info}}
"""


LIST_DIRECTORY_CODE = """
from tools.files import FileManager

def run(params):
    manager = FileManager(params["whitelist"])
    info = manager.list_directory(params.get("path"), confirmed=params.get("confirmed", False))
    items = info.get("items", [])
    listing = "\n".join(items) if items else "(пусто)"
    stdout = f"Каталог: {info.get(\"path\")}\\n{listing}"
    return {"ok": bool(info.get("ok")), "stdout": stdout, "stderr": "", "data": info}
"""


SEARCH_LOCAL_CODE = """
from tools.files import open_path
from tools.search import search_local

def run(params):
    results = search_local(
        params["query"],
        max_results=params.get("max_results", 10),
        whitelist=params.get("whitelist"),
        extensions=params.get("extensions"),
    )
    data = {"results": results}
    if not results:
        return {"ok": False, "stdout": "Ничего не найдено", "stderr": "", "data": data}
    if params.get("auto_open_first"):
        first = results[0]
        opened = open_path(first)
        data["opened"] = opened
        stdout = opened.get("reply", f"Открыто: {first}")
        return {
            "ok": bool(opened.get("ok", False)),
            "stdout": stdout,
            "stderr": opened.get("error", ""),
            "data": data,
        }
    lines = [f"{idx + 1}) {path}" for idx, path in enumerate(results)]
    stdout = "Нашёл:\\n" + "\\n".join(lines)
    return {"ok": True, "stdout": stdout, "stderr": "", "data": data}
"""


OPEN_APP_CODE = """
from tools.apps import launch

def run(params):
    result = launch(params["name"])
    message = result.get("message", "")
    return {"ok": bool(result.get("ok", False)), "stdout": message, "stderr": result.get("error", ""), "data": {"result": result}}
"""


SEARCH_WEB_CODE = """
from tools.web import open_site, search_web

def run(params):
    results = search_web(params["query"], max_results=params.get("max_results", 5))
    data = {"results": results}
    if not results:
        return {"ok": False, "stdout": "Ничего не найдено", "stderr": "", "data": data}
    if params.get("open_first"):
        first = results[0]
        opened = open_site(first["url"])
        data["opened"] = opened
        title = opened.get("title", first.get("title") or first.get("url"))
        stdout = f"Открыт сайт: {title}"
        return {"ok": bool(opened.get("ok", False)), "stdout": stdout, "stderr": opened.get("warning", ""), "data": data}
    lines = [f"{idx + 1}) {item['title']} — {item['url']}" for idx, item in enumerate(results)]
    stdout = "Нашёл сайты:\\n" + "\\n".join(lines)
    return {"ok": True, "stdout": stdout, "stderr": "", "data": data}
"""


OPEN_WEB_CODE = """
from tools.web import open_site

def run(params):
    opened = open_site(params["url"])
    title = opened.get("title", params["url"])
    stdout = f"Открыт сайт: {title}"
    return {"ok": bool(opened.get("ok", False)), "stdout": stdout, "stderr": opened.get("warning", ""), "data": {"result": opened}}
"""


CODE_BY_INTENT = {
    "create_file": CREATE_FILE_CODE,
    "write_file": WRITE_FILE_CODE,
    "append_file": APPEND_FILE_CODE,
    "open_file": OPEN_PATH_CODE,
    "list_directory": LIST_DIRECTORY_CODE,
    "search_local": SEARCH_LOCAL_CODE,
    "open_app": OPEN_APP_CODE,
    "search_web": SEARCH_WEB_CODE,
    "open_web": OPEN_WEB_CODE,
}


@dataclass(slots=True)
class PendingAction:
    description: str
    request: TaskRequest
    code: str


@dataclass(slots=True)
class AgentSession:
    auto_confirm: bool = False
    model: str = "llama3.1:8b"
    pending: Optional[PendingAction] = None
    session_id: str = field(default_factory=lambda: str(uuid.uuid4()))


@dataclass(slots=True)
class SessionState:
    last_results: List[str] = field(default_factory=list)
    last_kind: str = "none"
    last_updated: float = 0.0

    def set_results(self, results: List[str], kind: str) -> None:
        filtered = [item for item in results if item]
        if filtered:
            self.last_results = filtered
            self.last_kind = kind
            self.last_updated = time.time()
        else:
            self.clear_results()

    def clear_results(self) -> None:
        self.last_results = []
        self.last_kind = "none"
        self.last_updated = 0.0

    def get_results(self, kind: Optional[str] = None) -> List[str]:
        if kind and kind != self.last_kind:
            return []
        return list(self.last_results)


class IntentInferencer:
    CREATE_RE = re.compile(r"создай(?:те)?\s+(?:файл\s+)?(?P<path>[\w./\\:-]+)", re.IGNORECASE)
    WRITE_RE = re.compile(r"(?:запиши|перезапиши)\s+(?:в|во)\s+(?P<path>[\w./\\:-]+)", re.IGNORECASE)
    APPEND_RE = re.compile(r"(?:добавь|допиши)\s+(?:к|в)\s+(?P<path>[\w./\\:-]+)", re.IGNORECASE)
    LIST_RE = re.compile(r"(?:покажи|показать|список|открой)\s+(?:каталог|директорию|папк[ауи])\s*(?P<path>.+)?", re.IGNORECASE)
    OPEN_FILE_RE = re.compile(r"открой\s+(?:файл|документ|папк[ауи])\s+(?P<path>[\w./\\:-]+)", re.IGNORECASE)
    URL_RE = re.compile(r"(https?://\S+|www\.\S+)", re.IGNORECASE)
    CONTENT_RE = re.compile(r"(?:с\s+текстом|контент|текст(?:ом)?)\s+(?P<value>.+)", re.IGNORECASE)

    FILE_HINTS = {
        "файл",
        "файлик",
        "документ",
        "отчёт",
        "отчет",
        "заметка",
        "таблица",
        "каталог",
        "папка",
        "скрин",
        "скриншот",
        "фото",
        "фотограф",
        "картинка",
        "изображение",
        "видео",
    }
    SEARCH_VERBS = {
        "найди",
        "найти",
        "поищи",
        "поищем",
        "ищи",
        "показать",
        "покажи",
        "посмотри",
        "посмотреть",
        "нужен",
        "нужна",
        "нужны",
        "хочу",
    }
    WEB_HINTS = {
        "сайт",
        "страница",
        "страничка",
        "википедия",
        "wiki",
        "docs",
        "документация",
        "официальный",
        "в интернете",
        "в сети",
        "гугл",
        "google",
        "яндекс",
        "yandex",
        "bing",
        "бинг",
    }

    def __init__(self, app_aliases: Dict[str, str]):
        self.app_aliases = app_aliases

    def infer(self, message: str) -> Optional[Dict[str, Any]]:
        normalized = message.lower().strip()
        message_core = message.strip().rstrip(" ?!.")
        app = self._detect_app(normalized)
        if app:
            return {"intent": "open_app", "name": app}

        file_hint = any(word in normalized for word in self.FILE_HINTS)

        match = self.CREATE_RE.search(message_core)
        if match:
            path = match.group("path")
            content = self._extract_content(message_core)
            return {"intent": "create_file", "path": path, "content": content}

        match = self.WRITE_RE.search(message_core)
        if match:
            path = match.group("path")
            content = self._extract_content(message_core)
            return {"intent": "write_file", "path": path, "content": content}

        match = self.APPEND_RE.search(message_core)
        if match:
            path = match.group("path")
            content = self._extract_content(message_core)
            return {"intent": "append_file", "path": path, "content": content}

        match = self.LIST_RE.search(message_core)
        if match:
            path = match.group("path")
            return {"intent": "list_directory", "path": path.strip() if path else None}

        match = self.OPEN_FILE_RE.search(message_core)
        if match:
            return {"intent": "open_file", "path": match.group("path")}

        url_match = self.URL_RE.search(message_core)
        if url_match:
            return {"intent": "open_web", "url": url_match.group(0)}

        if self._should_search_web(normalized):
            query = self._clean_query(message) or message.strip()
            open_first = "найди" not in normalized or bool(re.search(r"найди\s+(?:сайт|страницу)", normalized))
            return {"intent": "search_web", "query": query, "open_first": open_first}

        should_local = self._should_search_local(normalized)
        if should_local or file_hint:
            query = self._clean_query(message) or message.strip()
            auto_open = ("найди" not in normalized) or file_hint
            return {"intent": "search_local", "query": query, "auto_open_first": auto_open}

        return None

    def _detect_app(self, normalized: str) -> Optional[str]:
        best_key: Optional[str] = None
        best_len = 0
        for alias, key in self.app_aliases.items():
            pattern = rf"\b{re.escape(alias)}\b"
            if re.search(pattern, normalized) and len(alias) > best_len:
                best_key = key
                best_len = len(alias)
        return best_key

    def _extract_content(self, message: str) -> str:
        match = self.CONTENT_RE.search(message)
        if match:
            return match.group("value").strip()
        if ":" in message:
            return message.split(":", 1)[1].strip()
        return ""

    def _clean_query(self, message: str) -> str:
        cleaned = re.sub(r"^(?:найди|найти|покажи|посмотри|посмотреть|ищи|мне\s+нужен|мне\s+нужна|нужен|нужна|нужны|хочу)\s+", "", message, flags=re.IGNORECASE)
        cleaned = re.sub(r"^(?:мне\s+)?(?:файл|документ|папку|каталог|скриншот|фото)\s+", "", cleaned, flags=re.IGNORECASE)
        return cleaned.strip()

    def _should_search_web(self, normalized: str) -> bool:
        return any(marker in normalized for marker in self.WEB_HINTS)

    def _should_search_local(self, normalized: str) -> bool:
        return any(verb in normalized for verb in self.SEARCH_VERBS)


class IntentRouter:
    CONTEXT_RESET = {"сбрось контекст", "очисти контекст", "сброс контекста"}
    CONTEXT_RE = re.compile(
        r"открой\s+(?P<value>его|ее|её|их|перв(?:ый|ую)?|втор(?:ой|ую)?|трет(?:ий|ью)?|последн(?:ий|ю)?|\d+)",
        re.IGNORECASE,
    )
    WORD_TO_INDEX = {
        "перв": 0,
        "втор": 1,
        "трет": 2,
    }

    FILE_ACTION_NAMES = {
        "create_file": "создание",
        "write_file": "запись",
        "append_file": "добавление",
        "open_file": "открытие",
        "list_directory": "просмотр",
    }

    def __init__(self) -> None:
        paths_config = load_config("paths")
        whitelist = paths_config.get("whitelist", [])
        if not isinstance(whitelist, list):
            whitelist = []
        self.whitelist: List[str] = [str(item) for item in whitelist]
        self.file_manager = FileManager(self.whitelist)
        self.intent_inferencer = IntentInferencer(get_aliases())
        self.APP_KEYWORDS: Dict[str, tuple[str, ...]] = self._build_app_keywords()

    def handle_message(self, message: str, session: AgentSession, session_state: SessionState) -> Dict[str, Any]:
        message = message.strip()
        if not message:
            return self._make_response("Пустая команда.", ok=False)

        if session_state.last_results and time.time() - session_state.last_updated > 900:
            session_state.clear_results()

        normalized = message.lower().strip()
        context_response = self._handle_context_commands(message, normalized, session, session_state)
        if context_response:
            return context_response

        if normalized in {"напиши путь до рабочего стола", "какой путь до рабочего стола"}:
            desktop = get_desktop_path().resolve(strict=False)
            return self._make_response(f"Рабочий стол: {desktop}", ok=True)

        if normalized in {"какие файлы есть на рабочем столе", "покажи рабочий стол"}:
            intent_data: Optional[Dict[str, Any]] = {
                "intent": "list_directory",
                "path": str(get_desktop_path()),
            }
        else:
            intent_data = self.intent_inferencer.infer(message)

        if not intent_data:
            return self._make_response("Не понял запрос. Попробуйте переформулировать.", ok=False)

        intent = intent_data.pop("intent")
        return self._run_intent(intent, intent_data, session, session_state)

    def _handle_context_commands(
        self,
        message: str,
        normalized: str,
        session: AgentSession,
        session_state: SessionState,
    ) -> Optional[Dict[str, Any]]:
        if normalized in self.CONTEXT_RESET:
            session_state.clear_results()
            return self._make_response("Контекст очищен.", ok=True)

        match = self.CONTEXT_RE.search(normalized)
        if not match:
            return None
        if not session_state.last_results:
            return self._make_response("Нет сохранённых результатов для открытия.", ok=False)
        token = match.group("value")
        index = self._resolve_context_index(token, len(session_state.last_results))
        if index is None:
            total = len(session_state.last_results)
            return self._make_response(f"Выберите число от 1 до {total} или используйте 'первый/последний'.", ok=False)
        item = session_state.last_results[index]
        kind = session_state.last_kind
        if kind == "file":
            params = {"path": item, "confirmed": session.auto_confirm, "from_context": True}
            return self._run_intent("open_file", params, session, session_state)
        if kind == "web":
            params = {"url": item, "from_context": True}
            return self._run_intent("open_web", params, session, session_state)
        if kind == "app":
            params = {"name": item, "from_context": True}
            return self._run_intent("open_app", params, session, session_state)
        return self._make_response("Контекст недоступен для повторного открытия.", ok=False)

    def _resolve_context_index(self, token: str, total: int) -> Optional[int]:
        token = token.lower()
        if token.isdigit():
            index = int(token) - 1
        elif token.startswith("последн"):
            index = total - 1
        elif token in {"его", "ее", "её", "их"}:
            index = 0
        else:
            for prefix, value in self.WORD_TO_INDEX.items():
                if token.startswith(prefix):
                    index = value
                    break
            else:
                return None
        if index < 0 or index >= total:
            return None
        return index

    def _run_intent(
        self,
        intent: str,
        params: Dict[str, Any],
        session: AgentSession,
        session_state: SessionState,
    ) -> Dict[str, Any]:
        prepared, confirmation_response = self._prepare_params(intent, params, session)
        if confirmation_response is not None:
            return confirmation_response
        code = CODE_BY_INTENT.get(intent)
        if not code:
            return self._make_response("Действие пока не поддерживается.", ok=False)
        request = TaskRequest(id=str(uuid.uuid4()), title=intent, intent=intent, params=prepared)
        result = compile_and_run(code, request.params)
        return self._format_response(request, result, session_state)

    def _prepare_params(
        self,
        intent: str,
        params: Dict[str, Any],
        session: AgentSession,
    ) -> tuple[Dict[str, Any], Optional[Dict[str, Any]]]:
        prepared = dict(params)
        if intent in {"create_file", "write_file", "append_file", "open_file", "list_directory"}:
            path_value = prepared.get("path")
            if path_value:
                target = self.file_manager.normalize(path_value)
            else:
                target = self.file_manager.default_root if intent == "list_directory" else None
            if target is not None:
                needs_confirmation = self.file_manager.requires_confirmation(target)
                confirmed = bool(prepared.get("confirmed")) or session.auto_confirm or not needs_confirmation
                if needs_confirmation and not confirmed:
                    action = self.FILE_ACTION_NAMES.get(intent, "операция")
                    reply = f"Нужно подтверждение: {action} {target}"
                    return prepared, self._make_response(reply, ok=False, requires_confirmation=True)
                prepared["confirmed"] = confirmed
                prepared["path"] = str(target)
        if intent in {"create_file", "write_file", "append_file", "open_file", "list_directory", "search_local"}:
            prepared["whitelist"] = list(self.whitelist)
        if intent == "search_local":
            prepared.setdefault("max_results", 10)
        if intent == "search_web":
            prepared.setdefault("max_results", 5)
        return prepared, None

    def _format_response(self, request: TaskRequest, result: TaskResult, session_state: SessionState) -> Dict[str, Any]:
        data = dict(result.data)
        if result.stdout and "stdout" not in data:
            data["stdout"] = result.stdout
        if result.stderr and "stderr" not in data:
            data["stderr"] = result.stderr
        items: Optional[List[Any]] = None

        if result.ok:
            if request.intent == "search_local":
                results = data.get("results", [])
                if isinstance(results, list):
                    session_state.set_results([str(item) for item in results], "file")
                    items = list(results)
            elif request.intent == "open_file":
                path = request.params.get("path")
                if path and not request.params.get("from_context"):
                    session_state.set_results([str(path)], "file")
                elif request.params.get("from_context"):
                    session_state.last_updated = time.time()
            elif request.intent == "search_web":
                raw_results = data.get("results", [])
                urls: List[str] = []
                display: List[str] = []
                if isinstance(raw_results, list):
                    for entry in raw_results:
                        if isinstance(entry, dict):
                            url = entry.get("url")
                            title = entry.get("title", url or "")
                            if url:
                                urls.append(str(url))
                            display.append(f"{title} — {url}" if title and url else title or url or "")
                    if urls:
                        session_state.set_results(urls, "web")
                        items = [item for item in display if item]
            elif request.intent == "open_web":
                url = data.get("result", {}).get("url") or request.params.get("url")
                if url and not request.params.get("from_context"):
                    session_state.set_results([str(url)], "web")
                elif request.params.get("from_context"):
                    session_state.last_updated = time.time()
            elif request.intent == "open_app":
                name = request.params.get("name")
                if name and not request.params.get("from_context"):
                    session_state.set_results([str(name)], "app")
                elif request.params.get("from_context"):
                    session_state.last_updated = time.time()
            elif request.intent == "list_directory":
                items = data.get("items") if isinstance(data.get("items"), list) else None
        else:
            if request.intent in {"search_local", "search_web"}:
                session_state.clear_results()

        if result.ok:
            reply_body = result.stdout.strip()
            reply = f"Готово: {reply_body}" if reply_body else "Готово."
        else:
            message = result.stderr.strip() or result.stdout.strip() or "Ошибка выполнения задачи."
            reply = f"Ошибка: {message}"

        return self._make_response(reply, ok=result.ok, data=data, items=items)

    @staticmethod
    def _make_response(
        reply: str,
        *,
        ok: bool,
        data: Optional[Dict[str, Any]] = None,
        items: Optional[List[Any]] = None,
        requires_confirmation: bool = False,
    ) -> Dict[str, Any]:
        response: Dict[str, Any] = {
            "reply": reply,
            "ok": ok,
            "requires_confirmation": requires_confirmation,
        }
        if data is not None:
            response["data"] = data
        if items is not None:
            response["items"] = items
        return response

    def _build_app_keywords(self) -> Dict[str, tuple[str, ...]]:
        aliases = get_aliases()
        mapping: Dict[str, set[str]] = {}
        for alias, key in aliases.items():
            mapping.setdefault(key, set()).add(alias)
        return {key: tuple(sorted(values)) for key, values in mapping.items()}

    def fuzzy_match(self, phrase: str, keywords: Dict[str, tuple[str, ...]]) -> Optional[str]:
        phrase_lower = phrase.lower()
        best_key: Optional[str] = None
        best_score = 0.0
        for key, variants in keywords.items():
            for variant in variants:
                score = fuzz.partial_ratio(phrase_lower, variant)
                if score > best_score:
                    best_score = score
                    best_key = key
        return best_key if best_score >= 65 else None
