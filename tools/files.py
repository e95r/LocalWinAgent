"""Инструменты работы с файлами для LocalWinAgent."""

from __future__ import annotations

import logging
import os
import platform
import shutil
import subprocess
from pathlib import Path
from typing import Iterable, Optional

import config
from tools.apps import open_with_shell
from tools.exceptions import (
    EverythingNotInstalledError,
    FileOperationError,
    NotAllowedPathError,
)

try:  # pragma: no cover - импорт проверяется в рантайме
    from docx import Document
except ImportError as exc:  # pragma: no cover - отсутствие зависимостей
    raise EverythingNotInstalledError("Требуется установить пакет python-docx") from exc

try:  # pragma: no cover
    from openpyxl import Workbook, load_workbook
except ImportError as exc:  # pragma: no cover
    raise EverythingNotInstalledError("Требуется установить пакет openpyxl") from exc

try:  # pragma: no cover
    from pptx import Presentation
    from pptx.util import Inches
except ImportError as exc:  # pragma: no cover
    raise EverythingNotInstalledError("Требуется установить пакет python-pptx") from exc

logger = logging.getLogger(__name__)

FILE_KIND_EXT = {
    "txt": ".txt",
    "docx": ".docx",
    "xlsx": ".xlsx",
    "pptx": ".pptx",
}

FILE_TYPE_EXT = {
    "текст": ".txt",
    "текстовый": ".txt",
    "txt": ".txt",
    "text": ".txt",
    "word": ".docx",
    "ворд": ".docx",
    "документ": ".docx",
    "docx": ".docx",
    "excel": ".xlsx",
    "таблица": ".xlsx",
    "xls": ".xlsx",
    "xlsx": ".xlsx",
    "ppt": ".pptx",
    "pptx": ".pptx",
    "презентация": ".pptx",
}

KIND_BY_EXTENSION = {value: key for key, value in FILE_KIND_EXT.items()}
FILE_KIND_ALIASES = {
    alias: KIND_BY_EXTENSION.get(ext)
    for alias, ext in FILE_TYPE_EXT.items()
    if ext in KIND_BY_EXTENSION
}


def _expand(path: str | os.PathLike[str]) -> Path:
    expanded = os.path.expandvars(str(path))
    expanded = os.path.expanduser(expanded)
    return Path(expanded)


def normalize_path(
    path: str | os.PathLike[str], *, base: str | os.PathLike[str] | None = None
) -> Path:
    """Нормализовать путь до абсолютного представления."""

    candidate = _expand(path)
    if candidate.is_absolute():
        return candidate.resolve(strict=False)
    base_path = _expand(base) if base is not None else Path.cwd()
    if not base_path.is_absolute():
        base_path = (Path.cwd() / base_path).resolve(strict=False)
    return (base_path / candidate).resolve(strict=False)


def get_desktop_path() -> Path:
    desktop = config._KNOWN.get("DESKTOP")  # pylint: disable=protected-access
    if desktop:
        return Path(desktop).resolve(strict=False)
    return (Path.home() / "Desktop").resolve(strict=False)


def _get_file_attributes(path: Path) -> int:
    if platform.system() != "Windows":
        return 0
    try:
        import ctypes  # type: ignore

        attrs = ctypes.windll.kernel32.GetFileAttributesW(str(path))  # type: ignore[attr-defined]
    except Exception:  # pragma: no cover - системные ошибки
        return 0
    return attrs if attrs != -1 else 0


def _is_hidden_or_system(path: Path) -> bool:
    name = path.name
    if not name:
        return False
    lowered = name.lower()
    if lowered == "desktop.ini" or lowered.startswith("~$"):
        return True
    if name.startswith("."):
        return True
    attributes = _get_file_attributes(path)
    return bool(attributes & 0x2) or bool(attributes & 0x4)


def is_path_hidden(path: Path) -> bool:
    return _is_hidden_or_system(path)


def _resolve_shortcut(path: Path) -> Path:
    if platform.system() != "Windows" or path.suffix.lower() != ".lnk":
        return path
    try:  # pragma: no cover - зависит от win32com
        import win32com.client  # type: ignore

        shell = win32com.client.Dispatch("WScript.Shell")  # type: ignore[attr-defined]
        shortcut = shell.CreateShortCut(str(path))
        target = Path(shortcut.Targetpath)
        return target.resolve(strict=False)
    except Exception:  # pragma: no cover
        logger.warning("Не удалось разрешить ярлык %s", path)
        return path


def open_path(path: str | os.PathLike[str]) -> dict:
    manager = FileManager([])
    return manager.open_path(str(path))


class FileManager:
    """Класс для безопасной работы с файлами в заданных директориях."""

    def __init__(self, whitelist: Iterable[str]):
        self.whitelist = list(whitelist)
        self._allowed_paths = [normalize_path(item) for item in self.whitelist]
        self._default_root = (
            self._allowed_paths[0] if self._allowed_paths else get_desktop_path()
        )
        logger.debug("Белый список директорий: %s", self._allowed_paths)

    @property
    def default_root(self) -> Path:
        return self._default_root

    def normalize(self, path: str | os.PathLike[str]) -> Path:
        return normalize_path(path, base=self._default_root)

    def _normalize_kind(self, kind: Optional[str]) -> Optional[str]:
        if not kind:
            return None
        key = kind.strip().lower()
        if key in FILE_KIND_EXT:
            return key
        if key in FILE_KIND_ALIASES and FILE_KIND_ALIASES[key]:
            return FILE_KIND_ALIASES[key]
        return None

    def _resolve_path(self, raw: str, *, kind: Optional[str] = None) -> Path:
        candidate = _expand(raw)
        normalized_kind = self._normalize_kind(kind)
        if normalized_kind:
            ext = FILE_KIND_EXT[normalized_kind]
            if not candidate.suffix:
                candidate = candidate.with_name(candidate.name + ext)
        if not candidate.is_absolute():
            candidate = (self._default_root / candidate).resolve(strict=False)
        else:
            candidate = candidate.resolve(strict=False)
        return candidate

    def _is_allowed(self, path: Path) -> bool:
        for allowed in self._allowed_paths:
            try:
                path.relative_to(allowed)
                return True
            except ValueError:
                continue
        return False

    def _make_confirmation(self, path: Path, op_name: str) -> dict:
        logger.warning("Требуется подтверждение для %s: %s", op_name, path)
        return {
            "ok": False,
            "path": str(path),
            "requires_confirmation": True,
            "error": f"Требуется подтверждение для {op_name}",
        }

    def _ensure_allowed(
        self, path: Path, confirmed: bool, op_name: str
    ) -> Optional[dict]:
        if self._is_allowed(path) or confirmed:
            return None
        return self._make_confirmation(path, op_name)

    def _inspect(self, path: Path) -> tuple[bool, int]:
        try:
            exists = path.exists()
            size = path.stat().st_size if exists and path.is_file() else 0
        except OSError:
            exists = False
            size = 0
        return exists, size

    def _finalize(
        self,
        path: Path,
        *,
        ok: bool,
        exists: Optional[bool] = None,
        size: Optional[int] = None,
        error: Optional[str] = None,
        requires_confirmation: bool = False,
    ) -> dict:
        result = {
            "ok": ok,
            "path": str(path),
            "requires_confirmation": requires_confirmation,
        }
        if exists is not None:
            result["exists"] = exists
        if size is not None:
            result["size"] = size
        if error:
            result["error"] = error
        return result

    def _handle_exception(self, path: Path, exc: Exception, op_name: str) -> dict:
        if isinstance(exc, NotAllowedPathError):
            error_message = str(exc)
        else:
            error_message = str(exc) or f"Ошибка {op_name}"
        logger.exception("Ошибка при %s %s: %s", op_name, path, exc)
        return self._finalize(path, ok=False, error=error_message)

    def _detect_kind_from_path(self, path: Path, fallback: Optional[str] = None) -> str:
        ext = path.suffix.lower()
        if ext in KIND_BY_EXTENSION:
            return KIND_BY_EXTENSION[ext]
        normalized = self._normalize_kind(fallback)
        if normalized:
            return normalized
        return "txt"

    def _sync_write(self, path: Path, content: str, mode: str, encoding: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open(mode, encoding=encoding) as handler:
            handler.write(content)
            handler.flush()
            os.fsync(handler.fileno())

    def create_file(
        self,
        path: str,
        content: Optional[str] = None,
        *,
        kind: Optional[str] = None,
        confirmed: bool = False,
        encoding: str = "utf-8",
    ) -> dict:
        target = self._resolve_path(path, kind=kind)
        denied = self._ensure_allowed(target, confirmed, "создания файла")
        if denied:
            return denied
        logger.info("Создание файла: %s", target)
        file_kind = self._detect_kind_from_path(target, kind)
        try:
            if file_kind == "docx":
                target.parent.mkdir(parents=True, exist_ok=True)
                document = Document()
                if content:
                    document.add_paragraph(content)
                document.save(str(target))
            elif file_kind == "xlsx":
                target.parent.mkdir(parents=True, exist_ok=True)
                workbook = Workbook()
                sheet = workbook.active
                if content:
                    sheet["A1"] = str(content)
                workbook.save(str(target))
            elif file_kind == "pptx":
                target.parent.mkdir(parents=True, exist_ok=True)
                presentation = Presentation()
                layout = presentation.slide_layouts[1] if len(presentation.slide_layouts) > 1 else presentation.slide_layouts[0]
                slide = presentation.slides.add_slide(layout)
                if content:
                    applied = False
                    title = getattr(slide.shapes, "title", None)
                    if title is not None and getattr(title, "has_text_frame", False):
                        title.text = content
                        applied = True
                    for shape in slide.shapes:
                        if getattr(shape, "has_text_frame", False):
                            shape.text_frame.text = content
                            applied = True
                            break
                    if not applied:
                        textbox = slide.shapes.add_textbox(
                            Inches(1), Inches(1.5), Inches(8), Inches(2)
                        )
                        textbox.text_frame.text = content
                presentation.save(str(target))
            else:
                text = content if content is not None else ""
                self._sync_write(target, text, mode="w", encoding=encoding)
            exists, size = self._inspect(target)
            logger.info("Создание завершено: %s exists=%s size=%s", target, exists, size)
            return self._finalize(target, ok=exists, exists=exists, size=size)
        except Exception as exc:
            return self._handle_exception(target, exc, "создании файла")

    def write_text(
        self,
        path: str,
        content: str,
        *,
        confirmed: bool = False,
        encoding: str = "utf-8",
    ) -> dict:
        target = self._resolve_path(path)
        denied = self._ensure_allowed(target, confirmed, "записи файла")
        if denied:
            return denied
        logger.info("Запись текста в файл: %s", target)
        try:
            self._sync_write(target, content, mode="w", encoding=encoding)
            exists, size = self._inspect(target)
            logger.info("Запись завершена: %s exists=%s size=%s", target, exists, size)
            return self._finalize(target, ok=exists, exists=exists, size=size)
        except Exception as exc:
            return self._handle_exception(target, exc, "записи файла")

    def append_text(
        self,
        path: str,
        content: str,
        *,
        confirmed: bool = False,
        encoding: str = "utf-8",
    ) -> dict:
        target = self._resolve_path(path)
        denied = self._ensure_allowed(target, confirmed, "добавления текста")
        if denied:
            return denied
        logger.info("Добавление текста в файл: %s", target)
        try:
            mode = "a" if target.exists() else "w"
            self._sync_write(target, content, mode=mode, encoding=encoding)
            exists, size = self._inspect(target)
            logger.info("Добавление завершено: %s exists=%s size=%s", target, exists, size)
            return self._finalize(target, ok=exists, exists=exists, size=size)
        except Exception as exc:
            return self._handle_exception(target, exc, "добавлении текста")

    def read_text(self, path: str, encoding: str = "utf-8") -> dict:
        target = self._resolve_path(path)
        logger.info("Чтение файла: %s", target)
        if target.suffix.lower() != ".txt":
            error = "Чтение доступно только для текстовых файлов"
            logger.error("%s: %s", error, target)
            return self._finalize(target, ok=False, exists=target.exists(), error=error)
        try:
            content = target.read_text(encoding=encoding)
            exists, size = self._inspect(target)
            logger.info("Чтение завершено: %s exists=%s size=%s", target, exists, size)
            return {
                "ok": True,
                "path": str(target),
                "content": content,
                "exists": exists,
                "size": size,
                "requires_confirmation": False,
            }
        except Exception as exc:
            return self._handle_exception(target, exc, "чтении файла")

    def edit_word(
        self,
        path: str,
        content: str,
        *,
        confirmed: bool = False,
    ) -> dict:
        target = self._resolve_path(path, kind="docx")
        denied = self._ensure_allowed(target, confirmed, "редактирования документа")
        if denied:
            return denied
        logger.info("Редактирование Word-документа: %s", target)
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            if target.exists():
                document = Document(str(target))
            else:
                document = Document()
            document.add_paragraph(content)
            document.save(str(target))
            exists, size = self._inspect(target)
            logger.info("Word-документ обновлён: %s exists=%s size=%s", target, exists, size)
            return self._finalize(target, ok=exists, exists=exists, size=size)
        except Exception as exc:
            return self._handle_exception(target, exc, "редактировании документа")

    def edit_excel(
        self,
        path: str,
        cell: str,
        value: str,
        *,
        confirmed: bool = False,
    ) -> dict:
        target = self._resolve_path(path, kind="xlsx")
        denied = self._ensure_allowed(target, confirmed, "редактирования таблицы")
        if denied:
            return denied
        logger.info("Редактирование Excel-файла: %s", target)
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            if target.exists():
                workbook = load_workbook(str(target))
            else:
                workbook = Workbook()
            sheet = workbook.active
            sheet[str(cell).upper()] = value
            workbook.save(str(target))
            exists, size = self._inspect(target)
            logger.info("Excel-файл обновлён: %s exists=%s size=%s", target, exists, size)
            return self._finalize(target, ok=exists, exists=exists, size=size)
        except Exception as exc:
            return self._handle_exception(target, exc, "редактировании таблицы")

    def edit_pptx(
        self,
        path: str,
        content: str,
        *,
        confirmed: bool = False,
    ) -> dict:
        target = self._resolve_path(path, kind="pptx")
        denied = self._ensure_allowed(target, confirmed, "редактирования презентации")
        if denied:
            return denied
        logger.info("Редактирование презентации: %s", target)
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            presentation = Presentation(str(target)) if target.exists() else Presentation()
            layout = presentation.slide_layouts[1] if len(presentation.slide_layouts) > 1 else presentation.slide_layouts[0]
            slide = presentation.slides.add_slide(layout)
            if content:
                applied = False
                title = getattr(slide.shapes, "title", None)
                if title is not None and getattr(title, "has_text_frame", False):
                    title.text = content
                    applied = True
                for shape in slide.shapes:
                    if getattr(shape, "has_text_frame", False):
                        shape.text_frame.text = content
                        applied = True
                        break
                if not applied:
                    textbox = slide.shapes.add_textbox(Inches(1), Inches(1.5), Inches(8), Inches(2))
                    textbox.text_frame.text = content
            presentation.save(str(target))
            exists, size = self._inspect(target)
            logger.info("Презентация обновлена: %s exists=%s size=%s", target, exists, size)
            return self._finalize(target, ok=exists, exists=exists, size=size)
        except Exception as exc:
            return self._handle_exception(target, exc, "редактировании презентации")

    def open_path(self, path: str) -> dict:
        target = self._resolve_path(path)
        logger.info("Открытие пути: %s", target)
        if not target.exists():
            logger.error("Путь не существует: %s", target)
            return self._finalize(target, ok=False, exists=False, error="Путь не существует")
        actual = _resolve_shortcut(target)
        resolved = actual.resolve(strict=False)
        try:
            if os.name == "nt":
                os.startfile(str(actual))  # type: ignore[attr-defined]
            else:
                if actual.is_dir():
                    subprocess.Popen(["xdg-open", str(actual)])  # noqa: S603,S607 pragma: no cover - для *nix
                else:
                    open_with_shell(str(actual))
        except Exception as exc:  # pragma: no cover - системные ошибки
            logger.exception("Ошибка открытия %s: %s", resolved, exc)
            exists, size = self._inspect(resolved)
            return self._finalize(resolved, ok=False, exists=exists, size=size, error=str(exc))
        logger.info("Путь открыт: %s", resolved)
        exists, size = self._inspect(resolved)
        return self._finalize(resolved, ok=True, exists=exists, size=size)

    def move_path(self, src: str, dst: str, *, confirmed: bool = False) -> dict:
        source = self._resolve_path(src)
        destination = self._resolve_path(dst)
        denied_src = self._ensure_allowed(source, confirmed, "перемещения файла")
        if denied_src:
            return denied_src
        denied_dst = self._ensure_allowed(destination, confirmed, "перемещения файла")
        if denied_dst:
            return denied_dst
        logger.info("Перемещение: %s -> %s", source, destination)
        try:
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(source), str(destination))
            exists, size = self._inspect(destination)
            logger.info("Перемещение завершено: %s exists=%s size=%s", destination, exists, size)
            return self._finalize(destination, ok=exists, exists=exists, size=size)
        except Exception as exc:
            return self._handle_exception(destination, exc, "перемещении файла")

    def copy_path(self, src: str, dst: str, *, confirmed: bool = False) -> dict:
        source = self._resolve_path(src)
        destination = self._resolve_path(dst)
        denied_src = self._ensure_allowed(source, confirmed, "копирования файла")
        if denied_src:
            return denied_src
        denied_dst = self._ensure_allowed(destination, confirmed, "копирования файла")
        if denied_dst:
            return denied_dst
        logger.info("Копирование: %s -> %s", source, destination)
        try:
            if source.is_dir():
                shutil.copytree(source, destination, dirs_exist_ok=True)
            else:
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source, destination)
            exists, size = self._inspect(destination)
            logger.info("Копирование завершено: %s exists=%s size=%s", destination, exists, size)
            return self._finalize(destination, ok=exists, exists=exists, size=size)
        except Exception as exc:
            return self._handle_exception(destination, exc, "копировании файла")

    def delete_path(self, path: str, *, confirmed: bool = False) -> dict:
        target = self._resolve_path(path)
        denied = self._ensure_allowed(target, confirmed, "удаления файла")
        if denied:
            return denied
        logger.info("Удаление: %s", target)
        try:
            if target.is_dir():
                shutil.rmtree(target, ignore_errors=False)
            elif target.exists():
                target.unlink()
            exists, _ = self._inspect(target)
            logger.info("Удаление завершено: %s exists=%s", target, exists)
            return self._finalize(target, ok=not exists, exists=exists)
        except Exception as exc:
            return self._handle_exception(target, exc, "удалении файла")

    def list_directory(self, path: Optional[str] = None, *, confirmed: bool = False) -> dict:
        directory = self._default_root if path is None else self._resolve_path(path)
        denied = self._ensure_allowed(directory, confirmed, "просмотра каталога")
        if denied:
            return denied
        logger.info("Просмотр каталога: %s", directory)
        try:
            if not directory.exists() or not directory.is_dir():
                raise FileOperationError(f"Каталог {directory} не найден")
            items = [
                item.name
                for item in sorted(directory.iterdir(), key=lambda candidate: candidate.name.lower())
                if not is_path_hidden(item)
            ]
            logger.info("Каталог %s содержит %d элементов", directory, len(items))
            return {
                "ok": True,
                "path": str(directory),
                "items": items,
                "requires_confirmation": False,
            }
        except Exception as exc:
            return self._handle_exception(directory, exc, "просмотре каталога")
