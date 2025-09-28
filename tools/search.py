"""Поиск файлов с использованием Everything CLI и обхода белого списка."""

from __future__ import annotations

import logging
import os
import subprocess
from pathlib import Path
from typing import Iterable, List

import config
from tools.files import is_path_hidden, normalize_path

logger = logging.getLogger(__name__)

EVERYTHING_CLI = Path(__file__).resolve().parent.parent / "bin" / "es.exe"


def _call_everything(query: str, max_results: int) -> List[str]:
    command = [str(EVERYTHING_CLI), query, "-n", str(max_results), "-full-path"]
    logger.debug("Everything CLI: %s", command)
    try:
        completed = subprocess.run(  # noqa: S603
            command,
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="ignore",
        )
    except FileNotFoundError:
        logger.info("es.exe не найден по пути %s", EVERYTHING_CLI)
        return []
    except Exception as exc:  # pragma: no cover - системные ошибки
        logger.warning("Ошибка запуска es.exe: %s", exc)
        return []
    if completed.returncode != 0:
        logger.debug("Everything вернул код %s", completed.returncode)
        return []
    lines = [line.strip() for line in completed.stdout.splitlines() if line.strip()]
    normalized = [str(normalize_path(line)) for line in lines]
    unique: List[str] = []
    seen = set()
    for entry in normalized:
        if entry in seen:
            continue
        seen.add(entry)
        unique.append(entry)
        if len(unique) >= max_results:
            break
    return unique


def _iter_whitelist_paths() -> Iterable[Path]:
    paths_config = config.load_config("paths")
    whitelist = paths_config.get("whitelist", [])
    if not isinstance(whitelist, list):
        return []
    for raw in whitelist:
        if not isinstance(raw, str) or not raw.strip():
            continue
        normalized = Path(normalize_path(raw))
        if normalized.exists():
            yield normalized


def _fallback_search(query: str, max_results: int) -> List[str]:
    query_lower = query.lower()
    results: List[str] = []
    seen = set()
    for root in _iter_whitelist_paths():
        if not root.is_dir():
            continue
        for current_root, dirnames, filenames in os.walk(root):
            current_path = Path(current_root)
            dirnames[:] = [name for name in dirnames if not is_path_hidden(current_path / name)]
            for filename in filenames:
                candidate = current_path / filename
                if is_path_hidden(candidate):
                    continue
                if query_lower not in filename.lower():
                    continue
                resolved = str(candidate.resolve(strict=False))
                if resolved in seen:
                    continue
                seen.add(resolved)
                results.append(resolved)
                if len(results) >= max_results:
                    return results
    return results


def search_files(query: str, max_results: int = 50) -> List[str]:
    query = query.strip()
    if not query:
        return []
    results = _call_everything(query, max_results)
    if results:
        return results
    return _fallback_search(query, max_results)


def search_local(query: str, *, max_results: int = 25, whitelist=None, extensions=None) -> List[str]:  # noqa: D401
    """Совместимость со старым API: перенаправление на search_files."""

    return search_files(query, max_results=max_results)
