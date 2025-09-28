"""Индексатор приложений Windows из меню «Пуск» для быстрого запуска."""

from __future__ import annotations

import json
import logging
import os
import re
import time
from configparser import ConfigParser
from pathlib import Path
from typing import Dict, Iterable, Iterator, List, Mapping, Optional, Tuple

logger = logging.getLogger(__name__)


class AppIndexer:
    """Сканирование меню "Пуск" для быстрого поиска приложений."""

    CACHE_FILE = "apps_index.json"
    CACHE_TTL_SECONDS = 24 * 3600
    SUPPORTED_EXTENSIONS = {".lnk", ".appref-ms", ".url"}

    def __init__(
        self,
        cache_dir: str | os.PathLike[str] = "cache",
        max_entries: int = 5000,
        *,
        start_menu_dirs: Optional[Mapping[str, str | os.PathLike[str]]] = None,
        cache_ttl: Optional[int] = None,
    ) -> None:
        self.cache_dir = Path(cache_dir)
        self.cache_path = self.cache_dir / self.CACHE_FILE
        self.max_entries = max(0, int(max_entries))
        self.cache_ttl = int(cache_ttl if cache_ttl is not None else self.CACHE_TTL_SECONDS)
        if start_menu_dirs is None:
            user_start = os.environ.get("APPDATA")
            dirs: Dict[str, Path] = {
                "common": Path(r"C:/ProgramData/Microsoft/Windows/Start Menu/Programs"),
            }
            if user_start:
                dirs["user"] = Path(user_start) / "Microsoft" / "Windows" / "Start Menu" / "Programs"
            else:
                dirs["user"] = Path.home() / "AppData" / "Roaming" / "Microsoft" / "Windows" / "Start Menu" / "Programs"
        else:
            dirs = {key: Path(value) for key, value in start_menu_dirs.items()}
        self.start_menu_dirs = {key: path.resolve(strict=False) for key, path in dirs.items()}

    # ------------------------- публичные методы -------------------------
    def scan(self) -> List[Dict[str, object]]:
        """Полный обход меню "Пуск"."""

        results: List[Dict[str, object]] = []
        total_shortcuts = 0
        valid_shortcuts = 0

        for source, root in self.start_menu_dirs.items():
            if not root.exists():
                logger.debug("Каталог меню 'Пуск' %s не найден: %s", source, root)
                continue
            for shortcut_path in self._iter_shortcuts(root):
                total_shortcuts += 1
                item = self._build_entry(shortcut_path, source)
                if not item:
                    continue
                results.append(item)
                valid_shortcuts += 1
                if self.max_entries and len(results) >= self.max_entries:
                    logger.warning(
                        "Достигнут лимит индекса приложений (%s записей)",
                        self.max_entries,
                    )
                    break
            else:
                continue
            break

        logger.info(
            "Индексация меню 'Пуск': найдено %s ярлыков, валидных %s",
            total_shortcuts,
            valid_shortcuts,
        )
        return results

    def save_cache(self, items: Iterable[Dict[str, object]]) -> str:
        """Сохранить результаты сканирования в JSON."""

        payload = {
            "generated_at": time.time(),
            "items": list(items)[: self.max_entries or None],
        }
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        with self.cache_path.open("w", encoding="utf-8") as fh:
            json.dump(payload, fh, ensure_ascii=False, indent=2)
        logger.debug("Кэш индекса приложений сохранён: %s", self.cache_path)
        return str(self.cache_path)

    def load_cache(self) -> List[Dict[str, object]]:
        """Загрузить кэш, если он существует и не устарел."""

        if not self.cache_path.exists():
            return []
        try:
            with self.cache_path.open("r", encoding="utf-8") as fh:
                payload = json.load(fh)
        except (OSError, json.JSONDecodeError) as exc:
            logger.warning("Не удалось загрузить кэш приложений: %s", exc)
            return []
        generated = float(payload.get("generated_at", 0))
        if generated and self.cache_ttl > 0 and time.time() - generated > self.cache_ttl:
            logger.info("Кэш индекса приложений устарел")
            return []
        items = payload.get("items")
        if not isinstance(items, list):
            return []
        return [item for item in items if isinstance(item, dict)]

    # ------------------------- служебные методы -------------------------
    def _iter_shortcuts(self, root: Path) -> Iterator[Path]:
        for path in root.rglob("*"):
            if path.is_dir():
                continue
            if path.suffix.lower() in self.SUPPORTED_EXTENSIONS:
                yield path

    def _build_entry(self, shortcut_path: Path, source: str) -> Optional[Dict[str, object]]:
        suffix = shortcut_path.suffix.lower()
        name = self.normalize_name(shortcut_path.name)
        if not name:
            logger.debug("Пропущен ярлык без названия: %s", shortcut_path)
            return None
        target_path = ""
        args = ""

        if suffix == ".lnk":
            target_path, args = self.resolve_lnk(shortcut_path)
            if target_path and not Path(target_path).exists():
                logger.debug("Пропущен ярлык на отсутствующий файл: %s -> %s", shortcut_path, target_path)
                return None
        elif suffix == ".url":
            target_path = self.resolve_url(shortcut_path)
            if not target_path:
                logger.debug("Пропущен .url без URL: %s", shortcut_path)
                return None
        elif suffix == ".appref-ms":
            target_path = self.resolve_appref(shortcut_path)
        else:
            return None

        entry = {
            "name": name,
            "path": target_path or "",
            "args": args or "",
            "shortcut": str(shortcut_path),
            "source": source,
            "score_boost": 0,
        }
        return entry

    # ------------------------- методы разбора ярлыков -------------------------
    @staticmethod
    def resolve_lnk(shortcut_path: Path) -> Tuple[str, str]:
        try:
            import win32com.client  # type: ignore

            shell = win32com.client.Dispatch("WScript.Shell")  # type: ignore[attr-defined]
            shortcut = shell.CreateShortcut(str(shortcut_path))  # type: ignore[attr-defined]
            # На разных версиях Windows объект ярлыка предоставляет TargetPath
            # c заглавной или строчной буквой «P». Проверяем обе версии, чтобы
            # избежать падения на одной из систем и не терять цель ярлыка.
            target = getattr(shortcut, "TargetPath", "") or getattr(shortcut, "Targetpath", "")
            arguments = getattr(shortcut, "Arguments", "")
            return str(target or ""), str(arguments or "")
        except Exception as exc:  # pragma: no cover - зависит от окружения
            logger.debug("Не удалось разобрать ярлык %s: %s", shortcut_path, exc)
            return "", ""

    @staticmethod
    def resolve_url(path: Path) -> str:
        parser = ConfigParser()
        try:
            parser.read(path, encoding="utf-8")
        except OSError:
            return ""
        if not parser.has_section("InternetShortcut"):
            return ""
        return parser.get("InternetShortcut", "URL", fallback="").strip()

    @staticmethod
    def resolve_appref(path: Path) -> str:
        try:
            content = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            return ""
        for line in content.splitlines():
            if "=" not in line:
                continue
            key, value = line.split("=", 1)
            lowered = key.strip().lower()
            if lowered in {"deploymentproviderurl", "url", "appurl"}:
                return value.strip()
        return ""

    # ------------------------- утилиты -------------------------
    @staticmethod
    def normalize_name(filename: str) -> str:
        stem = Path(filename).stem
        stem = re.sub(r"(?i)\s*-\s*ссылка$", "", stem)
        stem = re.sub(r"(?i)\s*\(x64\)$", "", stem)
        stem = re.sub(r"\s+", " ", stem)
        return stem.strip()


__all__ = ["AppIndexer"]
