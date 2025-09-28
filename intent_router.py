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
from tools.files import ConfirmationRequiredError, FileManager
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

    APP_KEYWORDS: Dict[str, tuple[str, ...]] = {
        "notepad": ("блокнот", "текстовый редактор", "notepad"),
        "vscode": ("visual studio code", "вс код", "vscode", "код"),
        "word": ("word", "ворд", "microsoft word"),
        "excel": ("excel", "ексель", "microsoft excel"),
        "chrome": ("chrome", "хром", "google chrome", "браузер"),
    }

    CLOSE_KEYWORDS: Dict[str, tuple[str, ...]] = {
        key: tuple(value) for key, value in APP_KEYWORDS.items()
    }

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

    def _extract_path(self, text: str) -> Optional[str]:
        match = re.search(r"([A-Za-z]:\\[^\s]+)", text)
        if match:
            return match.group(1)
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
            path = self._extract_path(text)
            if path:
                return {"type": "read_file", "path": path}

        if "модель" in lowered:
            for candidate in ("llama3.1:8b", "qwen2:7b"):
                if candidate in lowered:
                    return {"type": "switch_model", "model": candidate}

        return None

    def _execute_with_confirmation(self, func: Callable[[], str], session: AgentSession, description: str) -> Dict[str, str]:
        if session.auto_confirm:
            logger.debug("Автоподтверждение включено, выполняем %s", description)
            result = func()
            return {"reply": result, "requires_confirmation": False}

        session.pending = PendingAction(description=description, callback=lambda _: func())
        return {
            "reply": f"Нужно подтверждение: {description}. Ответьте 'да', чтобы продолжить.",
            "requires_confirmation": True,
        }

    @staticmethod
    def _is_positive(text: str) -> bool:
        return text.strip().lower() in {"да", "подтверждаю", "конечно", "ок"}

    @staticmethod
    def _is_negative(text: str) -> bool:
        return text.strip().lower() in {"нет", "отмена", "не надо"}

    def handle_message(self, message: str, session: AgentSession, *, force_confirm: bool = False) -> Dict[str, str]:
        if force_confirm and session.pending:
            logger.debug("Принудительное подтверждение действия %s", session.pending.description)
            result = session.pending.callback(True)
            session.pending = None
            return {"reply": result, "requires_confirmation": False}

        if session.pending:
            if self._is_positive(message):
                result = session.pending.callback(True)
                session.pending = None
                return {"reply": result, "requires_confirmation": False}
            if self._is_negative(message):
                description = session.pending.description
                session.pending = None
                return {"reply": f"Отменено: {description}", "requires_confirmation": False}
            return {"reply": "Пожалуйста, ответьте 'да' или 'нет' для подтверждения.", "requires_confirmation": True}

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
                    return self._execute_with_confirmation(_action, session, description)
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
            return self._execute_with_confirmation(lambda: self.file_manager.read_text(intent.get("path", ""), confirmed=True), session, description)
        except Exception as exc:  # pragma: no cover - защита от неожиданных ошибок
            logger.exception("Ошибка при обработке интента: %s", exc)
            return {"reply": f"Ошибка: {exc}", "requires_confirmation": False}

        logger.debug("Интент %s не потребовал действий", intent_type)
        return {"reply": "Команда обработана", "requires_confirmation": False}
