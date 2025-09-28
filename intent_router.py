"""Маршрутизатор интентов для LocalWinAgent."""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Callable, Dict, Optional

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
from tools.files import ConfirmationRequiredError, FileManager, get_desktop_path
from tools.search import EverythingNotInstalledError, search_files
from tools.web import WebAutomation

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
        self.web_automation = WebAutomation(
            browser=web_cfg.get("browser", "chromium"),
            headless=web_cfg.get("headless", False),
            implicit_wait_ms=int(web_cfg.get("implicit_wait_ms", 1500)),
        )
        self.llm_client = LLMClient()
        self.download_dir = paths_cfg.get("default_downloads")

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
    ) -> Dict[str, str]:
        confirmed_flag = session.auto_confirm
        try:
            result = performer(confirmed_flag)
        except ConfirmationRequiredError:
            normalized = self.file_manager._normalize(path)
            if session.auto_confirm:
                result = performer(True)
                return {"reply": formatter(result), "requires_confirmation": False}
            description = f"{action_name}: {normalized}"

            def _callback(_: bool) -> str:
                data = performer(True)
                return formatter(data)

            session.pending = PendingAction(description=description, callback=_callback)
            return {
                "reply": f"Требуется подтверждение для пути: {normalized}. Скажите 'да' для подтверждения.",
                "requires_confirmation": True,
            }
        except Exception as exc:
            logger.exception("Ошибка при выполнении операции %s: %s", action_name, exc)
            return {"reply": f"Ошибка: {exc}", "requires_confirmation": False}
        return {"reply": formatter(result), "requires_confirmation": False}

    def _handle_create(self, message: str, session: AgentSession) -> Optional[Dict[str, str]]:
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

    def _handle_write(self, message: str, session: AgentSession) -> Optional[Dict[str, str]]:
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

    def _handle_append(self, message: str, session: AgentSession) -> Optional[Dict[str, str]]:
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

    def _handle_open(self, message: str, session: AgentSession) -> Optional[Dict[str, str]]:
        match = self.OPEN_RE.match(message)
        if not match:
            return None
        raw_path = self._clean_path(match.group(1))
        try:
            result = self.file_manager.open_path(raw_path)
        except Exception as exc:
            logger.exception("Ошибка открытия пути %s: %s", raw_path, exc)
            return {"reply": f"Ошибка: {exc}", "requires_confirmation": False}
        if not result.get("ok", False):
            error = result.get("error", "Не удалось открыть путь")
            return {"reply": f"Ошибка: {error}", "requires_confirmation": False}
        return {"reply": f"Открыто: {result['path']}", "requires_confirmation": False}

    def _handle_move(self, message: str, session: AgentSession) -> Optional[Dict[str, str]]:
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

    def _handle_copy(self, message: str, session: AgentSession) -> Optional[Dict[str, str]]:
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

    def _handle_delete(self, message: str, session: AgentSession) -> Optional[Dict[str, str]]:
        match = self.DELETE_RE.match(message)
        if not match:
            return None
        raw_path = self._clean_path(match.group(1))

        def _perform(confirmed: bool) -> dict:
            return self.file_manager.delete_path(raw_path, confirmed=confirmed)

        def _format(data: dict) -> str:
            return f"Удалено: {data['path']} (exists={data.get('exists')})"

        return self._execute_with_confirmation(session, raw_path, "удаление", _perform, _format)

    def _handle_list(self, message: str, session: AgentSession) -> Optional[Dict[str, str]]:
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

    def _handle_file_commands(self, message: str, session: AgentSession) -> Optional[Dict[str, str]]:
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

    def handle_message(self, message: str, session: AgentSession, *, force_confirm: bool = False) -> Dict[str, str]:
        if force_confirm and session.pending:
            logger.debug("Принудительное подтверждение действия %s", session.pending.description)
            try:
                result = session.pending.callback(True)
            finally:
                session.pending = None
            return {"reply": result, "requires_confirmation": False}

        if session.pending:
            if self._is_positive(message):
                try:
                    result = session.pending.callback(True)
                except Exception as exc:  # pragma: no cover - неожиданные ошибки при выполнении колбэка
                    logger.exception("Ошибка в подтверждённом действии: %s", exc)
                    reply = f"Ошибка: {exc}"
                else:
                    reply = result
                session.pending = None
                return {"reply": reply, "requires_confirmation": False}
            if self._is_negative(message):
                description = session.pending.description
                session.pending = None
                return {"reply": f"Отменено: {description}", "requires_confirmation": False}
            return {"reply": "Пожалуйста, ответьте 'да' или 'нет' для подтверждения.", "requires_confirmation": True}

        message = message.strip()
        if not message:
            return {"reply": "Пустой запрос", "requires_confirmation": False}

        normalized_lower = message.lower().strip()
        normalized_lower = normalized_lower.rstrip(" ?")

        if normalized_lower == "напиши путь до рабочего стола":
            desktop_path = get_desktop_path().resolve(strict=False)
            return {
                "reply": f"Рабочий стол: {desktop_path}",
                "requires_confirmation": False,
            }

        if normalized_lower == "какие файлы есть на рабочем столе":
            desktop_path = get_desktop_path().resolve(strict=False)
            try:
                data = self.file_manager.list_directory(str(desktop_path), confirmed=True)
            except FileNotFoundError:
                reply = f"Рабочий стол не найден: {desktop_path}"
            except Exception as exc:  # pragma: no cover - защита от неожиданных ошибок
                logger.exception("Ошибка при получении списка рабочего стола: %s", exc)
                reply = f"Ошибка: {exc}"
            else:
                items = ", ".join(data.get("items", [])) or "(пусто)"
                reply = f"Рабочий стол ({desktop_path}): {items}"
            return {"reply": reply, "requires_confirmation": False}

        file_result = self._handle_file_commands(message, session)
        if file_result is not None:
            return file_result

        intent = self.detect_intent(message)
        if not intent:
            logger.debug("Интент не распознан, обращение к LLM")
            return {"reply": self.llm_client.chat(message, model=session.model), "requires_confirmation": False}

        intent_type = intent["type"]
        logger.info("Обнаружен интент: %s", intent_type)

        try:
            if intent_type == "open_app":
                result = self.app_manager.launch(intent["app"])
                return {"reply": result, "requires_confirmation": False}

            if intent_type == "close_app":
                result = self.app_manager.close(intent["app"])
                return {"reply": result, "requires_confirmation": False}

            if intent_type == "search_file":
                results = search_files(intent["query"])
                if not results:
                    return {"reply": "Ничего не найдено", "requires_confirmation": False}
                joined = "\n".join(results[:10])
                return {"reply": f"Найдены пути:\n{joined}", "requires_confirmation": False}

            if intent_type == "web_search":
                result = self.web_automation.search_and_open(intent["query"])
                return {"reply": result, "requires_confirmation": False}

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
                        return {"reply": _action(), "requires_confirmation": False}
                    session.pending = PendingAction(description=description, callback=lambda _: _action())
                    return {
                        "reply": f"Нужно подтверждение: {description}. Ответьте 'да', чтобы продолжить.",
                        "requires_confirmation": True,
                    }
                else:
                    preview = content[:500]
                    if len(content) > 500:
                        preview += "..."
                    return {"reply": f"Содержимое файла:\n{preview}", "requires_confirmation": False}

            if intent_type == "switch_model":
                session.model = intent["model"]
                return {"reply": f"Использую модель {session.model}", "requires_confirmation": False}

        except EverythingNotInstalledError as exc:
            return {"reply": str(exc), "requires_confirmation": False}
        except ConfirmationRequiredError as exc:
            description = exc.args[0]
            session.pending = PendingAction(description=description, callback=lambda _: description)
            return {
                "reply": f"Требуется подтверждение: {description}. Ответьте 'да', чтобы продолжить.",
                "requires_confirmation": True,
            }
        except Exception as exc:  # pragma: no cover - защита от неожиданных ошибок
            logger.exception("Ошибка при обработке интента: %s", exc)
            return {"reply": f"Ошибка: {exc}", "requires_confirmation": False}

        logger.debug("Интент %s не потребовал действий", intent_type)
        return {"reply": "Команда обработана", "requires_confirmation": False}
