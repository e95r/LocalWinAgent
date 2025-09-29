"""Точка входа для FastAPI сервера LocalWinAgent."""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Dict

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
try:  # pragma: no cover - rich может отсутствовать в тестовой среде
    from rich.logging import RichHandler  # type: ignore
except ModuleNotFoundError:  # pragma: no cover
    class RichHandler(logging.StreamHandler):
        pass

from intent_router import AgentSession, IntentRouter, SessionState

LOG_PATH = Path("logs/agent.log")
LOG_PATH.parent.mkdir(parents=True, exist_ok=True)

logging.basicConfig(
    level="INFO",
    format="%(message)s",
    handlers=[
        RichHandler(rich_tracebacks=True, markup=True),
        logging.FileHandler(LOG_PATH, encoding="utf-8"),
    ],
)

logger = logging.getLogger("localwinagent")

app = FastAPI(title="LocalWinAgent", version="1.0.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

frontend_dir = Path("frontend")
app.mount("/static", StaticFiles(directory=frontend_dir), name="static")

intent_router = IntentRouter()


class ConnectionManager:
    def __init__(self) -> None:
        self.sessions: Dict[int, AgentSession] = {}
        self.session_states: Dict[int, dict] = {}

    async def connect(self, websocket: WebSocket) -> None:
        await websocket.accept()
        self.sessions[id(websocket)] = AgentSession()
        self.session_states[id(websocket)] = {"session_state": SessionState()}
        logger.info("Новое подключение WebSocket: %s", id(websocket))

    def disconnect(self, websocket: WebSocket) -> None:
        session = self.sessions.pop(id(websocket), None)
        self.session_states.pop(id(websocket), None)
        logger.info("Отключение WebSocket %s", id(websocket))
        if session and session.pending:
            logger.debug("Сброс ожидающего действия для %s", id(websocket))

    def get_session(self, websocket: WebSocket) -> AgentSession:
        return self.sessions[id(websocket)]

    def get_state(self, websocket: WebSocket) -> dict:
        return self.session_states[id(websocket)]


manager = ConnectionManager()


@app.get("/health")
def healthcheck() -> JSONResponse:
    return JSONResponse({"status": "ok"})


@app.get("/chat")
async def chat_page() -> FileResponse:
    return FileResponse(frontend_dir / "chat" / "index.html")


@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket) -> None:
    await manager.connect(websocket)
    session = manager.get_session(websocket)
    state = manager.get_state(websocket)
    try:
        while True:
            payload = await websocket.receive_text()
            try:
                data = json.loads(payload)
            except json.JSONDecodeError:
                logger.warning("Некорректный JSON от клиента: %s", payload)
                await websocket.send_json(
                    {
                        "response": "Ошибка: некорректный формат сообщения",
                        "requires_confirmation": False,
                        "ok": False,
                        "model": session.model,
                    }
                )
                continue

            message = str(data.get("message", ""))
            auto_present = "auto_confirm" in data
            force_present = "force_confirm" in data
            auto_value = bool(data.get("auto_confirm", False))
            force_value = bool(data.get("force_confirm", False))
            model = data.get("model")

            if auto_present:
                session.auto_confirm = auto_value
            if isinstance(model, str) and model:
                session.model = model

            logger.info("Сообщение от клиента: %s", message)
            session.streaming_enabled = True
            response = intent_router.handle_message(
                message,
                session,
                state,
                auto_confirm=auto_value if auto_present else None,
                force_confirm=force_value if force_present else None,
            )
            streaming_requested = session.streaming_enabled
            session.streaming_enabled = False
            data_field = response.get("data")
            prompt_value = ""
            if isinstance(data_field, dict):
                prompt_value = str(data_field.get("prompt") or "")
            should_stream = streaming_requested and response.get("intent") == "qa" and prompt_value
            if should_stream:
                prompt = prompt_value
                model_name = session.model or intent_router.llm.default_model
                try:
                    async for chunk in intent_router.llm.stream_generate(model_name, prompt):
                        if chunk:
                            await websocket.send_text(chunk)
                except Exception as exc:  # pragma: no cover - защита от неожиданных ошибок
                    logger.exception("Ошибка потоковой генерации: %s", exc)
                    await websocket.send_text(f"Ошибка генерации: {exc}")
                finally:
                    await websocket.send_text(json.dumps({"done": True, "model": session.model}))
                continue
            payload = {
                "response": response["reply"],
                "requires_confirmation": response["requires_confirmation"],
                "ok": response.get("ok", True),
                "model": session.model,
            }
            if "items" in response:
                payload["items"] = response["items"]
            if "intent" in response:
                payload["intent"] = response["intent"]
            await websocket.send_json(payload)
    except WebSocketDisconnect:
        manager.disconnect(websocket)
        logger.info("WebSocket %s отключен", id(websocket))
    except Exception as exc:  # pragma: no cover - защита от неожиданных ошибок
        logger.exception("Ошибка в WebSocket: %s", exc)
        await websocket.close(code=1011, reason=str(exc))
        manager.disconnect(websocket)


if __name__ == "__main__":  # pragma: no cover
    import uvicorn

    uvicorn.run("main:app", host="127.0.0.1", port=8765, reload=False)
