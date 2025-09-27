"""Загрузка конфигураций проекта LocalWinAgent."""
from __future__ import annotations

from pathlib import Path
from typing import Any, Dict

try:  # pragma: no cover - в тестовой среде модуль может отсутствовать
    import yaml  # type: ignore
except ModuleNotFoundError:  # pragma: no cover
    def _parse_scalar(value: str) -> Any:
        value = value.strip()
        if value.startswith('"') and value.endswith('"'):
            return value[1:-1]
        if value.lower() in {"true", "false"}:
            return value.lower() == "true"
        try:
            return int(value)
        except ValueError:
            try:
                return float(value)
            except ValueError:
                return value

    def _next_non_empty(lines: list[str], start: int) -> str | None:
        for idx in range(start, len(lines)):
            candidate = lines[idx].strip()
            if candidate and not candidate.startswith("#"):
                return candidate
        return None

    def _simple_yaml_load(text: str) -> Dict[str, Any]:
        lines = text.splitlines()
        root: Dict[str, Any] = {}
        stack: list[tuple[int, Any]] = [(-1, root)]

        for idx, raw_line in enumerate(lines):
            line = raw_line.split("#", 1)[0].rstrip()
            if not line.strip():
                continue
            indent = len(line) - len(line.lstrip(" "))
            stripped = line.strip()

            while stack and indent <= stack[-1][0]:
                stack.pop()
            parent = stack[-1][1]

            if stripped.startswith("- "):
                value_text = stripped[2:].strip()
                value = _parse_scalar(value_text)
                if isinstance(parent, list):
                    parent.append(value)
                else:
                    raise ValueError("Неверная структура YAML")
                continue

            key, _, value_text = stripped.partition(":")
            key = key.strip()
            value_text = value_text.strip()

            if not value_text:
                next_line = _next_non_empty(lines, idx + 1)
                if next_line and next_line.startswith("- "):
                    container: Any = []
                else:
                    container = {}
                if isinstance(parent, dict):
                    parent[key] = container
                else:
                    new_item: Dict[str, Any] = {key: container}
                    parent.append(new_item)
                    container = new_item[key]
                stack.append((indent, container))
            else:
                value = _parse_scalar(value_text)
                if isinstance(parent, dict):
                    parent[key] = value
                else:
                    parent.append({key: value})

        return root

    class _DummyYaml:
        @staticmethod
        def safe_load(text: str) -> Dict[str, Any]:
            return _simple_yaml_load(text)

    yaml = _DummyYaml()  # type: ignore

_CONFIG_CACHE: Dict[str, Dict[str, Any]] = {}


def _load_yaml(path: Path) -> Dict[str, Any]:
    with path.open("r", encoding="utf-8") as handler:
        return yaml.safe_load(handler.read()) or {}


def load_config(name: str) -> Dict[str, Any]:
    """Загрузить YAML-конфигурацию из каталога config."""
    if name in _CONFIG_CACHE:
        return _CONFIG_CACHE[name]

    file_path = Path(__file__).resolve().parent / f"{name}.yml"
    if not file_path.exists():
        raise FileNotFoundError(f"Не найден файл конфигурации: {file_path}")

    data = _load_yaml(file_path)
    _CONFIG_CACHE[name] = data
    return data


def refresh_cache() -> None:
    """Очистить кеш конфигураций (используется в тестах)."""
    _CONFIG_CACHE.clear()
