from __future__ import annotations

import asyncio
import re
from pathlib import Path

from playwright.async_api import async_playwright

from cian_bot.config import load_settings


async def _wait_until_logged_in(page, timeout_s: int) -> None:
    """
    Wait until Cian looks authenticated.
    Heuristic: "Войти" link/button is no longer visible.
    """
    start = asyncio.get_running_loop().time()
    login_name = re.compile(r"войти", re.I)

    login_link = page.get_by_role("link", name=login_name)
    login_button = page.get_by_role("button", name=login_name)

    while True:
        elapsed = asyncio.get_running_loop().time() - start
        if elapsed > timeout_s:
            raise TimeoutError(
                f"Не дождались авторизации за {timeout_s} секунд. "
                "Проверьте, что вы вошли в аккаунт в открытом браузере."
            )

        try:
            is_login_visible = False
            try:
                is_login_visible = await login_link.first.is_visible()
            except Exception:
                pass
            if not is_login_visible:
                try:
                    is_login_visible = await login_button.first.is_visible()
                except Exception:
                    pass

            if not is_login_visible:
                return
        except Exception:
            # ignore transient DOM issues
            pass

        await page.wait_for_timeout(1000)


async def main() -> None:
    s = load_settings()
    state_path: Path = s.storage_state_path
    state_path.parent.mkdir(parents=True, exist_ok=True)

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=False, slow_mo=s.slow_mo_ms or 0)
        context = await browser.new_context()
        page = await context.new_page()

        await page.goto("https://novosibirsk.cian.ru/", wait_until="domcontentloaded")

        print("\nОткрыл Циан в браузере.")
        print("Войдите в аккаунт вручную (если нужно — пройдите капчу).")
        print("Я подожду авторизацию и автоматически сохраню сессию в файл.\n")

        # Wait up to 15 minutes for manual login
        await _wait_until_logged_in(page, timeout_s=15 * 60)

        await context.storage_state(path=str(state_path))
        await browser.close()

    print(f"\nГотово. Сохранено: {state_path}")


if __name__ == "__main__":
    asyncio.run(main())


