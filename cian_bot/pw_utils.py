from __future__ import annotations

import re
from pathlib import Path
from typing import Iterable, Optional

from playwright.async_api import Locator, Page


async def safe_screenshot(page: Page, name: str) -> None:
    Path("screenshots").mkdir(parents=True, exist_ok=True)
    safe = re.sub(r"[^a-zA-Z0-9_.-]+", "_", name)[:80]
    try:
        await page.screenshot(path=str(Path("screenshots") / f"{safe}.png"), full_page=True)
    except Exception:
        # If page/context/browser already closed (TargetClosedError), don't mask the real error.
        return


async def click_first_visible(locators: Iterable[Locator], timeout_ms: int = 2000) -> bool:
    for loc in locators:
        try:
            await loc.first.wait_for(state="visible", timeout=timeout_ms)
            await loc.first.click()
            return True
        except Exception:
            continue
    return False


async def find_dialog_id_from_url(url: str) -> Optional[str]:
    # Expected patterns:
    # - https://novosibirsk.cian.ru/dialogs/123456789/
    # - https://novosibirsk.cian.ru/dialogs/123456789/?foo=bar
    m = re.search(r"/dialogs/(\d+)(?:/|\?|$)", url)
    return m.group(1) if m else None


