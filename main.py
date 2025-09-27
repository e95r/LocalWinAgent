# main.py
# Веб-API + WebSocket-чат + раздача статики для LocalWinAgent
# Зависит от: fastapi, uvicorn, starlette, pydantic, rich
# Предполагает, что intent_router.py предоставляет функцию handle_query(query: str, auto_confirm: bool = False) -> dict

import json
import os
from pathlib import Path
from typing import Optional, Dict, Any

from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Request
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from rich import print as rprint

# Импорт роутера намерений
try:
    from intent_router import handle_query
except Exception as e:
    # Жёсткая ошибка здесь была бы неприятна в проде — выведем понятное сообщение и поднимем дальше.
    raise RuntimeError(f"Не удалось импортировать intent_router.handle_query: {e}")

app = FastAPI(title="LocalWinAgent", version="1.0.0")

# --- Статика (чат) ---
BASE_DIR = Path(__file__).parent.resolve()
CHAT_DIR = BASE_DIR / "frontend" / "chat"
if not CHAT_DIR.exists():
    # Сознательно не создаём ничего автоматически: проект должен содержать эти файлы.
    raise RuntimeError(f"Не найден каталог чата: {CHAT_DIR}")

app.mount("/static", StaticFiles(directory=str(CHAT_DIR)), name="static")

# Корневой редирект на чат
@app.get("/", response_class=HTMLResponse)
async def root():
    return RedirectResponse(url="/chat")

# Отдаём саму страницу чата
@app.get("/chat", response_class=HTMLResponse)
async def chat_page():
    index_file = CHAT_DIR / "index.html"
    return HTMLResponse(content=index_file.read_text(encoding="utf-8"))

# Здоровье сервиса
@app.get("/health")
async def health():
    return {"status": "ok"}

# Синхронная точка /ask (поддерживаем контракт из ТЗ)
class AskPayload(BaseModel):
    query: str
    yes: Optional[bool] = False

@app.post("/ask")
async def ask(payload: AskPayload):
    try:
        result: Dict[str, Any] = handle_query(payload.query, auto_confirm=bool(payload.yes))
        return JSONResponse(result)
    except Exception as e:
        rprint(f"[red]Ошибка /ask:[/red] {e}")
        return JSONResponse({"ok": False, "error": str(e)}, status_code=500)

# WebSocket для живого чата
@app.websocket("/ws")
async def ws_chat(ws: WebSocket):
    await ws.accept()
    try:
        while True:
            raw = await ws.receive_text()
            try:
                msg = json.loads(raw)
                text = str(msg.get("text", "")).strip()
                auto_yes = bool(msg.get("yes", False))
                if not text:
                    await ws.send_text(json.dumps({
                        "ok": False,
                        "answer": "Пустой запрос.",
                        "logs": []
                    }, ensure_ascii=False))
                    continue

                rprint(f"[cyan]WS запрос:[/cyan] {text} (auto_yes={auto_yes})")
                result: Dict[str, Any] = handle_query(text, auto_confirm=auto_yes)

                # Ожидаем, что handle_query вернёт структуру:
                # {"ok": True/False, "answer": "...", "intent": "...", "logs": [...], "data": {...}}
                # Это совместимо с нашей предыдущей архитектурой.
                await ws.send_text(json.dumps(result, ensure_ascii=False))
            except Exception as e:
                rprint(f"[red]Ошибка в обработке WS:[/red] {e}")
                await ws.send_text(json.dumps({
                    "ok": False,
                    "answer": f"Ошибка: {e}",
                    "logs": []
                }, ensure_ascii=False))
    except WebSocketDisconnect:
        rprint("[yellow]WS отключен клиентом[/yellow]")
    except Exception as e:
        rprint(f"[red]WS авария:[/red] {e}")
    finally:
        await ws.close()
