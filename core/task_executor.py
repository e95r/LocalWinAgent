"""Компиляция и запуск пользовательских задач."""

from __future__ import annotations

from typing import Any, Dict

from core import sandbox
from core.task_schema import TaskResult


def compile_and_run(py_code: str, params: Dict[str, Any], *, timeout_s: int = 20) -> TaskResult:
    """Скомпилировать пользовательский код и выполнить его в песочнице."""

    if not isinstance(py_code, str) or not py_code.strip():
        return TaskResult.error("Пустой код задачи")
    if not isinstance(params, dict):
        return TaskResult.error("Параметры задачи должны быть dict")
    return sandbox.run_py(py_code, params, timeout_s=timeout_s)
