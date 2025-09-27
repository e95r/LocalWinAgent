"""Автоматизация браузера через Playwright."""
from __future__ import annotations

import logging
from contextlib import contextmanager
from typing import Optional

try:  # pragma: no cover - импорт Playwright может отсутствовать в тестовой среде
    from playwright.sync_api import TimeoutError, sync_playwright
    PLAYWRIGHT_AVAILABLE = True
except ModuleNotFoundError:  # pragma: no cover
    TimeoutError = RuntimeError  # type: ignore[assignment]

    def sync_playwright():  # type: ignore[override]
        raise ModuleNotFoundError("Playwright не установлен")

    PLAYWRIGHT_AVAILABLE = False

logger = logging.getLogger(__name__)


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
