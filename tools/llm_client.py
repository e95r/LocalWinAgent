"""Клиент для взаимодействия с локальной Ollama."""

from __future__ import annotations

import json
from typing import AsyncGenerator, Dict, List, Optional
from urllib import error, request

import httpx


class OllamaClient:
    """Простой HTTP-клиент для Ollama REST API."""

    def __init__(
        self,
        base_url: str = "http://127.0.0.1:11434",
        default_model: str = "llama3.1:8b",
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.default_model = default_model

    def _post(self, endpoint: str, payload: Dict[str, object], stream: bool) -> str:
        url = f"{self.base_url}{endpoint}"
        data = json.dumps(payload).encode("utf-8")
        req = request.Request(url, data=data, method="POST")
        req.add_header("Content-Type", "application/json")
        try:
            with request.urlopen(req) as response:  # noqa: S310 - локальное подключение
                if stream:
                    chunks: List[str] = []
                    for raw_line in response:
                        line = raw_line.decode("utf-8").strip()
                        if not line:
                            continue
                        try:
                            message = json.loads(line)
                        except json.JSONDecodeError:
                            continue
                        chunk = self._extract_text(message)
                        if chunk:
                            chunks.append(chunk)
                    return "".join(chunks)
                payload_raw = response.read().decode("utf-8")
                message = json.loads(payload_raw) if payload_raw else {}
                return self._extract_text(message)
        except (error.URLError, error.HTTPError, OSError, json.JSONDecodeError) as exc:
            return f"Модель недоступна. Ошибка: {exc}"
        except Exception as exc:  # pragma: no cover - защита от неожиданных ошибок
            return f"Модель недоступна. Ошибка: {exc}"

    @staticmethod
    def _extract_text(message: Dict[str, object]) -> str:
        if not isinstance(message, dict):
            return ""
        if "response" in message and isinstance(message["response"], str):
            return message["response"]
        chat_message = message.get("message")
        if isinstance(chat_message, dict):
            content = chat_message.get("content")
            if isinstance(content, str):
                return content
        return ""

    def generate(self, prompt: str, model: Optional[str] = None, stream: bool = True) -> str:
        payload: Dict[str, object] = {
            "model": model or self.default_model,
            "prompt": prompt,
            "stream": stream,
        }
        return self._post("/api/generate", payload, stream=stream)

    def chat(self, messages: List[Dict[str, object]], model: Optional[str] = None, stream: bool = True) -> str:
        payload: Dict[str, object] = {
            "model": model or self.default_model,
            "messages": messages,
            "stream": stream,
        }
        return self._post("/api/chat", payload, stream=stream)

    async def stream_generate(self, model: str, prompt: str) -> AsyncGenerator[str, None]:
        """Выполняет потоковую генерацию текста и возвращает части ответа модели."""

        payload: Dict[str, object] = {
            "model": model or self.default_model,
            "prompt": prompt,
            "stream": True,
        }
        url = f"{self.base_url}/api/generate"
        try:
            async with httpx.AsyncClient(timeout=None) as client:
                async with client.stream("POST", url, json=payload) as response:
                    response.raise_for_status()
                    async for line in response.aiter_lines():
                        if not line:
                            continue
                        try:
                            message = json.loads(line)
                        except json.JSONDecodeError:
                            continue
                        chunk = message.get("response")
                        if isinstance(chunk, str) and chunk:
                            yield chunk
                        if message.get("done"):
                            break
        except httpx.HTTPError as exc:
            yield f"Модель недоступна. Ошибка: {exc}"
        except Exception as exc:  # pragma: no cover - защита от неожиданных ошибок
            yield f"Модель недоступна. Ошибка: {exc}"
