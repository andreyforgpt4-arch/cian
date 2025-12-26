from __future__ import annotations

import asyncio
import re

from playwright.async_api import async_playwright

from cian_bot.config import load_settings
from cian_bot.pw_utils import click_first_visible


async def main() -> None:
    """
    Opens the listing in an authorized browser, opens the message composer if possible,
    then pauses in Playwright Inspector so user can pick the exact input element.
    """
    s = load_settings()
    if not s.storage_state_path.exists():
        raise RuntimeError(
            f"Не найден {s.storage_state_path}. Сначала выполните: python -m cian_bot.login"
        )

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=False, slow_mo=s.slow_mo_ms or 0)
        context = await browser.new_context(storage_state=str(s.storage_state_path))
        page = await context.new_page()

        await page.goto(s.cian_ad_url, wait_until="domcontentloaded")
        await page.wait_for_timeout(1200)

        # Best-effort open composer
        await click_first_visible(
            [
                page.get_by_role("button", name=re.compile(r"^написать", re.I)),
                page.get_by_role("button", name=re.compile(r"написать сообщение", re.I)),
                page.get_by_role("link", name=re.compile(r"написать", re.I)),
                page.locator("text=Написать сообщение"),
                page.locator("text=Написать"),
            ],
            timeout_ms=2500,
        )

        print(
            "\nОткрыл объявление и (если смог) открыл чат.\n"
            "Сейчас откроется Playwright Inspector.\n\n"
            "Что сделать:\n"
            "1) В Inspector нажмите кнопку 'Pick locator' (значок прицела/пипетки).\n"
            "2) Наведите и кликните на ПОЛЕ ВВОДА сообщения в чате.\n"
            "3) В Inspector справа появится локатор — скопируйте его и пришлите сюда.\n"
            "4) Затем так же кликните на кнопку отправки/скрепку (если нужно) и пришлите локатор.\n\n"
            "После этого я зафиксирую эти локаторы в коде, чтобы ввод/отправка работали стабильно.\n",
            flush=True,
        )

        await page.pause()

        await context.close()
        await browser.close()


if __name__ == "__main__":
    asyncio.run(main())


