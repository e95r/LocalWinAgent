"""Веб-поиск и открытие страниц."""

from __future__ import annotations

import html
import logging
import webbrowser
from dataclasses import dataclass
from html.parser import HTMLParser
from typing import List, Optional
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
