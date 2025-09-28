"""Автоматизация браузера через Playwright."""
from __future__ import annotations

import html
import logging
import webbrowser
from contextlib import contextmanager
from html.parser import HTMLParser
from typing import List, Optional, Tuple
from urllib.parse import parse_qs, unquote, urlencode, urlparse
from urllib.request import Request, urlopen

try:  # pragma: no cover - импорт Playwright может отсутствовать в тестовой среде
    from playwright.sync_api import TimeoutError, sync_playwright
    PLAYWRIGHT_AVAILABLE = True
except ModuleNotFoundError:  # pragma: no cover
    TimeoutError = RuntimeError  # type: ignore[assignment]

    def sync_playwright():  # type: ignore[override]
        raise ModuleNotFoundError("Playwright не установлен")

    PLAYWRIGHT_AVAILABLE = False

logger = logging.getLogger(__name__)


class _DuckDuckGoParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.results: List[Tuple[str, str]] = []
        self._capture = False
        self._current_url = ""
        self._current_title: List[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:  # type: ignore[override]
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
        title = html.unescape(" ".join(part for part in self._current_title if part).strip())
        url = self._normalize_url(self._current_url.strip())
        if title and url:
            self.results.append((title, url))
        self._capture = False
        self._current_url = ""
        self._current_title = []

    def handle_data(self, data: str) -> None:  # type: ignore[override]
        if self._capture:
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
                return unquote(target[0])
        return url


def search_web(query: str, max_results: int = 5) -> List[Tuple[str, str]]:
    """Простой веб-поиск по DuckDuckGo HTML."""

    query = query.strip()
    if not query:
        return []

    params = urlencode({"q": query, "ia": "web"})
    url = f"https://duckduckgo.com/html/?{params}"
    request = Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urlopen(request, timeout=10) as response:  # noqa: S310 - управляемый запрос
        content = response.read().decode("utf-8", errors="ignore")

    parser = _DuckDuckGoParser()
    parser.feed(content)
    return parser.results[:max_results]


def open_site(url: str) -> str:
    """Открыть ссылку в системном браузере и вернуть нормализованный URL."""

    normalized = url.strip()
    if not normalized:
        raise ValueError("Пустой URL")
    parsed = urlparse(normalized)
    if not parsed.scheme:
        normalized = "https://" + normalized.lstrip("/")
    webbrowser.open(normalized, new=2)
    return normalized


@contextmanager
def _launch_browser(browser_name: str, headless: bool):
    if not PLAYWRIGHT_AVAILABLE:
        raise RuntimeError("Playwright не установлен. Выполните 'pip install playwright' и 'playwright install'.")

    with sync_playwright() as playwright:
        browser_factory = getattr(playwright, browser_name)
        browser = browser_factory.launch(headless=headless)
        try:
            yield browser
        finally:
            browser.close()


class WebAutomation:
    def __init__(self, browser: str = "chromium", headless: bool = False, implicit_wait_ms: int = 1500):
        self.browser = browser
        self.headless = headless
        self.implicit_wait_ms = implicit_wait_ms

    def open_url(self, url: str, wait_selector: Optional[str] = None) -> str:
        logger.info("Открытие страницы %s", url)
        with _launch_browser(self.browser, self.headless) as browser:
            page = browser.new_page()
            page.goto(url, wait_until="domcontentloaded")
            if wait_selector:
                try:
                    page.wait_for_selector(wait_selector, timeout=self.implicit_wait_ms)
                except TimeoutError:
                    logger.warning("Элемент %s не найден за %sms", wait_selector, self.implicit_wait_ms)
            title = page.title()
        return f"Открыта страница '{title}'"

    def search_and_open(self, query: str, engine: str = "https://www.google.com/search?q=") -> str:
        encoded_query = query.replace(" ", "+")
        url = f"{engine}{encoded_query}"
        return self.open_url(url)
