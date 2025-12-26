from __future__ import annotations

import asyncio
import re

from playwright.async_api import async_playwright

from cian_bot.config import load_settings
from cian_bot.pw_utils import click_first_visible


async def main() -> None:
    s = load_settings()
    if not s.storage_state_path.exists():
        raise RuntimeError(
            f"Не найден {s.storage_state_path}. Сначала выполните: python -m cian_bot.login"
        )

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=False, slow_mo=s.slow_mo_ms or 0)
        context = await browser.new_context(storage_state=str(s.storage_state_path))
        page = await context.new_page()

        await page.goto(s.dialogs_url, wait_until="domcontentloaded")
        await page.wait_for_timeout(1500)

        # Best-effort: try to open dialog by message snippet; otherwise open newest.
        snippet = s.cian_message.strip().replace("\n", " ")
        snippet = snippet[:24] if len(snippet) > 24 else snippet
        opened = False
        if snippet:
            try:
                opened = await click_first_visible([page.locator(f"text={snippet}")], timeout_ms=2000)
            except Exception:
                opened = False
        if not opened:
            await click_first_visible([page.locator('a[href*="/dialogs/"]').first], timeout_ms=4000)

        await page.wait_for_timeout(1200)

        print(
            "\nОткрыл /dialogs и попытался открыть диалог.\n"
            "Сейчас откроется Playwright Inspector.\n\n"
            "Что сделать:\n"
            "1) В Inspector нажмите 'Pick locator'.\n"
            "2) Кликните на кнопку ПРИКРЕПИТЬ (скрепка/плюс) внизу чата.\n"
            "3) Пришлите сюда локатор.\n"
            "4) Если после клика появляется меню/форма — снова 'Pick locator' и кликните на элемент,\n"
            "   который открывает выбор файла (или на input[type=file], если он есть) — пришлите локатор.\n"
            "5) Пришлите также локатор кнопки отправки вложения (если отличается от send_button).\n",
            flush=True,
        )

        # Pause for picking locators
        await page.pause()

        await context.close()
        await browser.close()


if __name__ == "__main__":
    asyncio.run(main())


