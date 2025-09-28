"""Поиск файлов и директорий."""

from __future__ import annotations

import logging
import os
import platform
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, List, Optional, Sequence

try:  # pragma: no cover - rapidfuzz может отсутствовать
    from rapidfuzz import fuzz  # type: ignore
except ModuleNotFoundError:  # pragma: no cover
    from difflib import SequenceMatcher

    class _FallbackFuzz:
        @staticmethod
        def partial_ratio(a: str, b: str) -> float:
            return SequenceMatcher(None, a, b).ratio() * 100

    fuzz = _FallbackFuzz()  # type: ignore

from tools.files import is_path_hidden, normalize_path

logger = logging.getLogger(__name__)

EVERYTHING_CLI = Path(__file__).resolve().parent.parent / "bin" / "es.exe"


class EverythingNotInstalledError(RuntimeError):
    """Ошибка при отсутствии Everything CLI."""


@dataclass(slots=True)
class SearchResult:
    path: str
    score: float
    modified: float


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
    return [line.strip() for line in completed.stdout.splitlines() if line.strip()]


def _token_score(name: str, tokens: Sequence[str], baseline: str) -> float:
    name_lower = name.lower()
    scores = [fuzz.partial_ratio(name_lower, baseline)]
    for token in tokens:
        scores.append(fuzz.partial_ratio(name_lower, token))
    if baseline in name_lower:
        scores.append(100.0)
    return max(scores)


def _fallback_search(
    query: str,
    roots: Iterable[Path],
    *,
    max_results: int,
    extensions: Optional[Sequence[str]] = None,
) -> List[str]:
    tokens = [token for token in query.lower().replace("_", " ").split() if token]
    baseline = query.lower()
    extension_set = {ext.lower() for ext in extensions or ()}
    scored: List[SearchResult] = []

    for root in roots:
        if not root.exists() or not root.is_dir():
            continue
        for current_root, dirnames, filenames in os.walk(root):
            current_path = Path(current_root)
            dirnames[:] = [name for name in dirnames if not is_path_hidden(current_path / name)]
            for filename in filenames:
                candidate = current_path / filename
                if is_path_hidden(candidate):
                    continue
                if extension_set and candidate.suffix.lower() not in extension_set:
                    continue
                score = _token_score(candidate.name, tokens, baseline)
                if score < 75:
                    continue
                try:
                    mtime = candidate.stat().st_mtime
                except OSError:
                    mtime = 0.0
                scored.append(SearchResult(path=str(candidate.resolve(strict=False)), score=score, modified=mtime))
    scored.sort(key=lambda item: (-item.score, -item.modified))
    return [item.path for item in scored[:max_results]]


def search_local(
    query: str,
    *,
    max_results: int = 25,
    whitelist: Optional[Iterable[str]] = None,
    extensions: Optional[Sequence[str]] = None,
) -> List[str]:
    query = query.strip()
    if not query:
        return []

    results: List[str] = []
    if platform.system() == "Windows":
        results = _call_everything(query, max_results)
        results = [str(normalize_path(path)) for path in results][:max_results]
        if results:
            return results
    roots = [normalize_path(path) for path in (whitelist or []) if path]
    return _fallback_search(query, roots, max_results=max_results, extensions=extensions)
