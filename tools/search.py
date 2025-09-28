"""Поиск файлов через Everything или резервный обход."""
from __future__ import annotations

import logging
import os
import platform
import subprocess
from pathlib import Path
from typing import Iterable, List

logger = logging.getLogger(__name__)

EVERYTHING_CLI = r".\bin\es.exe"


class EverythingNotInstalledError(RuntimeError):
    """Поднимается, если Everything CLI недоступен."""


def _call_everything(query: str, max_results: int = 50) -> List[str]:
    command = [EVERYTHING_CLI, query, "-n", str(max_results), "-full-path"]
    logger.debug("Выполнение команды Everything: %s", command)
    try:
        completed = subprocess.run(
            command,
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
        )
    except FileNotFoundError as exc:
        raise

    if completed.returncode != 0:
        logger.warning("Everything вернул код %s", completed.returncode)
        return []

    return [line.strip() for line in completed.stdout.splitlines() if line.strip()]


def _fallback_search(query: str, roots: Iterable[Path], max_results: int = 20) -> List[str]:
    logger.info("Запуск резервного поиска для запроса '%s'", query)
    results: List[str] = []
    lowered = query.lower()
    for root in roots:
        if not root.exists():
            continue
        for path in root.rglob("*"):
            if lowered in path.name.lower():
                results.append(str(path))
                if len(results) >= max_results:
                    return results
    return results


def search_files(query: str, roots: Iterable[str] | None = None, max_results: int = 50) -> List[str]:
    """Найти файлы по запросу."""
    if not query:
        return []

    if platform.system() == "Windows":
        try:
            return _call_everything(query, max_results=max_results)
        except FileNotFoundError as exc:  # es.exe не найден
            logger.error("Everything CLI не найден: %s", exc)
            raise EverythingNotInstalledError("Установите Everything и утилиту es.exe") from exc
        except subprocess.CalledProcessError as exc:
            logger.error("Ошибка выполнения Everything CLI: %s", exc)
            raise RuntimeError("Everything вернул ошибку") from exc

    search_roots = [Path(os.path.expandvars(path)).expanduser() for path in (roots or [Path.home()])]
    return _fallback_search(query, search_roots, max_results=min(max_results, 20))
