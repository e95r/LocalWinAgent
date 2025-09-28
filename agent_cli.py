"""CLI для локального ассистента LocalWinAgent."""
from __future__ import annotations

import logging
from pathlib import Path

from prompt_toolkit import PromptSession
from prompt_toolkit.completion import WordCompleter
from prompt_toolkit.history import FileHistory
try:  # pragma: no cover - rich может отсутствовать
    from rich.console import Console  # type: ignore
    from rich.panel import Panel  # type: ignore
except ModuleNotFoundError:  # pragma: no cover
    class Console:
        def print(self, *objects, sep=" ") -> None:
            print(sep.join(str(obj) for obj in objects))

    class Panel:
        def __init__(self, renderable: str, title: str | None = None, border_style: str | None = None):
            self.renderable = renderable
            self.title = title

        def __str__(self) -> str:
            return f"{self.title or ''}\n{self.renderable}"

from intent_router import AgentSession, IntentRouter

console = Console()
logger = logging.getLogger("localwinagent.cli")

HISTORY_PATH = Path.home() / ".localwinagent_history"


def _print_response(text: str) -> None:
    console.print(Panel(text, title="Ответ агента", border_style="blue"))


def run_cli() -> None:
    logging.basicConfig(level="INFO")
    router = IntentRouter()
    session = AgentSession()
    history = FileHistory(str(HISTORY_PATH))
    completer = WordCompleter(
        [
            "открой текстовый редактор",
            "найди файл",
            "закрой excel",
            "найди страницу",
            ":auto on",
            ":auto off",
            ":model llama3.1:8b",
            ":model qwen2:7b",
            "выход",
        ],
        ignore_case=True,
    )
    prompt_session = PromptSession(history=history, completer=completer)
    console.print("[bold]LocalWinAgent[/bold] — введите команду. 'выход' для завершения.")

    while True:
        try:
            user_input = prompt_session.prompt("🧠 > ")
        except KeyboardInterrupt:
            console.print("[yellow]Прервано пользователем[/yellow]")
            continue
        except EOFError:
            console.print("[green]До встречи![/green]")
            break

        stripped = user_input.strip()
        if not stripped:
            continue
        if stripped.lower() in {"выход", "exit", "quit"}:
            console.print("[green]До встречи![/green]")
            break

        if stripped.lower().startswith(":auto"):
            session.auto_confirm = stripped.lower().endswith("on")
            state = "включено" if session.auto_confirm else "выключено"
            console.print(f"[cyan]Автоподтверждение {state}[/cyan]")
            continue

        if stripped.lower().startswith(":model"):
            parts = stripped.split(maxsplit=1)
            if len(parts) == 2:
                session.model = parts[1]
                console.print(f"[cyan]Используемая модель: {session.model}[/cyan]")
            else:
                console.print("[red]Укажите модель: :model llama3.1:8b[/red]")
            continue

        response = router.handle_message(stripped, session)
        _print_response(response["reply"])

        if response["requires_confirmation"]:
            confirm = prompt_session.prompt("Подтвердить действие? (y/n): ")
            if confirm.strip().lower().startswith("y"):
                confirmed = router.handle_message("да", session, force_confirm=True)
                _print_response(confirmed["reply"])
            else:
                router.handle_message("нет", session)
                console.print("[yellow]Действие отменено[/yellow]")


if __name__ == "__main__":  # pragma: no cover
    run_cli()
