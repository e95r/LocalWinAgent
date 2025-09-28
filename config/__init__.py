"""Загрузка конфигураций проекта LocalWinAgent."""
from __future__ import annotations

import os
import platform
import re
from pathlib import Path
from typing import Any, Dict
from uuid import UUID

try:  # pragma: no cover - ctypes может отсутствовать в урезанных окружениях
    import ctypes
    from ctypes import wintypes
except Exception:  # pragma: no cover
    ctypes = None  # type: ignore
    wintypes = None  # type: ignore

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
_PERCENT_VAR_RE = re.compile(r"%([^%]+)%")
_TOKEN_VAR_RE = re.compile(r"\$\{([A-Z0-9_]+)\}")

_KNOWN_FOLDER_IDS: Dict[str, str] = {
    "DESKTOP": "B4BFCC3A-DB2C-424C-B029-7FE99A87C641",
    "DOCUMENTS": "FDD39AD0-238F-46AF-ADB4-6C85480369C7",
    "DOWNLOADS": "374DE290-123F-4565-9164-39C4925E467B",
    "PICTURES": "33E28130-4E1E-4676-835A-98395C3BC3BB",
    "VIDEOS": "18989B1D-99B5-455B-841C-AB7C74E4DDFC",
}

_FALLBACK_NAMES: Dict[str, str] = {
    "DESKTOP": "Desktop",
    "DOCUMENTS": "Documents",
    "DOWNLOADS": "Downloads",
    "PICTURES": "Pictures",
    "VIDEOS": "Videos",
}


def _uuid_to_guid_struct(folder_id: str) -> "ctypes.Structure" | None:
    """Создать структуру GUID из UUID."""

    if ctypes is None or wintypes is None:  # pragma: no cover - защита от урезанных окружений
        return None
    try:
        uuid_obj = UUID(folder_id)
    except ValueError:
        return None

    class GUID(ctypes.Structure):
        _fields_ = [
            ("Data1", wintypes.DWORD),
            ("Data2", wintypes.WORD),
            ("Data3", wintypes.WORD),
            ("Data4", wintypes.BYTE * 8),
        ]

    data4 = (wintypes.BYTE * 8)(*uuid_obj.bytes[8:])
    guid = GUID(uuid_obj.time_low, uuid_obj.time_mid, uuid_obj.time_hi_version, data4)
    return guid


def SHGetKnownFolderPath(folder_id: str) -> str | None:
    """Получить путь к известной папке Windows через SHGetKnownFolderPath."""

    if platform.system() != "Windows" or ctypes is None:  # pragma: no cover - не Windows
        return None

    guid = _uuid_to_guid_struct(folder_id)
    if guid is None:
        return None

    try:
        shell32 = ctypes.windll.shell32  # type: ignore[attr-defined]
        ole32 = ctypes.windll.ole32  # type: ignore[attr-defined]
    except AttributeError:  # pragma: no cover - редкий случай
        return None

    path_ptr = ctypes.c_wchar_p()
    try:
        result = shell32.SHGetKnownFolderPath(ctypes.byref(guid), 0, None, ctypes.byref(path_ptr))
        if result != 0:
            return None
        return path_ptr.value
    except Exception:  # pragma: no cover - защита от системных ошибок
        return None
    finally:
        if getattr(path_ptr, "value", None):
            try:
                ole32.CoTaskMemFree(path_ptr)  # type: ignore[attr-defined]
            except Exception:  # pragma: no cover - освобождение памяти может не потребоваться
                pass


def _build_known_paths() -> Dict[str, str]:
    """Собрать словарь известных директорий."""

    home = Path.home()
    paths: Dict[str, str] = {}
    for token, folder_id in _KNOWN_FOLDER_IDS.items():
        resolved = SHGetKnownFolderPath(folder_id)
        if not resolved:
            fallback_name = _FALLBACK_NAMES.get(token, token.title())
            resolved = str((home / fallback_name).resolve(strict=False))
        else:
            resolved = str(Path(resolved).resolve(strict=False))
        paths[token] = resolved
    return paths


_KNOWN = _build_known_paths()


def _expand_env(value: Any) -> Any:
    """Рекурсивно раскрыть переменные окружения и тильду в строках."""

    if isinstance(value, str):
        expanded = os.path.expandvars(value)
        expanded = os.path.expanduser(expanded)

        def _replace_percent(match: re.Match[str]) -> str:
            var_name = match.group(1)
            return os.environ.get(var_name, match.group(0))

        expanded = _PERCENT_VAR_RE.sub(_replace_percent, expanded)

        def _replace_token(match: re.Match[str]) -> str:
            key = match.group(1)
            return _KNOWN.get(key, match.group(0))

        expanded = _TOKEN_VAR_RE.sub(_replace_token, expanded)
        return expanded
    if isinstance(value, dict):
        return {key: _expand_env(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_expand_env(item) for item in value]
    return value


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

    data = _expand_env(_load_yaml(file_path))
    _CONFIG_CACHE[name] = data
    return data


def refresh_cache() -> None:
    """Очистить кеш конфигураций (используется в тестах)."""
    _CONFIG_CACHE.clear()


__all__ = ["load_config", "refresh_cache", "SHGetKnownFolderPath", "_KNOWN"]
