from __future__ import annotations

import asyncio
import re

from playwright.async_api import async_playwright

from cian_bot.config import load_settings
from cian_bot.pw_utils import click_first_visible


async def main() -> None:
    """
    Opens listing -> opens chat -> pauses so user can click Send to trigger reCAPTCHA,
    then use Pick locator to select the captcha iframe/container.
    """
    s = load_settings()
    if not s.storage_state_path.exists():
        raise RuntimeError(
            f"Не найден {s.storage_state_path}. Сначала выполните: python -m cian_bot.login"
        )

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=False, slow_mo=s.slow_mo_ms or 0)
        ctx = await browser.new_context(storage_state=str(s.storage_state_path))
        page = await ctx.new_page()

        await page.goto(s.cian_ad_url, wait_until="domcontentloaded")
        await page.wait_for_timeout(1200)

        await click_first_visible(
            [
                page.get_by_role("button", name=re.compile(r"^написать", re.I)),
                page.get_by_role("button", name=re.compile(r"написать сообщение", re.I)),
                page.get_by_role("link", name=re.compile(r"написать", re.I)),
                page.locator("text=Написать сообщение"),
                page.locator("text=Написать"),
            ],
            timeout_ms=3000,
        )

        print(
            "\nИнструкция:\n"
            "1) Сейчас откроется Inspector (пауза).\n"
            "2) В браузере нажмите 'Отправить' (можно без текста) — чтобы спровоцировать капчу.\n"
            "3) В Inspector нажмите 'Pick locator' и кликните на iframe/контейнер reCAPTCHA.\n"
            "4) Пришлите сюда локатор (iframe selector или контейнер).\n",
            flush=True,
        )

        await page.pause()

        await ctx.close()
        await browser.close()


if __name__ == "__main__":
    asyncio.run(main())


