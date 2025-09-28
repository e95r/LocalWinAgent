"""Определения структур задач для режима Code-as-Actions."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict


@dataclass(slots=True)
class TaskRequest:
    """Описание задачи, которую необходимо выполнить в песочнице."""

    id: str
    title: str
    intent: str
    params: Dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class TaskResult:
    """Результат выполнения задачи в песочнице."""

    ok: bool
    stdout: str = ""
    stderr: str = ""
    data: Dict[str, Any] = field(default_factory=dict)

    @classmethod
    def error(cls, message: str, *, stderr: str | None = None, data: Dict[str, Any] | None = None) -> "TaskResult":
        """Сформировать объект результата с ошибкой."""

        return cls(ok=False, stdout="", stderr=stderr or message, data=data or {"error": message})

    def with_output(self, stdout: str, stderr: str) -> "TaskResult":
        """Вернуть новый экземпляр с объединёнными потоками вывода."""

        merged_stdout = stdout if not self.stdout else f"{stdout}{self.stdout}" if stdout else self.stdout
        merged_stderr = stderr if not self.stderr else f"{stderr}{self.stderr}" if stderr else self.stderr
        return TaskResult(ok=self.ok, stdout=merged_stdout, stderr=merged_stderr, data=self.data)
