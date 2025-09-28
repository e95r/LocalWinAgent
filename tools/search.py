"""Поиск файлов через Everything или резервный обход."""
from __future__ import annotations

import logging
import os
import platform
import re
import subprocess
from pathlib import Path
from typing import Iterable, List, Optional, Sequence, Tuple

try:  # pragma: no cover - rapidfuzz может отсутствовать в тестовой среде
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
    """Поднимается, если Everything CLI недоступен."""


def _call_everything(
    query: str,
    max_results: int = 50,
    *,
    raise_on_missing: bool = False,
) -> List[str]:
    command = [str(EVERYTHING_CLI), query, "-n", str(max_results), "-full-path"]
    logger.debug("Выполнение команды Everything: %s", command)
    try:
        completed = subprocess.run(
            command,
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="ignore",
        )
    except FileNotFoundError as exc:
        if raise_on_missing and platform.system() == "Windows":
            raise EverythingNotInstalledError("Установите Everything и утилиту es.exe") from exc
        logger.warning("Everything CLI не найден: %s", exc)
        return []

    if completed.returncode != 0:
        logger.debug("Everything вернул код %s", completed.returncode)
        return []

    return [line.strip() for line in completed.stdout.splitlines() if line.strip()]


def _tokenize(text: str) -> List[str]:
    return re.findall(r"[\w-]+", text.lower())


def _score_name(name: str, tokens: Sequence[str], baseline: str) -> float:
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
    max_results: int = 25,
    extensions: Optional[Sequence[str]] = None,
) -> List[str]:
    logger.info("Запуск резервного поиска для запроса '%s'", query)
    tokens = _tokenize(query)
    baseline = query.lower()
    extension_set = {ext.lower() for ext in extensions or ()}
    scored: List[Tuple[float, float, Path]] = []

    for root in roots:
        if not root.exists() or not root.is_dir():
            continue
        try:
            for current_root, dirnames, filenames in os.walk(root):
                current_path = Path(current_root)
                dirnames[:] = [name for name in dirnames if not is_path_hidden(current_path / name)]
                for filename in filenames:
                    candidate = current_path / filename
                    if is_path_hidden(candidate):
                        continue
                    if extension_set and candidate.suffix.lower() not in extension_set:
                        continue
                    score = _score_name(candidate.name, tokens, baseline)
                    if score < 65:
                        continue
                    try:
                        mtime = candidate.stat().st_mtime
                    except OSError:
                        mtime = 0.0
                    scored.append((score, mtime, candidate))
        except PermissionError:  # pragma: no cover - недоступные директории
            logger.debug("Нет доступа к директории %s", root)
            continue

    scored.sort(key=lambda item: (-item[0], -item[1]))
    limited = scored[:max_results]
    return [str(normalize_path(path)) for _, _, path in limited]


def _normalize_results(paths: Iterable[str], extensions: Optional[Sequence[str]] = None) -> List[str]:
    extension_set = {ext.lower() for ext in extensions or ()}
    normalized: List[str] = []
    for raw in paths:
        try:
            resolved = normalize_path(raw)
        except FileNotFoundError:
            continue
        if extension_set and resolved.suffix.lower() not in extension_set:
            continue
        normalized.append(str(resolved))
    return normalized


def search_local(
    query: str,
    max_results: int = 25,
    *,
    whitelist: Optional[Iterable[str]] = None,
    extensions: Optional[Sequence[str]] = None,
) -> List[str]:
    """Поиск локальных файлов с использованием Everything и резервного обхода."""

    query = query.strip()
    if not query:
        return []

    results = _call_everything(query, max_results=max_results, raise_on_missing=False)
    normalized = _normalize_results(results, extensions)
    if normalized:
        return normalized[:max_results]

    roots = [normalize_path(path) for path in (whitelist or [str(Path.home())])]
    return _fallback_search(query, roots, max_results=max_results, extensions=extensions)


def search_files(query: str, roots: Optional[Iterable[str]] = None, max_results: int = 50) -> List[str]:
    """Совместимая обёртка для старых вызовов поиска."""

    query = query.strip()
    if not query:
        return []

    if platform.system() == "Windows":
        results = _call_everything(query, max_results=max_results, raise_on_missing=True)
        return _normalize_results(results)

    whitelist = [normalize_path(path) for path in (roots or [str(Path.home())])]
    return _fallback_search(query, whitelist, max_results=min(max_results, 25))
