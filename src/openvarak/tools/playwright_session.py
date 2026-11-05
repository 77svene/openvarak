"""Persistent Playwright context confined to a dedicated owner thread."""

from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from openvarak.core.paths import get_config_dir
from openvarak.core.registry import ToolRegistry
from openvarak.core.types import ToolResult
from openvarak.tools._stubs import BaseTool, ToolSpec


@ToolRegistry.register("playwright_session")
class PlaywrightSessionTool(BaseTool):
    tool_id = "playwright_session"
    is_local = False

    def __init__(self, user_data_dir=None):
        self._directory = Path(user_data_dir or get_config_dir() / "browser")
        self._pool = ThreadPoolExecutor(
            max_workers=1, thread_name_prefix="varak-browser"
        )
        self._playwright = self._context = None
        self._closed = False

    @property
    def spec(self):
        return ToolSpec(
            name=self.tool_id,
            description="Persistent browser tabs, cookies, DOM, and auth sessions.",
            parameters={
                "type": "object",
                "properties": {
                    "action": {
                        "enum": [
                            "navigate",
                            "new_tab",
                            "snapshot",
                            "click",
                            "fill",
                            "tabs",
                            "close_tab",
                        ]
                    },
                    "url": {"type": "string"},
                    "tab": {"type": "integer"},
                    "selector": {"type": "string"},
                    "value": {"type": "string"},
                },
                "required": ["action"],
            },
            timeout_seconds=40,
        )

    def _operate(self, action, tab=0, url="", selector="", value=""):
        if self._context is None:
            from playwright.sync_api import sync_playwright

            self._directory.mkdir(parents=True, exist_ok=True, mode=0o700)
            self._playwright = sync_playwright().start()
            try:
                self._context = self._playwright.chromium.launch_persistent_context(
                    str(self._directory), headless=True
                )
                self._context.set_default_timeout(15000)
            except Exception:
                self._playwright.stop()
                self._playwright = None
                raise
        context = self._context
        if action == "tabs":
            return [{"tab": i, "url": page.url} for i, page in enumerate(context.pages)]
        if action == "new_tab":
            page = context.new_page()
        else:
            if not context.pages:
                context.new_page()
            if tab < 0 or tab >= len(context.pages):
                raise ValueError("Invalid tab index")
            page = context.pages[tab]
        if action in {"navigate", "new_tab"} and url:
            from urllib.parse import urlsplit

            if urlsplit(url).scheme not in {"http", "https"}:
                raise ValueError("Browser navigation requires an HTTP(S) URL")
            page.goto(url, wait_until="domcontentloaded", timeout=20000)
        elif action == "click":
            page.locator(selector).click()
        elif action == "fill":
            page.locator(selector).fill(value)
        elif action == "close_tab":
            page.close()
            return {"closed": tab}
        elif action not in {"snapshot", "new_tab", "navigate"}:
            raise ValueError("Unknown browser action")
        return {
            "url": page.url,
            "title": page.title(),
            "text": page.locator("body").inner_text()[:24000],
        }

    def execute(self, **params):
        if self._closed:
            return ToolResult(
                tool_name=self.tool_id, content="Browser session closed", success=False
            )
        try:
            value = self._pool.submit(self._operate, **params).result(timeout=35)
            return ToolResult(
                tool_name=self.tool_id, content=json.dumps(value), success=True
            )
        except Exception as exc:
            return ToolResult(tool_name=self.tool_id, content=str(exc), success=False)

    def close(self):
        if self._closed:
            return
        self._closed = True

        def shutdown():
            if self._context:
                self._context.close()
            if self._playwright:
                self._playwright.stop()

        self._pool.submit(shutdown).result(timeout=35)
        self._pool.shutdown(wait=True)
