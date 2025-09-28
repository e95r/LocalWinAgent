"""Изолированное выполнение пользовательского кода."""

from __future__ import annotations

import ast
import importlib
import io
import multiprocessing
import traceback
from contextlib import redirect_stderr, redirect_stdout
from dataclasses import asdict
from multiprocessing.connection import Connection
from typing import Any, Dict

import os

from core.task_schema import TaskResult

ALLOWED_MODULES = {
    "tools.files",
    "tools.apps",
    "tools.search",
    "tools.web",
    "pathlib",
    "json",
    "re",
    "datetime",
    "time",
}

BANNED_NAMES = {
    "__import__",
    "eval",
    "exec",
    "open",
    "compile",
    "input",
    "globals",
    "locals",
    "vars",
    "dir",
    "type",
    "object",
    "super",
    "help",
    "exit",
    "quit",
    "sys",
    "os",
    "shutil",
    "subprocess",
}

BANNED_ATTRS = {
    "__dict__",
    "__class__",
    "__subclasses__",
    "__bases__",
    "__getattribute__",
    "__globals__",
    "__code__",
    "__closure__",
}

OUTPUT_LIMIT = 64 * 1024


class SandboxViolation(RuntimeError):
    """Ошибка статического анализа пользовательского кода."""


class LimitedBuffer(io.StringIO):
    """Буфер с ограничением размера выводимых данных."""

    def __init__(self, limit: int) -> None:
        super().__init__()
        self.limit = limit
        self.truncated = False

    def write(self, s: str) -> int:  # type: ignore[override]
        if not s:
            return 0
        current = self.tell()
        remaining = self.limit - current
        if remaining <= 0:
            self.truncated = True
            return 0
        if len(s) > remaining:
            super().write(s[:remaining])
            self.truncated = True
            return remaining
        return super().write(s)


def _check_ast(tree: ast.AST) -> None:
    class _Visitor(ast.NodeVisitor):
        def visit_Import(self, node: ast.Import) -> None:  # noqa: N802 - API ast
            for alias in node.names:
                if alias.name not in ALLOWED_MODULES:
                    raise SandboxViolation(f"Импорт {alias.name!r} запрещён")
            self.generic_visit(node)

        def visit_ImportFrom(self, node: ast.ImportFrom) -> None:  # noqa: N802
            if node.level != 0 or not node.module:
                raise SandboxViolation("Разрешены только абсолютные импорты")
            if node.module not in ALLOWED_MODULES:
                raise SandboxViolation(f"Импорт из {node.module!r} запрещён")
            self.generic_visit(node)

        def visit_Attribute(self, node: ast.Attribute) -> None:  # noqa: N802
            if node.attr in BANNED_ATTRS:
                raise SandboxViolation(f"Обращение к {node.attr} запрещено")
            self.generic_visit(node)

        def visit_Name(self, node: ast.Name) -> None:  # noqa: N802
            if node.id in BANNED_NAMES:
                raise SandboxViolation(f"Использование {node.id} запрещено")
            self.generic_visit(node)

    _Visitor().visit(tree)


def _safe_builtins() -> Dict[str, Any]:
    return {
        "abs": abs,
        "all": all,
        "any": any,
        "bool": bool,
        "enumerate": enumerate,
        "float": float,
        "int": int,
        "len": len,
        "list": list,
        "max": max,
        "min": min,
        "print": print,
        "range": range,
        "repr": repr,
        "round": round,
        "sorted": sorted,
        "str": str,
        "sum": sum,
        "tuple": tuple,
        "zip": zip,
        "Exception": Exception,
        "ValueError": ValueError,
        "RuntimeError": RuntimeError,
        "FileNotFoundError": FileNotFoundError,
        "OSError": OSError,
    }


def _restricted_import(name: str, globals_dict: Dict[str, Any] | None = None, locals_dict: Dict[str, Any] | None = None, fromlist: tuple[str, ...] = (), level: int = 0) -> Any:
    if level != 0:
        raise ImportError("Разрешены только абсолютные импорты")
    if name not in ALLOWED_MODULES:
        raise ImportError(f"Импорт {name!r} запрещён")
    module = importlib.import_module(name)
    if fromlist:
        return module
    return module


def _prepare_globals() -> Dict[str, Any]:
    safe_globals: Dict[str, Any] = {"__builtins__": _safe_builtins()}
    safe_globals["__builtins__"]["__import__"] = _restricted_import
    return safe_globals


def _coerce_result(result: Any) -> TaskResult:
    if isinstance(result, TaskResult):
        return result
    if isinstance(result, dict):
        ok = bool(result.get("ok", False))
        stdout = str(result.get("stdout", ""))
        stderr = str(result.get("stderr", ""))
        data = result.get("data")
        if isinstance(data, dict):
            payload = data
        else:
            payload = {"result": data}
        return TaskResult(ok=ok, stdout=stdout, stderr=stderr, data=payload)
    if result is None:
        return TaskResult(ok=True, stdout="", stderr="", data={})
    return TaskResult(ok=True, stdout=str(result), stderr="", data={"result": result})


def _execute(py_code: str, params: Dict[str, Any], output_limit: int) -> TaskResult:
    tree = ast.parse(py_code, mode="exec")
    _check_ast(tree)
    compiled = compile(tree, "<sandbox>", "exec")
    namespace = _prepare_globals()
    exec(compiled, namespace, None)
    run_callable = namespace.get("run")
    if not callable(run_callable):  # pragma: no cover - защита от ошибочных скриптов
        raise SandboxViolation("Скрипт обязан объявить функцию run(params)")

    stdout_buffer = LimitedBuffer(output_limit)
    stderr_buffer = LimitedBuffer(output_limit)

    with redirect_stdout(stdout_buffer), redirect_stderr(stderr_buffer):
        task_result = _coerce_result(run_callable(params))  # type: ignore[arg-type]

    stdout_text = stdout_buffer.getvalue()
    stderr_text = stderr_buffer.getvalue()
    if stdout_buffer.truncated:
        stdout_text += "\n[output truncated]"
    if stderr_buffer.truncated:
        stderr_text += "\n[stderr truncated]"
    return task_result.with_output(stdout_text, stderr_text)


def _worker(py_code: str, params: Dict[str, Any], conn: Connection, output_limit: int) -> None:
    try:
        result = _execute(py_code, params, output_limit)
        conn.send(("ok", asdict(result)))
    except Exception as exc:  # pragma: no cover - аварийные ситуации
        conn.send((
            "error",
            {
                "error": str(exc),
                "traceback": traceback.format_exc(),
            },
        ))
    finally:
        conn.close()


def run_py(py_code: str, params: Dict[str, Any], *, timeout_s: int = 20, output_limit: int = OUTPUT_LIMIT) -> TaskResult:
    """Выполнить пользовательский код в отдельном процессе."""

    try:
        tree = ast.parse(py_code, mode="exec")
        _check_ast(tree)
    except (SyntaxError, SandboxViolation) as exc:
        return TaskResult.error(f"Код не прошёл проверку: {exc}")

    if os.environ.get("LOCALWINAGENT_INLINE_SANDBOX") == "1":
        try:
            return _execute(py_code, params, output_limit)
        except Exception as exc:  # pragma: no cover - аварийные ситуации
            return TaskResult.error(str(exc), stderr=traceback.format_exc())

    ctx = multiprocessing.get_context("spawn")
    parent_conn, child_conn = ctx.Pipe(duplex=False)
    process = ctx.Process(target=_worker, args=(py_code, params, child_conn, output_limit), daemon=True)
    process.start()
    child_conn.close()

    try:
        if not parent_conn.poll(timeout_s):
            process.terminate()
            process.join(1.0)
            return TaskResult.error("Превышено время выполнения задачи", data={"timeout": timeout_s})
        status, payload = parent_conn.recv()
    finally:
        parent_conn.close()
        process.join()

    if status == "ok":
        return TaskResult(ok=payload["ok"], stdout=payload.get("stdout", ""), stderr=payload.get("stderr", ""), data=payload.get("data", {}))
    error = payload.get("error", "Ошибка выполнения")
    return TaskResult.error(error, stderr=payload.get("traceback", ""), data=payload)
