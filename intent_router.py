"""Маршрутизатор интентов для LocalWinAgent."""
from __future__ import annotations

import logging
import os
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Dict, Iterable, List, Literal, Mapping, Optional, Sequence, Tuple

try:  # pragma: no cover - rapidfuzz может отсутствовать в тестовой среде
    from rapidfuzz import fuzz  # type: ignore
except ModuleNotFoundError:  # pragma: no cover
    from difflib import SequenceMatcher

    class _FallbackFuzz:
        @staticmethod
        def partial_ratio(a: str, b: str) -> int:
            a_lower = a.lower()
            b_lower = b.lower()
            if not a_lower or not b_lower:
                return 0
            if len(a_lower) < len(b_lower):
                a_lower, b_lower = b_lower, a_lower
            best = 0.0
            window = len(b_lower)
            for start in range(len(a_lower) - window + 1):
                fragment = a_lower[start : start + window]
                ratio = SequenceMatcher(None, fragment, b_lower).ratio()
                if ratio > best:
                    best = ratio
                if best == 1.0:
                    break
            return int(best * 100)

    fuzz = _FallbackFuzz()  # type: ignore

from config import load_config
from tools.apps import ApplicationManager
from tools.files import ConfirmationRequiredError, FileManager, get_desktop_path, open_path
from tools.search import EverythingNotInstalledError, search_local
from tools.web import WebAutomation, open_site, search_web

logger = logging.getLogger(__name__)


@dataclass
class PendingAction:
    description: str
    callback: Callable[[bool], str]


@dataclass
class AgentSession:
    auto_confirm: bool = False
    model: str = "llama3.1:8b"
    pending: Optional[PendingAction] = None


@dataclass
class SessionState:
    last_results: List[str] = field(default_factory=list)
    last_kind: Literal["file", "app", "web", "none"] = "none"
    last_task: Literal["search_files", ""] = ""
    last_updated: float = field(default_factory=time.time)

    def set_results(
        self,
        results: List[str],
        task: Literal["search_files", ""] = "search_files",
        *,
        kind: Literal["file", "app", "web", "none"] = "file",
    ) -> None:
        self.last_results = list(results)
        self.last_task = task if results else ""
        self.last_kind = kind if results else "none"
        self.last_updated = time.time()

    def get_results(
        self, *, kind: Literal["any", "file", "app", "web"] = "any"
    ) -> List[str]:
        if self.last_results and time.time() - self.last_updated > 900:
            self.clear_results()
        if kind != "any" and self.last_kind != kind:
            return []
        return list(self.last_results)

    def clear_results(self) -> None:
        self.last_results = []
        self.last_task = ""
        self.last_kind = "none"
        self.last_updated = time.time()

    def clear(self) -> None:
        self.clear_results()


class IntentInferencer:
    """Определение вероятного интента пользователя по тексту."""

    RE_WORD = re.compile(r"[\w-]+", re.UNICODE)

    STOP_WORDS = {
        "а",
        "еще",
        "ещё",
        "в",
        "во",
        "это",
        "этот",
        "эта",
        "эту",
        "эти",
        "тот",
        "та",
        "то",
        "на",
        "надо",
        "нужно",
        "нужен",
        "нужна",
        "нужны",
        "мне",
        "пожалуйста",
        "пожалуй",
        "давай",
        "да",
        "нет",
        "хочу",
        "хотел",
        "хотела",
        "можно",
        "дайте",
        "дай",
        "глянь",
        "глянуть",
        "посмотри",
        "посмотреть",
        "посмотри-ка",
        "послушай",
        "послушать",
        "покажи",
        "показать",
        "открой",
        "открыть",
        "запусти",
        "запустить",
        "запуск",
        "просто",
        "это",
        "там",
        "его",
        "ее",
        "её",
        "их",
        "по",
        "из",
        "для",
        "как",
        "что",
        "какой",
        "какая",
        "какие",
        "тут",
        "вот",
        "бы",
        "быть",
        "плиз",
        "pls",
        "please",
        "можешь",
        "можешь",
        "могу",
        "можно",
        "сильно",
        "прям",
        "очень",
    }

    GENERIC_FILE_WORDS = {"файл", "файлы", "папка", "папку", "каталог", "документ", "документы"}

    SEARCH_MARKERS = {
        "найди",
        "найти",
        "поищи",
        "поищем",
        "ищи",
        "поиск",
        "хочу посмотреть в интернете",
        "посмотри в интернете",
        "искать",
        "отыщи",
    }

    NEGATIVE_SEARCH_MARKERS = {"не ищи", "не надо искать", "без интернета"}

    WEB_SEARCH_HINTS = {
        "в интернете",
        "в сети",
        "в гугле",
        "в google",
        "в яндексе",
        "в yandex",
        "в бинг",
        "в bing",
        "в вебе",
        "в сети",
    }

    WEB_KEYWORDS = {
        "документация",
        "страница",
        "сайт",
        "страничку",
        "википедия",
        "wiki",
        "docs",
        "официальный",
        "официальную",
        "блог",
        "мануал",
        "руководство",
        "форум",
        "гайд",
        "tutorial",
        "инструкция",
        "описание",
        "продукт",
        "release",
    }

    FILE_DOMAINS: Dict[str, Dict[str, Sequence[str]]] = {
        "documents": {
            "keywords": (
                "документ",
                "документы",
                "отчёт",
                "отчет",
                "смета",
                "invoice",
                "инвойс",
                "контракт",
                "презентация",
                "спецификация",
                "спека",
                "протокол",
                "план",
                "таблица",
                "заметка",
            ),
            "extensions": (".pdf", ".doc", ".docx", ".txt", ".rtf", ".xlsx", ".xls", ".ppt", ".pptx"),
            "default_terms": ("pdf", "docx", "xlsx"),
        },
        "images": {
            "keywords": (
                "фото",
                "фотку",
                "фотография",
                "картинка",
                "картинку",
                "изображение",
                "скрин",
                "скриншот",
                "снимок",
                "превью",
            ),
            "extensions": (".png", ".jpg", ".jpeg", ".bmp", ".gif", ".webp", ".svg"),
            "default_terms": ("png", "jpg", "screenshot"),
        },
        "audio": {
            "keywords": (
                "музыка",
                "музыку",
                "трек",
                "треков",
                "песня",
                "песню",
                "аудио",
                "sound",
            ),
            "extensions": (".mp3", ".wav", ".flac", ".aac", ".ogg"),
            "default_terms": ("mp3", "audio"),
        },
        "video": {
            "keywords": (
                "видео",
                "ролик",
                "фильм",
                "запись",
                "запусти видео",
                "клип",
            ),
            "extensions": (".mp4", ".mkv", ".avi", ".mov", ".wmv", ".webm"),
            "default_terms": ("mp4", "video"),
        },
        "archives": {
            "keywords": ("архив", "архивы", "backup", "бэкап"),
            "extensions": (".zip", ".rar", ".7z", ".tar", ".gz"),
            "default_terms": ("zip", "rar"),
        },
    }

    APP_EXTRA_ALIASES: Dict[str, Sequence[str]] = {
        "calc": ("калькулятор", "посчитать", "calculator", "calc"),
        "notepad": ("блокнот", "заметки", "notepad", "текстовый редактор"),
        "excel": ("excel", "ексель", "таблицы", "таблицу", "spreadsheet"),
        "word": ("word", "ворд", "документы word"),
        "chrome": ("браузер", "chrome", "хром", "google"),
        "vscode": ("vscode", "vs code", "редактор кода", "код", "visual studio code"),
    }

    def __init__(self, app_aliases: Mapping[str, str]):
        alias_map: Dict[str, str] = {}
        for alias, key in app_aliases.items():
            alias_map[alias.lower()] = key
        for key, phrases in self.APP_EXTRA_ALIASES.items():
            for phrase in phrases:
                alias_map.setdefault(phrase.lower(), key)
        self._app_aliases = alias_map

    @staticmethod
    def _merge_terms(terms: Iterable[str]) -> List[str]:
        seen: set[str] = set()
        ordered: List[str] = []
        for term in terms:
            cleaned = term.strip()
            if not cleaned or cleaned in seen:
                continue
            seen.add(cleaned)
            ordered.append(cleaned)
        return ordered

    def _tokenize(self, text: str) -> List[str]:
        return [match.group(0) for match in self.RE_WORD.finditer(text)]

    def _extract_keywords(self, tokens: Sequence[str]) -> List[str]:
        keywords: List[str] = []
        for token in tokens:
            lowered = token.lower()
            if lowered in self.STOP_WORDS:
                continue
            if lowered.isdigit():
                continue
            keywords.append(lowered)
        return keywords

    def _score_app(self, text: str) -> Tuple[Optional[str], float]:
        best_key: Optional[str] = None
        best_score = 0.0
        lowered = text.lower()
        for alias, key in self._app_aliases.items():
            if alias in lowered:
                score = 0.85 if len(alias) > 2 else 0.65
            else:
                score = fuzz.partial_ratio(lowered, alias) / 100.0
            if score > best_score:
                best_score = score
                best_key = key
        if best_score < 0.55:
            return None, 0.0
        return best_key, best_score

    def _score_file(
        self, tokens: Sequence[str], text: str, keywords: Sequence[str]
    ) -> Tuple[float, List[str], str]:
        best_score = 0.0
        best_terms: List[str] = []
        best_domain = "documents"
        lowered = text.lower()
        for domain, data in self.FILE_DOMAINS.items():
            domain_keywords = {item.lower() for item in data["keywords"]}
            hits = [token.lower() for token in tokens if token.lower() in domain_keywords]
            ext_hits = [ext for ext in data["extensions"] if ext in lowered]
            score = 0.0
            if hits:
                score = 0.6 + min(len(hits), 3) * 0.08
            elif ext_hits:
                score = 0.58
            if not score:
                continue
            score += min(len(ext_hits), 3) * 0.03
            filtered = [term for term in keywords if term not in self.GENERIC_FILE_WORDS]
            if not filtered:
                filtered = hits or list(keywords)
            combined = self._merge_terms(filtered + list(data.get("default_terms", ())))
            if score > best_score:
                best_score = score
                best_terms = combined
                best_domain = domain
        return best_score, best_terms, best_domain

    def _score_web(
        self,
        tokens: Sequence[str],
        text: str,
        keywords: Sequence[str],
        explicit_hint: bool,
    ) -> Tuple[float, List[str]]:
        lowered = text.lower()
        hits = [token.lower() for token in tokens if token.lower() in self.WEB_KEYWORDS]
        score = 0.0
        if hits:
            score = 0.6 + min(len(hits), 3) * 0.05
        if explicit_hint:
            score = max(score, 0.65)
        if re.search(r"https?://", lowered) or re.search(r"www\.", lowered):
            score = max(score, 0.7)
        if re.search(r"\b[a-z0-9-]+\.[a-z]{2,}\b", lowered):
            score = max(score, 0.65)
        if score == 0:
            return 0.0, []
        filtered = [term for term in keywords if term not in hits]
        if not filtered:
            filtered = list(keywords)
        return score, self._merge_terms(filtered)

    def _contains_phrase(self, text: str, phrases: Iterable[str]) -> bool:
        return any(phrase in text for phrase in phrases)

    def infer(self, text: str, ctx: SessionState) -> Dict[str, object]:
        normalized = text.strip().lower()
        if not normalized:
            return {"kind": "other", "query": "", "confidence": 0.0}

        tokens = self._tokenize(normalized)
        keywords = self._extract_keywords(tokens)
        explicit_search = self._contains_phrase(normalized, self.SEARCH_MARKERS)
        explicit_web_hint = self._contains_phrase(normalized, self.WEB_SEARCH_HINTS)
        if self._contains_phrase(normalized, self.NEGATIVE_SEARCH_MARKERS):
            explicit_search = False
            explicit_web_hint = False

        app_key, app_score = self._score_app(normalized)
        file_score, file_terms, file_domain = self._score_file(tokens, normalized, keywords)
        web_score, web_terms = self._score_web(tokens, normalized, keywords, explicit_web_hint or explicit_search)

        if app_key and app_score >= 0.8 and app_score >= max(file_score, web_score) + 0.1:
            return {"kind": "open_app", "query": app_key, "confidence": app_score}

        if explicit_search:
            if file_score >= web_score:
                query_terms = file_terms or keywords or tokens
                query = " ".join(query_terms) or normalized
                return {
                    "kind": "search_file",
                    "query": query.strip(),
                    "confidence": max(file_score, 0.55),
                    "domain": file_domain,
                }
            query_terms = web_terms or keywords or tokens
            query = " ".join(query_terms) or normalized
            return {
                "kind": "search_web",
                "query": query.strip(),
                "confidence": max(web_score, 0.55),
            }

        if app_key and app_score >= 0.75 and app_score >= max(file_score, web_score) + 0.05:
            return {"kind": "open_app", "query": app_key, "confidence": app_score}

        if file_score >= 0.62 and file_score >= web_score - 0.05:
            query_terms = file_terms or keywords or tokens
            query = " ".join(query_terms) or normalized
            return {
                "kind": "open_file",
                "query": query.strip(),
                "confidence": file_score,
                "domain": file_domain,
            }

        if web_score >= 0.62:
            query_terms = web_terms or keywords or tokens
            query = " ".join(query_terms) or normalized
            return {"kind": "open_web", "query": query.strip(), "confidence": web_score}

        if (
            file_score >= 0.55
            and web_score >= 0.55
            and abs(file_score - web_score) <= 0.12
        ):
            return {
                "kind": "other",
                "query": "",
                "confidence": 0.0,
                "clarify": ("file", "web"),
            }

        if app_key and app_score >= 0.6:
            return {"kind": "open_app", "query": app_key, "confidence": app_score}

        return {
            "kind": "other",
            "query": " ".join(keywords) if keywords else normalized,
            "confidence": 0.0,
        }


class LLMClient:
    """Простой клиент Ollama."""

    def __init__(self, default_model: str = "llama3.1:8b") -> None:
        self.default_model = default_model

    def chat(self, message: str, model: Optional[str] = None) -> str:
        try:
            import ollama  # type: ignore
        except ModuleNotFoundError:  # pragma: no cover - в CI может не быть ollama
            logger.error("Библиотека ollama не установлена")
            return "Установите Ollama (https://ollama.com/download) и модель llama3.1:8b."

        target_model = model or self.default_model
        try:
            response = ollama.chat(
                model=target_model,
                messages=[
                    {
                        "role": "system",
                        "content": "Ты дружелюбный локальный помощник Windows. Отвечай кратко и по делу.",
                    },
                    {"role": "user", "content": message},
                ],
            )
        except Exception as exc:  # pragma: no cover - зависит от локального окружения
            logger.exception("Ошибка общения с Ollama: %s", exc)
            return f"Не удалось получить ответ от модели {target_model}: {exc}"

        content = response.get("message", {}).get("content")
        if not content:
            return "Модель не вернула текст."
        return content.strip()


class IntentRouter:
    """Определение интентов и вызов инструментов."""

    CREATE_RE = re.compile(r"^(?:создай|создать)\s+файл\s+(.+)$", re.IGNORECASE)
    WRITE_RE = re.compile(r"^(?:запиши|записать)\s+в\s+(.+?)[\s]*[:：]\s*(.+)$", re.IGNORECASE | re.DOTALL)
    APPEND_RE = re.compile(r"^(?:добавь|добавить)\s+к\s+(.+?)[\s]*[:：]\s*(.+)$", re.IGNORECASE | re.DOTALL)
    OPEN_RE = re.compile(r"^(?:открой|открыть)\s+(?:файл|папку)?\s*(.+)$", re.IGNORECASE)
    MOVE_RE = re.compile(r"^(?:перемести|переместить)\s+(.+?)\s+в\s+(.+)$", re.IGNORECASE)
    COPY_RE = re.compile(r"^(?:скопируй|скопировать)\s+(.+?)\s+в\s+(.+)$", re.IGNORECASE)
    DELETE_RE = re.compile(r"^(?:удали|удалить)\s+(.+)$", re.IGNORECASE)
    LIST_RE = re.compile(r"^(?:покажи|список|list)\s+(?:каталог|папку)\s+(.+)$", re.IGNORECASE)
    RE_OPEN_PRONOUN = re.compile(r"^(открой|покажи)\s+(его|её|его\s+пожалуйста|этот|это)$", re.IGNORECASE)
    RE_OPEN_INDEX = re.compile(
        r"^(открой|покажи)\s+(?:ссылку\s+|файл\s+|результат\s+)?"
        r"(\d+|первый|первую|второй|вторую|третий|третью|четвертый|четвертую|последний|последнюю)$",
        re.IGNORECASE,
    )
    RE_RESET_CTX = re.compile(r"^(сбрось\s+контекст|очисти\s+память)$", re.IGNORECASE)

    APP_KEYWORDS: Dict[str, tuple[str, ...]] = {
        "notepad": ("блокнот", "текстовый редактор", "notepad"),
        "vscode": ("visual studio code", "вс код", "vscode", "код"),
        "word": ("word", "ворд", "microsoft word"),
        "excel": ("excel", "ексель", "microsoft excel"),
        "chrome": ("chrome", "хром", "google chrome", "браузер"),
    }

    CLOSE_KEYWORDS: Dict[str, tuple[str, ...]] = {key: tuple(value) for key, value in APP_KEYWORDS.items()}

    def __init__(self) -> None:
        paths_cfg = load_config("paths")
        apps_cfg = load_config("apps")
        web_cfg = load_config("web")

        self.file_manager = FileManager(paths_cfg.get("whitelist", []))
        self.app_manager = ApplicationManager(apps_cfg.get("apps", {}))
        self.intent_inferencer = IntentInferencer(self.app_manager.alias_map)
        self.web_automation = WebAutomation(
            browser=web_cfg.get("browser", "chromium"),
            headless=web_cfg.get("headless", False),
            implicit_wait_ms=int(web_cfg.get("implicit_wait_ms", 1500)),
        )
        self.llm_client = LLMClient()
        self.download_dir = paths_cfg.get("default_downloads")
        self.search_whitelist = [
            str(Path(os.path.expandvars(path)).expanduser().resolve(strict=False))
            for path in paths_cfg.get("whitelist", [])
        ]

    @staticmethod
    def _make_response(
        reply: str,
        *,
        ok: bool,
        requires_confirmation: bool = False,
        items: Optional[List[str]] = None,
    ) -> Dict[str, object]:
        data: Dict[str, object] = {
            "ok": ok,
            "reply": reply,
            "requires_confirmation": requires_confirmation,
        }
        if items is not None:
            data["items"] = items
        return data

    @staticmethod
    def _clean_path(text: str) -> str:
        return text.strip().strip('"')

    @staticmethod
    def _is_positive(text: str) -> bool:
        return text.strip().lower() in {"да", "подтверждаю", "конечно", "ок"}

    @staticmethod
    def _is_negative(text: str) -> bool:
        return text.strip().lower() in {"нет", "отмена", "не надо"}

    @staticmethod
    def fuzzy_match(text: str, patterns: Dict[str, tuple[str, ...]]) -> Optional[str]:
        text_lower = text.lower()
        for key, phrases in patterns.items():
            for phrase in phrases:
                if phrase in text_lower:
                    return key
        best_key: Optional[str] = None
        best_score = 0
        for key, phrases in patterns.items():
            for phrase in phrases:
                if len(phrase) <= 3:
                    continue
                score = fuzz.partial_ratio(text_lower, phrase)
                if score > best_score:
                    best_score = score
                    best_key = key
        if best_score >= 60:
            return best_key
        return None

    def _format_size(self, size: int) -> str:
        return f"{size} байт"

    def _execute_with_confirmation(
        self,
        session: AgentSession,
        path: str,
        action_name: str,
        performer: Callable[[bool], dict],
        formatter: Callable[[dict], str],
    ) -> Dict[str, object]:
        confirmed_flag = session.auto_confirm
        try:
            result = performer(confirmed_flag)
        except ConfirmationRequiredError:
            normalized = self.file_manager._normalize(path)
            if session.auto_confirm:
                result = performer(True)
                ok_flag = bool(result.get("ok", True))
                return self._make_response(
                    formatter(result),
                    ok=ok_flag,
                    requires_confirmation=False,
                )
            description = f"{action_name}: {normalized}"

            def _callback(_: bool) -> str:
                data = performer(True)
                return formatter(data)

            session.pending = PendingAction(description=description, callback=_callback)
            return self._make_response(
                f"Требуется подтверждение для пути: {normalized}. Скажите 'да' для подтверждения.",
                ok=False,
                requires_confirmation=True,
            )
        except Exception as exc:
            logger.exception("Ошибка при выполнении операции %s: %s", action_name, exc)
            return self._make_response(f"Ошибка: {exc}", ok=False)
        ok_flag = bool(result.get("ok", True))
        return self._make_response(formatter(result), ok=ok_flag)

    def _handle_create(self, message: str, session: AgentSession) -> Optional[Dict[str, object]]:
        match = self.CREATE_RE.match(message)
        if not match:
            return None
        raw_path = self._clean_path(match.group(1))

        def _perform(confirmed: bool) -> dict:
            return self.file_manager.create_file(raw_path, confirmed=confirmed)

        def _format(data: dict) -> str:
            size = data.get("size", 0)
            return f"Создан файл: {data['path']} (exists={data.get('exists')}, size={self._format_size(size)})"

        return self._execute_with_confirmation(session, raw_path, "создание файла", _perform, _format)

    def _handle_write(self, message: str, session: AgentSession) -> Optional[Dict[str, object]]:
        match = self.WRITE_RE.match(message)
        if not match:
            return None
        raw_path = self._clean_path(match.group(1))
        content = match.group(2)

        def _perform(confirmed: bool) -> dict:
            return self.file_manager.write_text(raw_path, content, confirmed=confirmed)

        def _format(data: dict) -> str:
            size = data.get("size", 0)
            return f"Записано в: {data['path']} (exists={data.get('exists')}, size={self._format_size(size)})"

        return self._execute_with_confirmation(session, raw_path, "запись файла", _perform, _format)

    def _handle_append(self, message: str, session: AgentSession) -> Optional[Dict[str, object]]:
        match = self.APPEND_RE.match(message)
        if not match:
            return None
        raw_path = self._clean_path(match.group(1))
        content = match.group(2)

        def _perform(confirmed: bool) -> dict:
            return self.file_manager.append_text(raw_path, content, confirmed=confirmed)

        def _format(data: dict) -> str:
            size = data.get("size", 0)
            return f"Добавлено в: {data['path']} (exists={data.get('exists')}, size={self._format_size(size)})"

        return self._execute_with_confirmation(session, raw_path, "добавление в файл", _perform, _format)

    def _handle_open(self, message: str, session: AgentSession) -> Optional[Dict[str, object]]:
        match = self.OPEN_RE.match(message)
        if not match:
            return None
        raw_path = self._clean_path(match.group(1))
        try:
            result = self.file_manager.open_path(raw_path)
        except Exception as exc:
            logger.exception("Ошибка открытия пути %s: %s", raw_path, exc)
            return self._make_response(f"Ошибка: {exc}", ok=False)
        if not result.get("ok", False):
            error = result.get("error", "Не удалось открыть путь")
            return self._make_response(f"Ошибка: {error}", ok=False)
        return self._make_response(f"Открыто: {result['path']}", ok=True)

    def _handle_move(self, message: str, session: AgentSession) -> Optional[Dict[str, object]]:
        match = self.MOVE_RE.match(message)
        if not match:
            return None
        src = self._clean_path(match.group(1))
        dst = self._clean_path(match.group(2))

        def _perform(confirmed: bool) -> dict:
            return self.file_manager.move_path(src, dst, confirmed=confirmed)

        def _format(data: dict) -> str:
            return f"Перемещено в: {data['path']} (exists={data.get('exists')})"

        return self._execute_with_confirmation(session, dst, "перемещение", _perform, _format)

    def _handle_copy(self, message: str, session: AgentSession) -> Optional[Dict[str, object]]:
        match = self.COPY_RE.match(message)
        if not match:
            return None
        src = self._clean_path(match.group(1))
        dst = self._clean_path(match.group(2))

        def _perform(confirmed: bool) -> dict:
            return self.file_manager.copy_path(src, dst, confirmed=confirmed)

        def _format(data: dict) -> str:
            return f"Скопировано в: {data['path']} (exists={data.get('exists')})"

        return self._execute_with_confirmation(session, dst, "копирование", _perform, _format)

    def _handle_delete(self, message: str, session: AgentSession) -> Optional[Dict[str, object]]:
        match = self.DELETE_RE.match(message)
        if not match:
            return None
        raw_path = self._clean_path(match.group(1))

        def _perform(confirmed: bool) -> dict:
            return self.file_manager.delete_path(raw_path, confirmed=confirmed)

        def _format(data: dict) -> str:
            return f"Удалено: {data['path']} (exists={data.get('exists')})"

        return self._execute_with_confirmation(session, raw_path, "удаление", _perform, _format)

    def _handle_list(self, message: str, session: AgentSession) -> Optional[Dict[str, object]]:
        match = self.LIST_RE.match(message)
        if not match:
            return None
        raw_path = self._clean_path(match.group(1))

        def _perform(confirmed: bool) -> dict:
            return self.file_manager.list_directory(raw_path, confirmed=confirmed)

        def _format(data: dict) -> str:
            items = ", ".join(data.get("items", [])) or "(пусто)"
            return f"Каталог: {data['path']} -> {items}"

        return self._execute_with_confirmation(session, raw_path, "просмотр каталога", _perform, _format)

    def _handle_context_commands(
        self, message: str, session_state: SessionState
    ) -> Optional[Dict[str, object]]:
        normalized = message.strip().lower()
        if self.RE_RESET_CTX.match(normalized):
            session_state.clear()
            return self._make_response("Контекст очищен.", ok=True)

        match_pronoun = self.RE_OPEN_PRONOUN.match(normalized)
        match_index = self.RE_OPEN_INDEX.match(normalized)
        if not match_pronoun and not match_index:
            return None

        results = session_state.get_results()
        if not results:
            return self._make_response(
                "Нет сохранённых результатов. Сначала попросите найти или открыть что-то конкретное.",
                ok=False,
            )

        index_map = {
            "первый": 1,
            "первую": 1,
            "второй": 2,
            "вторую": 2,
            "третий": 3,
            "третью": 3,
            "четвертый": 4,
            "четвертую": 4,
            "последний": len(results),
            "последнюю": len(results),
        }

        if match_index:
            raw_index = match_index.group(2).strip().lower()
            if raw_index in index_map:
                index = index_map[raw_index]
            else:
                try:
                    index = int(raw_index)
                except ValueError:
                    return self._make_response("Уточните номер.", ok=False)
        else:
            index = 1

        if index < 1 or index > len(results):
            return self._make_response(
                f"Выберите число от 1 до {len(results)}.",
                ok=False,
            )

        target = results[index - 1]
        session_state.last_updated = time.time()
        kind = session_state.last_kind

        if kind == "web":
            try:
                opened_url = open_site(target)
            except Exception as exc:  # pragma: no cover - зависит от окружения
                logger.exception("Не удалось открыть ссылку %s: %s", target, exc)
                return self._make_response(
                    f"Не удалось открыть ссылку: {target}",
                    ok=False,
                )
            reply = f"Открыл ссылку: {opened_url}"
        elif kind == "app":
            try:
                reply = self.app_manager.launch(target)
            except Exception as exc:  # pragma: no cover - запуск приложений зависит от ОС
                logger.exception("Не удалось запустить %s: %s", target, exc)
                return self._make_response(
                    f"Не получилось запустить {target}",
                    ok=False,
                )
        else:
            open_result = open_path(target)
            if not open_result.get("ok"):
                error = open_result.get("error", "Не удалось открыть путь")
                return self._make_response(
                    f"Не удалось открыть: {open_result.get('path', target)} (ошибка: {error})",
                    ok=False,
                )
            reply = open_result.get("reply") or f"Открыл: {open_result.get('path', target)}"

        if match_pronoun and len(results) > 1 and kind != "app":
            reply += "\nЕсли нужен другой результат, уточните номер."
        return self._make_response(reply, ok=True)

    def _handle_file_commands(self, message: str, session: AgentSession) -> Optional[Dict[str, object]]:
        for handler in (
            self._handle_create,
            self._handle_write,
            self._handle_append,
            self._handle_open,
            self._handle_move,
            self._handle_copy,
            self._handle_delete,
            self._handle_list,
        ):
            result = handler(message, session)
            if result is not None:
                return result
        return None

    def _handle_inferred_intent(
        self, message: str, session: AgentSession, session_state: SessionState
    ) -> Optional[Dict[str, object]]:
        inference = self.intent_inferencer.infer(message, session_state)
        if not inference:
            return None

        clarify = inference.get("clarify")
        if clarify:
            return self._make_response("Открыть как файл или как сайт?", ok=False)

        kind = inference.get("kind", "other")
        confidence = float(inference.get("confidence", 0.0))
        query = str(inference.get("query", "")).strip()

        if kind in {"open_file", "search_file", "open_web", "search_web"} and not query:
            return None

        if kind == "open_app":
            if confidence < 0.6:
                return None
            resolved = self.app_manager.resolve(query) or query
            try:
                reply = self.app_manager.launch(resolved)
            except KeyError:
                return self._make_response("Не знаю, как запустить это приложение.", ok=False)
            session_state.set_results([resolved], task="", kind="app")
            return self._make_response(reply, ok=True)

        if kind in {"open_file", "search_file"}:
            extensions = []
            domain = inference.get("domain")
            if domain and domain in IntentInferencer.FILE_DOMAINS:
                extensions = [ext.lower() for ext in IntentInferencer.FILE_DOMAINS[domain]["extensions"]]
            results = search_local(
                query,
                max_results=25,
                whitelist=self.search_whitelist or None,
                extensions=extensions or None,
            )
            if not results:
                session_state.clear_results()
                return self._make_response("Ничего подходящего не нашёл.", ok=False)
            session_state.set_results(results, task="search_files", kind="file")
            if kind == "search_file":
                lines = ["Нашёл следующие варианты:"]
                for idx, item in enumerate(results, 1):
                    lines.append(f"{idx}) {item}")
                lines.append("Скажите номер, чтобы открыть файл.")
                return self._make_response("\n".join(lines), ok=True, items=results)

            open_result = open_path(results[0])
            reply = open_result.get("reply") or f"Открыл: {results[0]}"
            if len(results) > 1:
                reply += "\nЕсли нужен другой вариант, назовите его номер."
            return self._make_response(reply, ok=open_result.get("ok", True), items=results)

        if kind in {"open_web", "search_web"}:
            try:
                found = search_web(query)
            except Exception as exc:  # pragma: no cover - зависит от сети
                logger.exception("Ошибка веб-поиска по запросу %s: %s", query, exc)
                return self._make_response(f"Не удалось выполнить веб-поиск: {exc}", ok=False)
            if not found:
                session_state.clear_results()
                return self._make_response("Не удалось найти подходящие ссылки.", ok=False)
            urls = [item[1] for item in found]
            session_state.set_results(urls, task="", kind="web")
            items = [f"{idx}) {title} — {url}" for idx, (title, url) in enumerate(found, 1)]
            if kind == "search_web" and confidence < 0.6:
                return self._make_response("\n".join(items), ok=True, items=urls)
            first_title, first_url = found[0]
            try:
                opened_url = open_site(first_url)
            except Exception as exc:  # pragma: no cover
                logger.exception("Не удалось открыть ссылку %s: %s", first_url, exc)
                return self._make_response(f"Не удалось открыть ссылку: {first_url}", ok=False)
            reply_lines = [f"Открываю {first_title}: {opened_url}"]
            if len(found) > 1:
                reply_lines.append("Другие результаты:")
                reply_lines.extend(items[1:])
                reply_lines.append("Скажите номер, чтобы открыть другую ссылку.")
            return self._make_response("\n".join(reply_lines), ok=True, items=urls)

        return None

    def detect_intent(self, text: str) -> Dict[str, str] | None:
        lowered = text.lower()
        if any(word in lowered for word in ("открой", "запусти")):
            app_key = self.fuzzy_match(lowered, self.APP_KEYWORDS)
            if app_key:
                return {"type": "open_app", "app": app_key}

        if "закрой" in lowered:
            app_key = self.fuzzy_match(lowered, self.CLOSE_KEYWORDS)
            if app_key:
                return {"type": "close_app", "app": app_key}

        if "найди" in lowered and "файл" in lowered:
            query = lowered.split("файл", 1)[1].strip()
            if not query:
                query = text
            return {"type": "search_file", "query": query}

        if "найди страницу" in lowered:
            start = lowered.index("найди страницу") + len("найди страницу")
            query = text[start:].strip()
            if query.startswith("и"):
                query = query[1:].strip()
            return {"type": "web_search", "query": query or text}

        if "найди" in lowered and "страницу" in lowered:
            try:
                start = lowered.index("найди") + len("найди")
                query = text[start:].replace("страницу", "", 1).strip()
            except ValueError:
                query = text
            return {"type": "web_search", "query": query or text}

        if "прочитай" in lowered and "файл" in lowered:
            match = re.search(r"([A-Za-z]:\\[^\s]+)", text)
            if match:
                return {"type": "read_file", "path": match.group(1)}

        if "модель" in lowered:
            for candidate in ("llama3.1:8b", "qwen2:7b"):
                if candidate in lowered:
                    return {"type": "switch_model", "model": candidate}

        return None

    def handle_message(
        self,
        message: str,
        session: AgentSession,
        session_state: SessionState,
        *,
        force_confirm: bool = False,
    ) -> Dict[str, object]:
        if force_confirm and session.pending:
            logger.debug("Принудительное подтверждение действия %s", session.pending.description)
            try:
                result = session.pending.callback(True)
            finally:
                session.pending = None
            return self._make_response(result, ok=True)

        if session.pending:
            if self._is_positive(message):
                try:
                    result = session.pending.callback(True)
                except Exception as exc:  # pragma: no cover - неожиданные ошибки при выполнении колбэка
                    logger.exception("Ошибка в подтверждённом действии: %s", exc)
                    reply = f"Ошибка: {exc}"
                    return_value = self._make_response(reply, ok=False)
                else:
                    reply = result
                    return_value = self._make_response(reply, ok=True)
                session.pending = None
                return return_value
            if self._is_negative(message):
                description = session.pending.description
                session.pending = None
                return self._make_response(f"Отменено: {description}", ok=False)
            return self._make_response(
                "Пожалуйста, ответьте 'да' или 'нет' для подтверждения.",
                ok=False,
                requires_confirmation=True,
            )

        message = message.strip()
        if not message:
            return self._make_response("Пустой запрос", ok=False)

        if session_state.last_results and time.time() - session_state.last_updated > 900:
            session_state.clear_results()

        context_response = self._handle_context_commands(message, session_state)
        if context_response is not None:
            return context_response

        normalized_lower = message.lower().strip()
        normalized_lower = normalized_lower.rstrip(" ?")

        if normalized_lower == "напиши путь до рабочего стола":
            desktop_path = get_desktop_path().resolve(strict=False)
            return self._make_response(f"Рабочий стол: {desktop_path}", ok=True)

        if normalized_lower == "какие файлы есть на рабочем столе":
            desktop_path = get_desktop_path().resolve(strict=False)
            try:
                data = self.file_manager.list_directory(str(desktop_path), confirmed=True)
            except FileNotFoundError:
                reply = f"Рабочий стол не найден: {desktop_path}"
                return self._make_response(reply, ok=False)
            except Exception as exc:  # pragma: no cover - защита от неожиданных ошибок
                logger.exception("Ошибка при получении списка рабочего стола: %s", exc)
                reply = f"Ошибка: {exc}"
                return self._make_response(reply, ok=False)
            else:
                items = ", ".join(data.get("items", [])) or "(пусто)"
                reply = f"Рабочий стол ({desktop_path}): {items}"
                return self._make_response(reply, ok=True, items=data.get("items"))

        file_result = self._handle_file_commands(message, session)
        if file_result is not None:
            return file_result

        inferred = self._handle_inferred_intent(message, session, session_state)
        if inferred is not None:
            return inferred

        intent = self.detect_intent(message)
        if not intent:
            logger.debug("Интент не распознан, обращение к LLM")
            reply = self.llm_client.chat(message, model=session.model)
            return self._make_response(reply, ok=True)

        intent_type = intent["type"]
        logger.info("Обнаружен интент: %s", intent_type)

        try:
            if intent_type == "open_app":
                result = self.app_manager.launch(intent["app"])
                return self._make_response(result, ok=True)

            if intent_type == "close_app":
                result = self.app_manager.close(intent["app"])
                return self._make_response(result, ok=True)

            if intent_type == "search_file":
                results = search_local(
                    intent["query"],
                    max_results=25,
                    whitelist=self.search_whitelist or None,
                )
                if not results:
                    session_state.clear_results()
                    return self._make_response("Ничего не найдено", ok=False)
                session_state.set_results(results, task="search_files", kind="file")
                lines = ["Нашёл (выберите номер):"]
                for idx, item in enumerate(results, 1):
                    lines.append(f"{idx}) {item}")
                reply = "\n".join(lines)
                return self._make_response(reply, ok=True, items=results)

            if intent_type == "web_search":
                result = self.web_automation.search_and_open(intent["query"])
                return self._make_response(result, ok=True)

            if intent_type == "read_file":
                path = intent["path"]

                def _action() -> str:
                    content = self.file_manager.read_text(path, confirmed=True)
                    preview = content[:500]
                    if len(content) > 500:
                        preview += "..."
                    return f"Содержимое файла:\n{preview}"

                description = f"чтение файла {path}"
                try:
                    content = self.file_manager.read_text(path, confirmed=False)
                except ConfirmationRequiredError:
                    if session.auto_confirm:
                        return self._make_response(_action(), ok=True)
                    session.pending = PendingAction(description=description, callback=lambda _: _action())
                    return self._make_response(
                        f"Нужно подтверждение: {description}. Ответьте 'да', чтобы продолжить.",
                        ok=False,
                        requires_confirmation=True,
                    )
                else:
                    preview = content[:500]
                    if len(content) > 500:
                        preview += "..."
                    return self._make_response(f"Содержимое файла:\n{preview}", ok=True)

            if intent_type == "switch_model":
                session.model = intent["model"]
                return self._make_response(f"Использую модель {session.model}", ok=True)

        except EverythingNotInstalledError as exc:
            return self._make_response(str(exc), ok=False)
        except ConfirmationRequiredError as exc:
            description = exc.args[0]
            session.pending = PendingAction(description=description, callback=lambda _: description)
            return self._make_response(
                f"Требуется подтверждение: {description}. Ответьте 'да', чтобы продолжить.",
                ok=False,
                requires_confirmation=True,
            )
        except Exception as exc:  # pragma: no cover - защита от неожиданных ошибок
            logger.exception("Ошибка при обработке интента: %s", exc)
            return self._make_response(f"Ошибка: {exc}", ok=False)

        logger.debug("Интент %s не потребовал действий", intent_type)
        return self._make_response("Команда обработана", ok=True)
