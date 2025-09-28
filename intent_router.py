"""Маршрутизация пользовательских запросов и запуск задач."""

from __future__ import annotations

import logging
import re
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

from config import load_config
from core.task_executor import compile_and_run
from core.task_schema import TaskRequest, TaskResult
from tools import apps as apps_module
from tools.apps import get_aliases, IndexedEntry

try:  # pragma: no cover - rapidfuzz может отсутствовать
    from rapidfuzz import fuzz  # type: ignore
except ModuleNotFoundError:  # pragma: no cover
    from difflib import SequenceMatcher

    class _FallbackFuzz:
        @staticmethod
        def partial_ratio(a: str, b: str) -> float:
            return SequenceMatcher(None, a, b).ratio() * 100

    fuzz = _FallbackFuzz()  # type: ignore
from tools.files import FileManager, get_desktop_path, FILE_TYPE_EXT, FILE_KIND_EXT
from tools import search as search_tools
from tools.llm_client import OllamaClient

logger = logging.getLogger(__name__)


KIND_BY_EXTENSION = {ext: kind for kind, ext in FILE_KIND_EXT.items()}
FILE_KIND_ALIASES = {
    alias: KIND_BY_EXTENSION.get(ext)
    for alias, ext in FILE_TYPE_EXT.items()
    if ext in KIND_BY_EXTENSION
}
FILE_REFERENCE_TOKENS = {
    "файл", "файла", "файлу", "файлом", "файле",
    "документ", "документа", "документу", "документом", "документе",
    "презентация", "презентацию", "презентации", "презентацией",
    "таблица", "таблицу", "таблицы", "таблицей",
    "document", "file", "presentation", "spreadsheet"
}
DEFAULT_KIND = "txt"
REWRITE_MARKERS = {"перепиш", "перезапиш", "замени"}

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
    info = manager.open_path(params["path"])
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
    preferred_browser: Optional[str] = None
    awaiting_browser_choice: bool = False
    available_browsers: tuple[str, ...] = field(default_factory=tuple)


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
    SEARCH_FILE_RE = re.compile(r"(?:найди|найти|поищи|ищи)\s+(?P<query>.+)", re.IGNORECASE)
    CLOSE_APP_RE = re.compile(r"(?:закрой(?:те)?|закрыть)\s+(?P<target>.+)", re.IGNORECASE)
    OPEN_GENERIC_RE = re.compile(r"(?:открой|покажи|запусти)\s+(?P<target>.+)", re.IGNORECASE)
    OPEN_BROWSER_RE = re.compile(r"(?:открой|запусти|запустить|открыть)\s+(?:.*\s)?браузер", re.IGNORECASE)
    URL_RE = re.compile(r"(https?://\S+|www\.\S+)", re.IGNORECASE)
    CONTENT_RE = re.compile(r"(?:с\s+текстом|контент|текст(?:ом)?)\s+(?P<value>.+)", re.IGNORECASE)
    TXT_PATH_PATTERN = r"(?P<path>\"[^\"]+?\\.txt\"|'[^']+?\\.txt'|«[^»]+?\\.txt»|[\w\s./\\:-]+?\\.txt)"
    GENERATE_APPEND_PATTERNS = (
        re.compile(
            rf"вставь\s+сгенерированн(?:ый|ого)\s+текст\s+в\s+{TXT_PATH_PATTERN}\s*[:：]\s*(?P<prompt>.+)",
            re.IGNORECASE,
        ),
        re.compile(
            rf"сгенерируй\s+текст(?:\s+про)?\s+(?P<prompt>.+?)\s+(?:и\s+)?(?:добавь|вставь|запиши|дополни)\s+(?:его\s+)?(?:в|к)\s+{TXT_PATH_PATTERN}",
            re.IGNORECASE,
        ),
        re.compile(
            rf"дополни\s+текстовый\s+файл\s+{TXT_PATH_PATTERN}\s+сгенерированн(?:ым|ого)\s+текстом(?:\s+про)?\s+(?P<prompt>.+)",
            re.IGNORECASE,
        ),
    )

    TYPE_KEYWORDS: Dict[str, str] = {
        "документ word": ".docx",
        "word": ".docx",
        "ворд": ".docx",
        "файл word": ".docx",
        "текстовый файл": ".txt",
        "текстовый документ": ".txt",
        "текстовая заметка": ".txt",
        "текстовая": ".txt",
        "текст": ".txt",
        "markdown": ".md",
        "маркдаун": ".md",
        "разметка markdown": ".md",
        "md": ".md",
    }

    NAME_STOPWORDS = {
        "под",
        "названием",
        "название",
        "назови",
        "файл",
        "файлик",
        "файла",
        "документ",
        "документы",
        "документа",
        "пожалуйста",
        "мне",
        "новый",
        "новую",
        "новое",
        "новая",
        "пустой",
        "пустую",
        "пустое",
        "пустая",
    }

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
        file_hint = any(re.search(rf"\b{re.escape(word)}\b", normalized) for word in self.FILE_HINTS)

        if normalized in {
            "пересканируй приложения",
            "пересканируй список приложений",
            "обнови список приложений",
            "обнови приложения",
        }:
            return {"intent": "refresh_apps"}

        create_data = self._parse_create_command(message)
        if create_data:
            return create_data

        edit_data = self._parse_edit_command(message)
        if edit_data:
            return edit_data

        generate_append = self._parse_generate_txt_command(message)
        if generate_append:
            return generate_append

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

        close_data = self._parse_close_app(message)
        if close_data:
            return close_data

        if self.OPEN_BROWSER_RE.search(message_core):
            return {"intent": "open_browser"}

        app = self._detect_app(normalized)
        if app:
            return {"intent": "open_app", "name": app}

        search_match = self.SEARCH_FILE_RE.search(message_core)
        if search_match and (file_hint or self._looks_like_path(search_match.group("query"))):
            query = search_match.group("query").strip()
            return {"intent": "search_file", "query": query}

        open_match = self.OPEN_GENERIC_RE.search(message_core)
        if open_match:
            target = open_match.group("target").strip()
            if self._looks_like_file_reference(target):
                return {"intent": "open_file", "query": target}

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
            return {"intent": "search_local", "query": query, "auto_open_first": False}

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
        patterns = (
            r'с\s+текстом\s+(?P<value>"[^"]*"|«.+?»|\'[^\']*\')',
            r'со\s+содержимым\s+(?P<value>"[^"]*"|«.+?»|\'[^\']*\')',
            r'с\s+содержанием\s+(?P<value>"[^"]*"|«.+?»|\'[^\']*\')',
            r'контент(?:ом)?\s+(?P<value>"[^"]*"|«.+?»|\'[^\']*\')',
            r'текст(?:ом)?\s+(?P<value>"[^"]*"|«.+?»|\'[^\']*\')',
            r"с\s+текстом\s+(?P<value>.+)",
            r"контент(?:ом)?\s+(?P<value>.+)",
            r"текст(?:ом)?\s+(?P<value>.+)",
        )
        for pattern in patterns:
            match = re.search(pattern, message, re.IGNORECASE)
            if match:
                return self._strip_quotes(match.group("value").strip())
        colon_split = re.split(r":\s+", message, maxsplit=1)
        if len(colon_split) == 2:
            return self._strip_quotes(colon_split[1].strip())
        return ""

    @staticmethod
    def _strip_quotes(value: str) -> str:
        trimmed = value.strip()
        if len(trimmed) >= 2 and trimmed[0] in {'"', "'", "«"}:
            closing = {'"': '"', "'": "'", "«": "»"}.get(trimmed[0], trimmed[0])
            if trimmed.endswith(closing):
                return trimmed[1:-1]
        if trimmed.endswith("»") and trimmed.startswith("«"):
            return trimmed[1:-1]
        return trimmed



    def _parse_create_command(self, message: str) -> Optional[Dict[str, Any]]:
        normalized_message = message.strip()
        if not re.search(r"созда[йте]", normalized_message, re.IGNORECASE):
            return None
        content = self._extract_content(message)
        kind = self._detect_kind(normalized_message)
        explicit_path = self._extract_explicit_path(message, kind)
        message_core = normalized_message.rstrip(" ?!.")
        if not explicit_path:
            direct = self.CREATE_RE.search(message_core)
            if direct:
                raw_path = direct.group("path").strip()
                if self._looks_like_path(raw_path):
                    explicit_path = raw_path
        if explicit_path:
            ext = Path(explicit_path).suffix.lower()
            if ext in KIND_BY_EXTENSION:
                kind = KIND_BY_EXTENSION[ext]
        if not explicit_path:
            ext = FILE_KIND_EXT.get(kind or DEFAULT_KIND, FILE_KIND_EXT[DEFAULT_KIND])
            desktop = get_desktop_path().resolve(strict=False)
            generated = desktop / f"new_{int(time.time())}{ext}"
            explicit_path = str(generated)
        return {
            "intent": "create_file",
            "path": explicit_path,
            "content": content,
            "kind": kind,
        }

    def _parse_edit_command(self, message: str) -> Optional[Dict[str, Any]]:
        if not re.search(r"(отредактируй|дополни|добавь)", message, re.IGNORECASE):
            return None
        kind = self._detect_kind(message)
        path = self._extract_explicit_path(message, kind)
        if not path:
            return None
        ext = Path(path).suffix.lower()
        if ext in KIND_BY_EXTENSION:
            kind = KIND_BY_EXTENSION[ext]
        content = self._extract_content(message)
        data: Dict[str, Any] = {
            "intent": "edit_file",
            "path": path,
            "content": content,
            "kind": kind,
        }
        cell = self._extract_cell_reference(message)
        if cell:
            data["cell"] = cell.upper()
        normalized = message.lower()
        if any(marker in normalized for marker in REWRITE_MARKERS):
            data["mode"] = "write"
        return data

    def _parse_generate_txt_command(self, message: str) -> Optional[Dict[str, Any]]:
        for pattern in self.GENERATE_APPEND_PATTERNS:
            match = pattern.search(message)
            if not match:
                continue
            raw_path = match.group("path").strip()
            prompt_raw = match.group("prompt").strip()
            path = self._strip_quotes(raw_path.strip())
            prompt = self._clean_generated_prompt(prompt_raw)
            if path and prompt:
                return {"intent": "generate_append_txt", "path": path, "prompt": prompt}
        return None

    def _detect_kind(self, message: str) -> Optional[str]:
        lowered = message.lower()
        for keyword, mapped in FILE_KIND_ALIASES.items():
            if mapped and re.search(rf"\b{re.escape(keyword)}\b", lowered):
                return mapped
        return None

    def _extract_cell_reference(self, message: str) -> Optional[str]:
        match = re.search(r"ячейк[аеуы]\s+(?P<cell>[A-Za-z]+\d+)", message, re.IGNORECASE)
        if match:
            return match.group("cell")
        return None

    def _parse_close_app(self, message: str) -> Optional[Dict[str, Any]]:
        match = self.CLOSE_APP_RE.search(message)
        if not match:
            return None
        target_raw = match.group("target")
        cleaned = re.sub(r"\bпожалуйста\b", "", target_raw, flags=re.IGNORECASE)
        cleaned = cleaned.strip().strip(".;,!?:")
        cleaned = self._strip_quotes(cleaned)
        if not cleaned:
            return None
        normalized = cleaned.lower()
        app_key = self._detect_app(normalized)
        if not app_key and normalized in self.app_aliases:
            app_key = self.app_aliases[normalized]
        return {"intent": "close_app", "name": app_key} if app_key else None

    def _clean_generated_prompt(self, prompt: str) -> str:
        cleaned = prompt.strip()
        cleaned = re.split(r"\s+(?:и\s+)?(?:добавь|вставь|запиши|дополни)\b", cleaned, maxsplit=1)[0]
        cleaned = re.sub(r"\s*(?:пожалуйста|спасибо)\.?$", "", cleaned, flags=re.IGNORECASE)
        return self._strip_quotes(cleaned.strip(" .\"'»«"))

    def _extract_explicit_path(self, message: str, kind: Optional[str] = None) -> Optional[str]:
        preferred_kind = kind.lower() if isinstance(kind, str) else None
        tokens = self._tokenize(message)
        keyword_set = set(FILE_REFERENCE_TOKENS) | {
            alias for alias, mapped in FILE_KIND_ALIASES.items() if mapped
        }
        for index, token in enumerate(tokens):
            cleaned = self._clean_token(token)
            if not cleaned:
                continue
            normalized = cleaned.lower()
            if normalized not in keyword_set:
                continue
            next_index = index + 1
            while next_index < len(tokens):
                candidate = self._clean_token(tokens[next_index])
                if not candidate:
                    next_index += 1
                    continue
                lowered = candidate.lower()
                if lowered in keyword_set:
                    next_index += 1
                    continue
                if self._looks_like_path(candidate):
                    return candidate
                if preferred_kind and not Path(candidate).suffix:
                    return candidate
                next_index += 1
        for token in tokens:
            candidate = self._clean_token(token)
            if candidate and self._looks_like_path(candidate):
                return candidate
        return None

    @staticmethod
    def _tokenize(message: str) -> List[str]:
        pattern = r'"[^"]*"|«[^»]+»|\'[^\']*\'|\S+'
        return [match.group(0) for match in re.finditer(pattern, message)]

    def _clean_token(self, token: str) -> str:
        stripped = token.strip().strip(",.;:")
        return self._strip_quotes(stripped)

    def _should_search_local(self, normalized: str) -> bool:
        return any(verb in normalized for verb in self.SEARCH_VERBS)

    def _should_search_web(self, normalized: str) -> bool:
        patterns = (
            r"\bв интернете\b",
            r"\bв сети\b",
            r"\bв гугле\b",
            r"\bнайди\s+(?:сайт|страницу)\b",
            r"\bпоиск в интернете\b",
        )
        return any(re.search(pattern, normalized) for pattern in patterns)

    def _looks_like_path(self, text: str) -> bool:
        lowered = text.lower()
        if any(symbol in lowered for symbol in ("\\", "/", ":")):
            return True
        return bool(re.search(r"\.[\w]{1,6}(?:\s|$)", lowered))

    def _looks_like_file_reference(self, text: str) -> bool:
        lowered = text.lower()
        if lowered.isdigit() or lowered in {"его", "ее", "её", "их"}:
            return True
        if any(lowered.startswith(prefix) for prefix in ("перв", "втор", "трет", "послед")):
            return True
        return self._looks_like_path(text)


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
        "edit_file": "редактирование",
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
        self.apps = apps_module
        self.APP_KEYWORDS: Dict[str, tuple[str, ...]] = self._build_app_keywords()
        self.llm = OllamaClient()
        self.browser_ids: tuple[str, ...] = ("chrome", "edge", "firefox")
        self.browser_aliases: Dict[str, tuple[str, ...]] = self._build_browser_aliases()

    def ask_llm(self, prompt: str, model: Optional[str] = None) -> str:
        chosen_model = model or getattr(self.llm, "default_model", None)
        answer = self.llm.generate(prompt, model=chosen_model)
        return answer if answer else "Модель не вернула ответ."

    def _ensure_session_state(self, state: dict | SessionState) -> SessionState:
        if isinstance(state, SessionState):
            return state
        session_state = state.get("session_state") if isinstance(state, dict) else None
        if not isinstance(session_state, SessionState):
            session_state = SessionState()
            if isinstance(state, dict):
                state["session_state"] = session_state
        return session_state

    def handle_message(
        self,
        text: str,
        session: AgentSession,
        state: dict,
        *,
        auto_confirm: bool | None = None,
        force_confirm: bool | None = None,
    ) -> Dict[str, Any]:
        try:
            message = text.strip()
            if not message:
                return self._make_response("Пустая команда.", ok=False)

            session_state = self._ensure_session_state(state)

            if session_state.last_results and time.time() - session_state.last_updated > 900:
                session_state.clear_results()

            confirmed_flag = bool(force_confirm) or bool(auto_confirm) or bool(getattr(session, "auto_confirm", False))

            normalized = message.lower().strip()
            normalized_clean = normalized.rstrip(" ?!.")

            if session.awaiting_browser_choice:
                choice = self._resolve_browser_choice(normalized, session.available_browsers or None)
                if choice:
                    return self._launch_browser(choice, session)
                options = self._browser_display_list(session.available_browsers or self.browser_ids)
                return self._make_response(f"Какой браузер открыть? Доступны: {options}", ok=False)

            context_response = self._handle_context_commands(
                message,
                normalized_clean,
                session,
                session_state,
                confirmed_flag,
            )
            if context_response:
                return context_response

            if normalized_clean in {"напиши путь до рабочего стола", "какой путь до рабочего стола"}:
                desktop = get_desktop_path().resolve(strict=False)
                return self._make_response(f"Рабочий стол: {desktop}", ok=True)

            if normalized_clean in {"какие файлы есть на рабочем столе", "покажи рабочий стол"}:
                intent_data: Optional[Dict[str, Any]] = {
                    "intent": "list_directory",
                    "path": str(get_desktop_path()),
                }
            else:
                intent_data = self.intent_inferencer.infer(message)

            if not intent_data:
                llm_response = self.ask_llm(message, model=getattr(session, "model", None))
                return self._make_response(llm_response, ok=True)

            intent = intent_data.pop("intent")
            if intent == "open_browser":
                intent_data.setdefault("utterance", message)
            return self._run_intent(intent, intent_data, session, session_state, confirmed_flag)
        except Exception as exc:  # pragma: no cover - защита от неожиданных ошибок
            logger.exception("Ошибка обработки сообщения: %s", exc)
            return self._make_response(f"Ошибка: {exc}", ok=False)

    def _handle_context_commands(
        self,
        message: str,
        normalized: str,
        session: AgentSession,
        session_state: SessionState,
        confirmed: bool,
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
            params = {"path": item, "confirmed": confirmed, "from_context": True}
            return self._run_intent("open_file", params, session, session_state, confirmed)
        if kind == "web":
            params = {"url": item, "from_context": True}
            return self._run_intent("open_web", params, session, session_state, confirmed)
        if kind == "app":
            params = {"name": item, "from_context": True}
            return self._run_intent("open_app", params, session, session_state, confirmed)
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

    def _resolve_file_kind(self, path: str, kind: Optional[str]) -> str:
        ext = Path(str(path)).suffix.lower()
        if ext in KIND_BY_EXTENSION:
            return KIND_BY_EXTENSION[ext]
        if isinstance(kind, str):
            key = kind.lower()
            if key in FILE_KIND_EXT:
                return key
            mapped = FILE_KIND_ALIASES.get(key)
            if mapped:
                return mapped
        return DEFAULT_KIND

    def _handle_search_file(self, params: Dict[str, Any], session_state: SessionState) -> Dict[str, Any]:
        query_raw = params.get("query")
        query = str(query_raw).strip() if isinstance(query_raw, str) else ""
        if not query:
            session_state.clear_results()
            return self._make_response("Не указан запрос для поиска.", ok=False, items=[])

        search_callable = getattr(search_tools, "search_files", None)
        if not callable(search_callable):  # pragma: no cover - fallback для старых версий
            search_callable = getattr(search_tools, "search_local")

        max_results = params.get("max_results")
        kwargs: Dict[str, Any] = {"whitelist": list(self.whitelist)}
        if isinstance(max_results, int) and max_results > 0:
            kwargs["max_results"] = max_results
        try:
            results = search_callable(query, **kwargs)
        except TypeError:  # pragma: no cover - совместимость сигнатур
            results = search_callable(query)

        normalized = [str(item) for item in results if item]
        if not normalized:
            session_state.clear_results()
            return self._make_response("Ничего не найдено.", ok=False, items=[])

        session_state.set_results(normalized, "file")
        lines = [f"{idx + 1}) {entry}" for idx, entry in enumerate(normalized[:10])]
        display = "\n".join(lines)
        reply = f"Нашёл:\n{display}"
        return self._make_response(reply, ok=True, items=list(normalized))

    def _handle_open_file(self, params: Dict[str, Any], session_state: SessionState) -> Dict[str, Any]:
        raw_path = params.get("path") or params.get("query")
        if raw_path is None:
            return self._make_response("Не указан путь для открытия.", ok=False)

        token = str(raw_path).strip()
        if not token:
            return self._make_response("Не указан путь для открытия.", ok=False)

        lowered = token.lower()
        use_context = False
        if lowered.isdigit() or lowered in {"его", "ее", "её", "их"}:
            use_context = True
        elif any(lowered.startswith(prefix) for prefix in ("перв", "втор", "трет", "послед")):
            use_context = True

        target_path = token
        if use_context:
            candidates = session_state.get_results(kind="file")
            if not candidates:
                return self._make_response("Нет сохранённых результатов для открытия.", ok=False)
            index = self._resolve_context_index(token, len(candidates))
            if index is None:
                total = len(candidates)
                return self._make_response(
                    f"Выберите число от 1 до {total} или используйте 'первый/последний'.",
                    ok=False,
                )
            target_path = candidates[index]
            session_state.last_updated = time.time()

        info = self.file_manager.open_path(str(target_path))
        opened_path = str(info.get("path") or target_path)

        if info.get("ok"):
            return self._make_response(f"Открыл: {opened_path}", ok=True, data={"result": info})

        error_message = info.get("error") or "Неизвестная ошибка"
        return self._make_response(f"Не удалось открыть: {error_message}", ok=False, data={"result": info})

    def _handle_close_app(self, params: Dict[str, Any], session_state: SessionState) -> Dict[str, Any]:
        name_raw = params.get("name") or params.get("app") or params.get("target")
        if not name_raw:
            return self._make_response("Не указано приложение для закрытия.", ok=False)
        result = apps_module.close(str(name_raw))
        message = result.get("message") or "Не удалось закрыть приложение."
        if result.get("ok"):
            return self._make_response(message, ok=True, data={"result": result})
        return self._make_response(message, ok=False, data={"result": result})

    def _handle_refresh_apps(self) -> Dict[str, Any]:
        result = self.apps.refresh_index()
        if result.get("ok"):
            count = int(result.get("count", 0))
            self.intent_inferencer.app_aliases = get_aliases()
            message = f"Готово, найдено {count} приложений"
            return self._make_response(message, ok=True, data={"result": result})
        error = result.get("error") or "Не удалось обновить список приложений"
        return self._make_response(error, ok=False, data={"result": result})

    def _handle_open_app(
        self,
        params: Dict[str, Any],
        session_state: SessionState,
    ) -> Dict[str, Any]:
        name_raw = params.get("name") or params.get("app") or params.get("target")
        if name_raw is None:
            return self._make_response("Не указано приложение для открытия.", ok=False)
        query = str(name_raw).strip()
        if not query:
            return self._make_response("Не указано приложение для открытия.", ok=False)

        from_context = bool(params.get("from_context"))
        if from_context:
            result = self.apps.launch(query)
            return self._finalize_app_launch(result, session_state, from_context=True, fallback_name=query)

        ranked = self.apps.candidates(query, limit=5)
        if ranked:
            best = ranked[0]
            second_score = ranked[1].score if len(ranked) > 1 else 0.0
            confident = best.is_manual or (
                best.score >= 80
                and (
                    len(ranked) == 1
                    or best.is_manual
                    or second_score < best.score - 5
                )
            )
            if confident:
                result = self.apps.launch_entry(best)
                return self._finalize_app_launch(result, session_state, fallback_name=best.name)
        if ranked:
            names = [entry.name for entry in ranked[:5]]
            session_state.set_results(names, "app")
            listing = self._format_app_options(ranked[:5])
            reply = "Нашёл несколько приложений:\n" + listing + "\nНазовите номер или точное название."
            return self._make_response(reply, ok=False, items=names)

        result = self.apps.launch(query)
        if not result.get("ok") and result.get("error") == "ambiguous":
            raw_options = result.get("candidates") or []
            options = [str(name) for name in raw_options if name]
            if options:
                session_state.set_results(options, "app")
                listing = "\n".join(f"{idx + 1}) {name}" for idx, name in enumerate(options))
                reply = "Нашёл несколько приложений:\n" + listing + "\nНазовите номер или точное название."
                return self._make_response(reply, ok=False, items=options)
        return self._finalize_app_launch(result, session_state, fallback_name=query)

    def _finalize_app_launch(
        self,
        result: Dict[str, Any],
        session_state: SessionState,
        *,
        from_context: bool = False,
        fallback_name: Optional[str] = None,
    ) -> Dict[str, Any]:
        message = result.get("message") or result.get("error") or "Не удалось открыть приложение"
        ok = bool(result.get("ok"))
        if ok:
            launched = result.get("launched") or fallback_name
            if launched:
                if from_context:
                    session_state.last_updated = time.time()
                else:
                    session_state.set_results([str(launched)], "app")
        data = {"result": result}
        return self._make_response(message, ok=ok, data=data)

    @staticmethod
    def _format_app_options(options: List[IndexedEntry]) -> str:
        label_map = {
            "common": "общая папка",
            "user": "личная папка",
            "manual": "ручной список",
        }
        lines: List[str] = []
        for idx, entry in enumerate(options, start=1):
            if entry.is_manual:
                source = label_map["manual"]
            else:
                source = label_map.get(entry.source, entry.source)
            suffix = f" — {source}" if source else ""
            lines.append(f"{idx}) {entry.name}{suffix}")
        return "\n".join(lines)

    def _handle_generate_append_txt(
        self,
        params: Dict[str, Any],
        session: AgentSession,
        session_state: SessionState,
        confirmed: bool,
    ) -> Dict[str, Any]:
        path_value = params.get("path")
        prompt_value = params.get("prompt")
        if not path_value:
            return self._make_response("Не удалось определить файл для дополнения.", ok=False)
        if not prompt_value:
            return self._make_response("Не удалось понять запрос для генерации текста.", ok=False)
        try:
            model = getattr(session, "model", None)
            generated = self.llm.generate(str(prompt_value), model=model)
        except Exception as exc:  # pragma: no cover - внешние ошибки клиента LLM
            logger.exception("Ошибка генерации текста: %s", exc)
            return self._make_response(f"Ошибка генерации текста: {exc}", ok=False)
        if not generated or not str(generated).strip():
            return self._make_response("Модель не вернула текст для вставки.", ok=False)
        text_to_append = str(generated)
        if text_to_append and not text_to_append.endswith("\n"):
            text_to_append += "\n"
        info = self.file_manager.append_text(str(path_value), content=text_to_append, confirmed=confirmed)
        requires_confirmation = bool(info.get("requires_confirmation"))
        if not info.get("ok"):
            message = info.get("error") or "Не удалось обновить файл."
            return self._make_response(
                message,
                ok=False,
                data={"file": info},
                requires_confirmation=requires_confirmation,
            )
        destination = str(info.get("path") or self.file_manager.normalize(str(path_value)))
        session_state.set_results([destination], "file")
        reply = f"Я вставил сгенерированный текст в {destination}."
        return self._make_response(
            reply,
            ok=True,
            data={"file": info, "generated": text_to_append},
            requires_confirmation=requires_confirmation,
        )

    def _run_intent(
        self,
        intent: str,
        params: Dict[str, Any],
        session: AgentSession,
        session_state: SessionState,
        confirmed: bool,
    ) -> Dict[str, Any]:
        if intent == "qa/smalltalk":
            prompt = str(params.get("prompt") or params.get("text") or params.get("message") or "")
            if not prompt and session_state.last_results:
                prompt = session_state.last_results[0]
            answer = self.ask_llm(prompt or "", model=getattr(session, "model", None))
            return self._make_response(answer, ok=True)

        if intent == "open_browser":
            return self._handle_open_browser(session, params)

        if intent == "close_app":
            return self._handle_close_app(params, session_state)

        if intent == "refresh_apps":
            return self._handle_refresh_apps()

        if intent == "open_app":
            return self._handle_open_app(params, session_state)

        if intent == "search_file":
            return self._handle_search_file(params, session_state)

        if intent == "open_file":
            return self._handle_open_file(params, session_state)

        if intent == "generate_append_txt":
            return self._handle_generate_append_txt(params, session, session_state, confirmed)

        prepared, confirmation_response = self._prepare_params(intent, params, session, confirmed)
        if confirmation_response is not None:
            return confirmation_response

        if intent in {
            "create_file",
            "write_file",
            "append_file",
            "edit_file",
            "move_path",
            "copy_path",
            "delete_path",
            "list_directory",
        }:
            return self._handle_file_operation(intent, prepared, session_state)

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
        confirmed: bool,
    ) -> tuple[Dict[str, Any], Optional[Dict[str, Any]]]:
        prepared = dict(params)
        if intent in {"create_file", "write_file", "append_file", "edit_file", "open_file", "list_directory", "move_path", "copy_path", "delete_path"}:
            path_value = prepared.get("path")
            if isinstance(path_value, str) and path_value.endswith(":") and len(path_value) > 2:
                path_value = path_value.rstrip(":")
                prepared["path"] = path_value
            if path_value:
                target = self.file_manager.normalize(path_value)
            else:
                target = self.file_manager.default_root if intent == "list_directory" else None
            if target is not None:
                prepared["path"] = str(target)
                prepared["confirmed"] = confirmed
                if intent in {"move_path", "copy_path"}:
                    prepared.setdefault("src", str(target))
        if intent == "move_path":
            destination = prepared.get("destination") or prepared.get("dst") or prepared.get("to")
            if isinstance(destination, str) and destination.endswith(":") and len(destination) > 2:
                destination = destination.rstrip(":")
                prepared["destination"] = destination
            if destination:
                prepared["dst"] = str(self.file_manager.normalize(destination))
        if intent == "copy_path":
            destination = prepared.get("destination") or prepared.get("dst") or prepared.get("to")
            if isinstance(destination, str) and destination.endswith(":") and len(destination) > 2:
                destination = destination.rstrip(":")
                prepared["destination"] = destination
            if destination:
                prepared["dst"] = str(self.file_manager.normalize(destination))
        if intent in {"create_file", "write_file", "append_file", "edit_file", "open_file", "list_directory", "search_local"}:
            prepared["whitelist"] = list(self.whitelist)
        if intent == "search_local":
            prepared.setdefault("max_results", 10)
        if intent == "search_web":
            prepared.setdefault("max_results", 5)
        return prepared, None

    def _handle_file_operation(
        self,
        intent: str,
        params: Dict[str, Any],
        session_state: SessionState,
    ) -> Dict[str, Any]:
        confirmed = bool(params.get("confirmed"))
        path_value = params.get("path")
        try:
            if intent == "create_file":
                if not path_value:
                    raise ValueError("Не указан путь для создания файла")
                info = self.file_manager.create_file(
                    path_value,
                    content=str(params.get("content", "")),
                    kind=params.get("kind"),
                    confirmed=confirmed,
                )
            elif intent == "write_file":
                if not path_value:
                    raise ValueError("Не указан путь для записи файла")
                info = self.file_manager.write_text(
                    path_value,
                    str(params.get("content", "")),
                    confirmed=confirmed,
                )
            elif intent == "append_file":
                if not path_value:
                    raise ValueError("Не указан путь для добавления в файл")
                info = self.file_manager.append_text(
                    path_value,
                    str(params.get("content", "")),
                    confirmed=confirmed,
                )
            elif intent == "edit_file":
                if not path_value:
                    raise ValueError("Не указан путь для редактирования файла")
                file_kind = self._resolve_file_kind(str(path_value), params.get("kind"))
                content_value = str(params.get("content", ""))
                if file_kind == "docx":
                    info = self.file_manager.edit_word(
                        path_value,
                        content_value,
                        confirmed=confirmed,
                    )
                elif file_kind == "xlsx":
                    cell_ref = str(params.get("cell") or "A1")
                    info = self.file_manager.edit_excel(
                        path_value,
                        cell_ref,
                        content_value,
                        confirmed=confirmed,
                    )
                elif file_kind == "pptx":
                    info = self.file_manager.edit_pptx(
                        path_value,
                        content_value,
                        confirmed=confirmed,
                    )
                else:
                    target_path = str(path_value)
                    if not Path(target_path).suffix:
                        target_path = str(Path(target_path).with_suffix(FILE_KIND_EXT[DEFAULT_KIND]))
                    if params.get("mode") == "write":
                        info = self.file_manager.write_text(
                            target_path,
                            content_value,
                            confirmed=confirmed,
                        )
                    else:
                        info = self.file_manager.append_text(
                            target_path,
                            content_value,
                            confirmed=confirmed,
                        )
                    path_value = target_path
            elif intent == "list_directory":
                info = self.file_manager.list_directory(path_value, confirmed=confirmed)
            elif intent == "copy_path":
                source = params.get("src") or path_value
                destination = params.get("dst") or params.get("destination") or params.get("to")
                if not source or not destination:
                    raise ValueError("Не указаны исходный и целевой пути для копирования")
                info = self.file_manager.copy_path(str(source), str(destination), confirmed=confirmed)
            elif intent == "move_path":
                source = params.get("src") or path_value
                destination = params.get("dst") or params.get("destination") or params.get("to")
                if not source or not destination:
                    raise ValueError("Не указаны исходный и целевой пути для перемещения")
                info = self.file_manager.move_path(str(source), str(destination), confirmed=confirmed)
            elif intent == "delete_path":
                if not path_value:
                    raise ValueError("Не указан путь для удаления")
                info = self.file_manager.delete_path(path_value, confirmed=confirmed)
            else:  # pragma: no cover - защита от неподдерживаемых веток
                return self._make_response("Операция недоступна.", ok=False)
        except Exception as exc:
            logger.exception("Ошибка подготовки операции %s: %s", intent, exc)
            return self._make_response(f"Ошибка: {exc}", ok=False)

        if info.get("requires_confirmation"):
            target_path = info.get("path") or str(path_value or params.get("dst") or "")
            reply = f"Нужно подтверждение для операции по пути: {target_path} — ответьте «да»"
            return self._make_response(reply, ok=False, data={"result": info}, requires_confirmation=True)

        if not info.get("ok"):
            error_message = info.get("error") or "Не удалось выполнить файловую операцию"
            target_path = info.get("path") or str(path_value or params.get("dst") or "")
            reply = f"Ошибка: {error_message} ({target_path})"
            return self._make_response(reply, ok=False, data={"result": info})

        path_display = info.get("path") or str(path_value or params.get("dst") or "")
        extras: List[str] = []
        if "exists" in info:
            extras.append(f"exists={info['exists']}")
        if intent in {"create_file", "write_file", "append_file", "edit_file"} and "size" in info:
            extras.append(f"size={info['size']}")

        if intent == "list_directory":
            raw_items = info.get("items") if isinstance(info.get("items"), list) else []
            items = list(raw_items)
            listing = "\n".join(items) if items else "(пусто)"
            desktop_path = str(get_desktop_path().resolve(strict=False))
            label = "Рабочий стол" if str(path_display) == desktop_path else "Каталог"
            reply = f"{label}: {path_display}\n{listing}"
            return self._make_response(reply, ok=True, data={"result": info}, items=items or None)

        action_titles = {
            "create_file": "Создан файл",
            "write_file": "Записан файл",
            "append_file": "Дополнен файл",
            "edit_file": "Файл обновлён",
            "copy_path": "Скопировано",
            "move_path": "Перемещено",
            "delete_path": "Удалено",
        }
        prefix = action_titles.get(intent, "Операция выполнена")
        suffix = f" ({', '.join(extras)})" if extras else ""
        reply = f"{prefix}: {path_display}{suffix}"
        return self._make_response(reply, ok=True, data={"result": info})
    def _format_response(self, request: TaskRequest, result: TaskResult, session_state: SessionState) -> Dict[str, Any]:
        data = dict(result.data)
        if result.stdout and "stdout" not in data:
            data["stdout"] = result.stdout
        if result.stderr and "stderr" not in data:
            data["stderr"] = result.stderr
        items: Optional[List[Any]] = None
        reply_override: Optional[str] = None

        if result.ok:
            if request.intent == "search_local":
                results = data.get("results", [])
                if isinstance(results, list):
                    normalized = [str(item) for item in results]
                    session_state.set_results(normalized, "file")
                    items = list(normalized)
                    if normalized:
                        lines = [f"{idx + 1}) {entry}" for idx, entry in enumerate(normalized)]
                        reply_override = "Готово: Нашёл:\n" + "\n".join(lines)
                    else:
                        reply_override = "Готово: Ничего не найдено"
            elif request.intent == "open_file":
                path = request.params.get("path")
                if path and not request.params.get("from_context"):
                    session_state.set_results([str(path)], "file")
                elif request.params.get("from_context"):
                    session_state.last_updated = time.time()
                message = result.stdout.strip()
                if message:
                    reply_override = message
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

        if reply_override is not None:
            reply = reply_override
        elif result.ok:
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

    def _build_browser_aliases(self) -> Dict[str, tuple[str, ...]]:
        return {
            "chrome": ("chrome", "google chrome", "хром", "гугл", "google"),
            "edge": ("edge", "microsoft edge", "эдж", "msedge", "микрософт эдж"),
            "firefox": ("firefox", "mozilla firefox", "фаерфокс", "мозилла", "mozilla"),
            "yandex": ("yandex", "яндекс", "яндекс браузер"),
        }

    def _build_app_keywords(self) -> Dict[str, tuple[str, ...]]:
        aliases = get_aliases()
        mapping: Dict[str, set[str]] = {}
        for alias, key in aliases.items():
            mapping.setdefault(key, set()).add(alias)
        return {key: tuple(sorted(values)) for key, values in mapping.items()}

    def _available_browsers(self) -> List[str]:
        available: List[str] = []
        for browser_id in self.browser_ids:
            if apps_module.is_installed(browser_id):
                available.append(browser_id)
        return available

    def _browser_title(self, browser_id: str) -> str:
        apps = apps_module.get_known_apps()
        app = apps.get(browser_id)
        if app:
            return app.title
        mapping = {
            "chrome": "Google Chrome",
            "edge": "Microsoft Edge",
            "firefox": "Mozilla Firefox",
            "yandex": "Яндекс.Браузер",
        }
        return mapping.get(browser_id, browser_id.title())

    def _browser_display_list(self, browsers: Iterable[str]) -> str:
        titles = [self._browser_title(browser) for browser in browsers]
        return ", ".join(titles)

    def _resolve_browser_choice(self, message: str, allowed: Optional[Iterable[str]] = None) -> Optional[str]:
        normalized = message.lower().strip()
        candidates = list(allowed) if allowed is not None else list(self.browser_aliases.keys())
        for browser_id in candidates:
            aliases = self.browser_aliases.get(browser_id, ())
            for alias in aliases:
                if re.search(rf"\b{re.escape(alias.lower())}\b", normalized):
                    return browser_id
        return None

    def _launch_browser(self, browser_id: str, session: AgentSession) -> Dict[str, Any]:
        title = self._browser_title(browser_id)
        if not apps_module.is_installed(browser_id):
            session.awaiting_browser_choice = False
            session.available_browsers = tuple()
            if session.preferred_browser == browser_id:
                session.preferred_browser = None
            return self._make_response(f"Браузер {title} недоступен на этом компьютере.", ok=False)

        result = apps_module.launch(browser_id)
        if result.get("ok"):
            session.preferred_browser = browser_id
            session.awaiting_browser_choice = False
            session.available_browsers = tuple()
            reply = f"Открываю {title}."
            return self._make_response(reply, ok=True, data={"result": result})

        error_message = result.get("message") or result.get("error") or "Не удалось открыть браузер."
        return self._make_response(f"Не удалось открыть {title}: {error_message}", ok=False, data={"result": result})

    def _handle_open_browser(self, session: AgentSession, params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        utterance = ""
        if params and isinstance(params, dict):
            utterance = str(params.get("utterance") or params.get("message") or "").lower()

        initial_choice: Optional[str] = None
        if utterance:
            initial_choice = self._resolve_browser_choice(utterance)
            if initial_choice:
                session.preferred_browser = initial_choice

        preferred = session.preferred_browser
        if preferred and apps_module.is_installed(preferred):
            return self._launch_browser(preferred, session)
        if preferred and not apps_module.is_installed(preferred):
            session.preferred_browser = None

        available = self._available_browsers()
        if not available:
            session.awaiting_browser_choice = False
            session.available_browsers = tuple()
            return self._make_response("Не удалось найти установленный браузер.", ok=False)

        if len(available) == 1:
            return self._launch_browser(available[0], session)

        session.awaiting_browser_choice = True
        session.available_browsers = tuple(available)
        options = self._browser_display_list(available)
        reply = f"Какой браузер открыть? Доступны: {options}"
        return self._make_response(reply, ok=False)

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
