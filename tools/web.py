"""Веб-поиск и открытие страниц."""

from __future__ import annotations

import html
import logging
import re
import webbrowser
from dataclasses import dataclass
from html.parser import HTMLParser
from typing import List, Optional
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlencode, urlparse
from urllib.request import Request, urlopen

try:  # pragma: no cover - в тестовой среде Playwright может отсутствовать
    from playwright.sync_api import TimeoutError as PlaywrightTimeout, sync_playwright

    PLAYWRIGHT_AVAILABLE = True
except ModuleNotFoundError:  # pragma: no cover
    sync_playwright = None  # type: ignore
    PlaywrightTimeout = RuntimeError  # type: ignore
    PLAYWRIGHT_AVAILABLE = False

import config

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class WebConfig:
    browser: str = "chromium"
    headless: bool = False
    implicit_wait_ms: int = 1500


class _DuckDuckGoParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.results: List[dict] = []
        self._capture = False
        self._current_url = ""
        self._current_title: List[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, Optional[str]]]) -> None:  # type: ignore[override]
        if tag != "a":
            return
        attributes = {key: value or "" for key, value in attrs}
        css = attributes.get("class", "")
        if "result__a" in css:
            self._capture = True
            self._current_url = attributes.get("href", "")
            self._current_title = []

    def handle_endtag(self, tag: str) -> None:  # type: ignore[override]
        if tag != "a" or not self._capture:
            return
        title = html.unescape(" ".join(self._current_title).strip())
        url = self._normalize_url(self._current_url)
        if title and url:
            self.results.append({"title": title, "url": url})
        self._capture = False
        self._current_url = ""
        self._current_title = []

    def handle_data(self, data: str) -> None:  # type: ignore[override]
        if self._capture and data.strip():
            self._current_title.append(data.strip())

    @staticmethod
    def _normalize_url(url: str) -> str:
        if not url:
            return ""
        parsed = urlparse(url)
        if parsed.netloc.endswith("duckduckgo.com") and parsed.path.startswith("/l/"):
            params = parse_qs(parsed.query)
            target = params.get("uddg")
            if target:
                return target[0]
        return url


class _SimpleHTMLTextExtractor(HTMLParser):
    """Простой HTML-парсер для извлечения текста из страницы."""

    _BLOCK_TAGS = {
        "p",
        "div",
        "section",
        "article",
        "main",
        "header",
        "footer",
        "nav",
        "li",
        "ul",
        "ol",
        "table",
        "tr",
        "td",
        "th",
        "h1",
        "h2",
        "h3",
        "h4",
        "h5",
        "h6",
        "br",
        "hr",
    }

    def __init__(self) -> None:
        super().__init__()
        self._skip_depth = 0
        self._chunks: List[str] = []
        self._title_parts: List[str] = []
        self._capture_title = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, Optional[str]]]) -> None:  # type: ignore[override]
        lowered = tag.lower()
        if lowered in {"script", "style", "noscript"}:
            self._skip_depth += 1
            return
        if lowered == "title":
            self._capture_title = True
            return
        if lowered in self._BLOCK_TAGS:
            self._append_newline()

    def handle_endtag(self, tag: str) -> None:  # type: ignore[override]
        lowered = tag.lower()
        if lowered in {"script", "style", "noscript"} and self._skip_depth:
            self._skip_depth -= 1
            return
        if lowered == "title":
            self._capture_title = False
            return
        if lowered in self._BLOCK_TAGS:
            self._append_newline()

    def handle_data(self, data: str) -> None:  # type: ignore[override]
        if self._capture_title:
            self._title_parts.append(data.strip())
            return
        if self._skip_depth:
            return
        text = data.strip()
        if text:
            self._chunks.append(text)

    def _append_newline(self) -> None:
        if self._chunks and self._chunks[-1] != "\n":
            self._chunks.append("\n")

    def get_text(self) -> str:
        raw = " ".join(self._chunks)
        raw = re.sub(r"\s*\n\s*", "\n", raw)
        lines = [line.strip() for line in raw.splitlines() if line.strip()]
        return "\n".join(lines)

    def get_title(self) -> str:
        title = " ".join(part.strip() for part in self._title_parts if part.strip())
        return html.unescape(title.strip())


def _load_config() -> WebConfig:
    try:
        raw = config.load_config("web")
    except Exception:  # pragma: no cover - конфиг может отсутствовать
        return WebConfig()
    if not isinstance(raw, dict):
        return WebConfig()
    return WebConfig(
        browser=str(raw.get("browser", "chromium")),
        headless=bool(raw.get("headless", False)),
        implicit_wait_ms=int(raw.get("implicit_wait_ms", 1500)),
    )


_WEB_CONFIG = _load_config()


def reload_config() -> None:
    global _WEB_CONFIG
    _WEB_CONFIG = _load_config()


def search_web(query: str, max_results: int = 5) -> List[dict]:
    query = query.strip()
    if not query:
        return []
    params = urlencode({"q": query, "ia": "web"})
    request = Request(
        f"https://duckduckgo.com/html/?{params}",
        headers={"User-Agent": "Mozilla/5.0"},
    )
    with urlopen(request, timeout=10) as response:  # noqa: S310
        content = response.read().decode("utf-8", errors="ignore")
    parser = _DuckDuckGoParser()
    parser.feed(content)
    return parser.results[:max_results]


def _open_with_playwright(url: str) -> dict:
    if not PLAYWRIGHT_AVAILABLE or sync_playwright is None:  # pragma: no cover - резервный путь
        raise RuntimeError("Playwright не установлен")
    config_obj = _WEB_CONFIG
    with sync_playwright() as playwright:
        browser_factory = getattr(playwright, config_obj.browser)
        browser = browser_factory.launch(headless=config_obj.headless)
        try:
            page = browser.new_page()
            page.goto(url, wait_until="domcontentloaded")
            if config_obj.implicit_wait_ms:
                try:
                    page.wait_for_load_state("networkidle", timeout=config_obj.implicit_wait_ms)
                except PlaywrightTimeout:
                    logger.debug("Сайт %s не достиг состояния networkidle", url)
            title = page.title() or url
            final_url = page.url
        finally:
            browser.close()
    return {"ok": True, "title": title, "url": final_url}


def open_site(url: str) -> dict:
    normalized = url.strip()
    if not normalized:
        return {"ok": False, "message": "Пустой URL"}
    parsed = urlparse(normalized)
    if not parsed.scheme:
        normalized = "https://" + normalized.lstrip("/")
    try:
        return _open_with_playwright(normalized)
    except Exception as exc:  # pragma: no cover - Playwright недоступен
        logger.info("Переход на webbrowser для %s: %s", normalized, exc)
        webbrowser.open(normalized, new=2)
        return {"ok": True, "title": normalized, "url": normalized, "warning": str(exc)}


def fetch_page_text(url: str, *, max_chars: int = 6000, max_bytes: int = 250_000) -> dict:
    """Загрузить страницу и извлечь текстовое содержание."""

    normalized = url.strip()
    if not normalized:
        return {"ok": False, "error": "Пустой URL", "url": url}

    parsed = urlparse(normalized)
    if not parsed.scheme:
        normalized = "https://" + normalized.lstrip("/")

    request = Request(normalized, headers={"User-Agent": "Mozilla/5.0"})

    try:
        with urlopen(request, timeout=12) as response:  # noqa: S310 - локальная загрузка
            raw = response.read(max_bytes)
            final_url = response.geturl() or normalized
            charset = response.headers.get_content_charset() if hasattr(response.headers, "get_content_charset") else None
            encoding = charset or "utf-8"
            html_text = raw.decode(encoding, errors="ignore")
    except (URLError, HTTPError, TimeoutError, OSError) as exc:  # pragma: no cover - зависит от сети
        logger.debug("Не удалось загрузить %s: %s", normalized, exc)
        return {"ok": False, "error": str(exc), "url": normalized}
    except Exception as exc:  # pragma: no cover - защита от неожиданных ошибок
        logger.exception("Ошибка загрузки %s: %s", normalized, exc)
        return {"ok": False, "error": str(exc), "url": normalized}

    parser = _SimpleHTMLTextExtractor()
    try:
        parser.feed(html_text)
    except Exception as exc:  # pragma: no cover - защита от неожиданных ошибок
        logger.debug("Ошибка парсинга HTML %s: %s", normalized, exc)
        return {"ok": False, "error": str(exc), "url": normalized}

    text = parser.get_text()
    if not text:
        return {"ok": False, "error": "Не удалось извлечь текст", "url": normalized}

    collapsed = text[:max_chars].strip()
    title = parser.get_title() or normalized

    return {
        "ok": True,
        "url": final_url if 'final_url' in locals() else normalized,
        "title": title,
        "text": collapsed,
    }
