from __future__ import annotations

import asyncio
from pathlib import Path

from playwright.async_api import async_playwright

from cian_bot.config import load_settings


async def main() -> None:
    """
    Helper: opens the listing in authorized session, opens message composer,
    then asks you to click into the message input field. After you click,
    press Enter in the console and the script will print info about the focused element.
    """
    s = load_settings()
    if not s.storage_state_path.exists():
        raise RuntimeError(f"Не найден {s.storage_state_path}. Сначала: python -m cian_bot.login")

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=False, slow_mo=s.slow_mo_ms or 0)
        ctx = await browser.new_context(storage_state=str(s.storage_state_path))
        page = await ctx.new_page()

        await page.goto(s.cian_ad_url, wait_until="domcontentloaded")
        await page.wait_for_timeout(1200)

        print("\n1) В открытом браузере откройте чат (кнопка 'Написать').")
        print("2) Кликните мышкой в ПОЛЕ ВВОДА, куда нужно писать сообщение.")
        input("3) Вернитесь сюда и нажмите Enter — я выведу данные о выбранном элементе... ")

        info = await page.evaluate(
            """() => {
              const el = document.activeElement;
              if (!el) return null;
              const attrs = {};
              for (const a of el.attributes || []) attrs[a.name] = a.value;
              return {
                tag: el.tagName,
                isContentEditable: !!el.isContentEditable,
                id: el.id || null,
                className: el.className || null,
                role: el.getAttribute('role'),
                placeholder: el.getAttribute('placeholder'),
                ariaLabel: el.getAttribute('aria-label'),
                attrs,
                outerHTML: (el.outerHTML || '').slice(0, 5000)
              };
            }"""
        )

        print("\n=== ACTIVE ELEMENT ===")
        print(info)
        print("\nПодсказка: чаще всего подходят локаторы вида:")
        print("- page.get_by_placeholder('...')  или")
        print("- page.locator('[contenteditable=\"true\"]')  или")
        print("- page.get_by_role('textbox')\n")

        await ctx.close()
        await browser.close()


if __name__ == "__main__":
    asyncio.run(main())


