from __future__ import annotations

import asyncio
import os
import re
from pathlib import Path

from playwright.async_api import FrameLocator, async_playwright, Locator, Page

from cian_bot.config import load_settings
from cian_bot.captcha import wait_for_recaptcha_to_be_solved, is_recaptcha_visible, is_visual_captcha_challenge_visible
from cian_bot.captcha_click_solve import solve_captcha_with_click_and_visual_check
from cian_bot.captcha_solver import find_recaptcha_response_context
from cian_bot.google_drive import download_public_file
from cian_bot.pw_utils import click_first_visible, find_dialog_id_from_url, safe_screenshot
from cian_bot.state_store import StateStore


def _extract_ad_id(url: str) -> str | None:
    m = re.search(r"/(\d+)(?:/|\\?|$)", url)
    return m.group(1) if m else None


async def _extract_dialog_id_from_chatmodal(chat_frame: FrameLocator) -> str | None:
    """
    Best-effort: in ChatModal header there is often a link to /dialogs/<id>.
    """
    try:
        href = await chat_frame.locator('a[href*="/dialogs/"]').first.get_attribute("href")
        if href:
            return await find_dialog_id_from_url(href)
    except Exception:
        pass
    return None


async def _chat_context(page: Page):
    """
    Returns FrameLocator for ChatModal if present, else the Page itself.
    """
    try:
        if await page.locator('[data-testid="ChatModal"]').count() > 0:
            return page.frame_locator('[data-testid="ChatModal"]')
    except Exception:
        pass
    return page


async def _has_attachment_ui(page: Page) -> bool:
    """
    Check if current view has chat composer with attachment button.
    Works both for ChatModal iframe and non-iframe dialogs.
    """
    try:
        ctx = await _chat_context(page)
        btn = ctx.locator(".x61f99309--_86555--attach_button").first
        await btn.wait_for(state="visible", timeout=1200)
        return True
    except Exception:
        return False


async def _wait_after_send(
    *,
    page: Page,
    chat_frame: FrameLocator,
    textbox: Locator,
    send_button: Locator,
    message: str,
    timeout_s: int = 30,
) -> None:
    """
    After clicking "Send", Cian may show reCAPTCHA slightly позже.
    We must NOT navigate away until either:
    - message is actually sent (input cleared / send disabled), OR
    - user solves reCAPTCHA and we retry sending.
    """
    deadline = asyncio.get_running_loop().time() + timeout_s
    snippet = message.strip().replace("\n", " ")[:24]

    while True:
        # If captcha appears slightly after send (including inside ChatModal iframe),
        # solve it with click and visual check, then retry send.
        captcha_visible = await is_recaptcha_visible(page)
        if captcha_visible:
            print("[SEND] Капча появилась после нажатия 'Отправить'. Решаю капчу...", flush=True)
            solved = await solve_captcha_with_click_and_visual_check(page, timeout_s=300)
            if solved:
                print("[SEND] ✓ Капча решена!", flush=True)
            else:
                print("[SEND] ⚠ Капча не решена полностью, но продолжаю...", flush=True)
            had_captcha = True
        else:
            had_captcha = False
        # Critical: after solving captcha (manually or automatically) user must re-submit.
        if had_captcha:
            print("[SEND] Капча была решена, повторяю отправку сообщения...", flush=True)
            # Wait longer for captcha to fully disappear and form to be ready
            await page.wait_for_timeout(2000)
            
            # Verify captcha is gone OR token is set
            captcha_still_visible = await is_recaptcha_visible(page)
            response_context = await find_recaptcha_response_context(page)
            token_set = bool(response_context and response_context.get("details", {}).get("filled"))
            
            if captcha_still_visible and not token_set:
                print("[SEND] ВНИМАНИЕ: Капча все еще видна после решения! Возможно требуется ручное вмешательство.", flush=True)
                await page.wait_for_timeout(1000)
                continue
            if token_set:
                context_info = response_context.get("context") if response_context else "unknown"
                context_url = response_context.get("url") if response_context else "unknown"
                print(
                    "[SEND] Токен установлен, форма готова к отправке "
                    f"(context={context_info}, url={context_url}).",
                    flush=True,
                )
            else:
                print("[SEND] Капча исчезла, но токен не подтвержден; жду подтверждения перед отправкой.", flush=True)
                await page.wait_for_timeout(1000)
                continue
            
            # Try to resend multiple times if needed
            for retry_attempt in range(3):
                try:
                    # Make sure send button is still available
                    await send_button.wait_for(state="visible", timeout=3000)
                    await send_button.click(timeout=2500)
                    print(f"[SEND] Повторная отправка выполнена (попытка {retry_attempt + 1})", flush=True)
                    
                    # CRITICAL: Wait and check if message was actually sent
                    await page.wait_for_timeout(3000)  # Increased wait time
                    
                    # Check if textbox is cleared (indicates message was sent)
                    textbox_cleared = False
                    try:
                        textbox_text = await textbox.input_value() if await _is_editable(textbox) else await textbox.text_content()
                        if not textbox_text or textbox_text.strip() == "":
                            textbox_cleared = True
                            print("[SEND] Текстбокс очищен - сообщение отправлено", flush=True)
                    except Exception:
                        pass
                    
                    # Also check if message appeared in chat
                    message_in_chat = False
                    if snippet:
                        try:
                            if await chat_frame.locator(f"text={snippet}").first.is_visible(timeout=2000):
                                message_in_chat = True
                                print("[SEND] Сообщение найдено в чате", flush=True)
                        except Exception:
                            pass
                    
                    # Check if new captcha appeared (means previous attempt failed)
                    new_captcha = await is_recaptcha_visible(page)
                    if new_captcha:
                        print("[SEND] ⚠ Появилась новая капча - предыдущая попытка не удалась", flush=True)
                        if retry_attempt < 2:
                            print("[SEND] Повторяю попытку...", flush=True)
                            continue
                    
                    # Success if textbox cleared OR message in chat AND no new captcha
                    if (textbox_cleared or message_in_chat) and not new_captcha:
                        print("[SEND] Сообщение успешно отправлено после решения капчи!", flush=True)
                        return
                    elif new_captcha:
                        print("[SEND] ⚠ Новая капча появилась, требуется повторное решение", flush=True)
                        if retry_attempt < 2:
                            continue
                    
                    # If message not visible yet, wait a bit more
                    if retry_attempt < 2:
                        await page.wait_for_timeout(1500)
                    
                except Exception as e:
                    print(f"[SEND] Ошибка при повторной отправке (попытка {retry_attempt + 1}): {e}", flush=True)
                    if retry_attempt < 2:
                        await page.wait_for_timeout(1000)

        # Success condition: message appears in the chat thread AND no captcha is visible.
        if snippet:
            try:
                # CRITICAL: Check for captcha FIRST before checking message
                captcha_visible = await is_recaptcha_visible(page)
                visual_challenge = await is_visual_captcha_challenge_visible(page)
                
                if captcha_visible or visual_challenge:
                    print(f"[SEND] ⚠ Капча все еще видна! captcha={captcha_visible}, visual={visual_challenge}", flush=True)
                    print("[SEND] Решаю капчу перед проверкой сообщения...", flush=True)
                    await solve_captcha_with_click_and_visual_check(page, timeout_s=300)
                    # После решения капчи нужно повторно отправить сообщение
                    print("[SEND] Капча решена, повторяю отправку...", flush=True)
                    await send_button.click(timeout=2500)
                    await page.wait_for_timeout(3000)
                
                # Проверяем сообщение только если капчи нет
                captcha_visible = await is_recaptcha_visible(page)
                visual_challenge = await is_visual_captcha_challenge_visible(page)
                
                if not captcha_visible and not visual_challenge:
                    if await chat_frame.locator(f"text={snippet}").first.is_visible(timeout=2000):
                        print("[SEND] Сообщение успешно отправлено (текст появился в чате, капчи нет)", flush=True)
                        return
                    else:
                        print("[SEND] ⚠ Сообщение не найдено в чате, но капчи нет", flush=True)
                else:
                    print(f"[SEND] ⚠ Капча все еще видна после решения! captcha={captcha_visible}, visual={visual_challenge}", flush=True)
            except Exception as e:
                # Check if browser/page was closed
                error_msg = str(e)
                if "closed" in error_msg.lower() or "Target page" in error_msg:
                    raise RuntimeError("Браузер или страница закрыты во время проверки отправки сообщения")
                pass

        if asyncio.get_running_loop().time() > deadline:
            # Check one more time if captcha is blocking
            captcha_visible = await is_recaptcha_visible(page)
            if captcha_visible:
                raise RuntimeError("Таймаут отправки сообщения: капча все еще видна и не решена автоматически. Сообщение не отправлено.")
            raise TimeoutError("Не дождались подтверждения отправки сообщения (текст не появился в чате). Возможно капча не была решена корректно.")

        # Not sent yet: retry clicking send (in case first click was blocked by captcha/overlay),
        # then wait a bit and loop.
        try:
            await send_button.click(timeout=1500)
        except Exception:
            pass
        await page.wait_for_timeout(750)


async def _dismiss_common_popups(page: Page) -> None:
    # Cookie / consent banners can cover the UI and block clicks/typing.
    await click_first_visible(
        [
            page.get_by_role("button", name=re.compile(r"принять", re.I)),
            page.get_by_role("button", name=re.compile(r"соглас", re.I)),
            page.get_by_role("button", name=re.compile(r"ok", re.I)),
            page.locator("text=Принять").locator("..").get_by_role("button"),
        ],
        timeout_ms=800,
    )


async def _is_editable(locator: Locator) -> bool:
    try:
        return bool(
            await locator.evaluate(
                "el => !!(el && (el.isContentEditable || el.tagName === 'TEXTAREA' || (el.tagName === 'INPUT' && el.type === 'text')))"
            )
        )
    except Exception:
        return False


async def _ensure_storage_state_exists(path: Path) -> None:
    if not path.exists():
        raise RuntimeError(
            f"Не найден STORAGE_STATE_PATH: {path}\n"
            "Сначала выполните: python -m cian_bot.login"
        )


async def _open_listing_and_send_message(page: Page, ad_url: str, message: str) -> str | None:
    print("  - navigating to listing...", flush=True)
    await page.goto(ad_url, wait_until="domcontentloaded")
    await page.wait_for_timeout(1200)
    await _dismiss_common_popups(page)

    # Try open message composer.
    print("  - opening message composer...", flush=True)
    opened = await click_first_visible(
        [
            page.get_by_role("button", name=re.compile(r"^написать", re.I)),
            page.get_by_role("button", name=re.compile(r"написать сообщение", re.I)),
            page.get_by_role("link", name=re.compile(r"написать", re.I)),
            page.locator("text=Написать сообщение"),
            page.locator("text=Написать"),
            page.locator("text=Сообщение"),
        ],
        timeout_ms=2500,
    )
    if not opened:
        # Sometimes the chat panel is already open; continue to search for the input before failing.
        print("  - composer button not found/clicked; trying to find input anyway...", flush=True)
    else:
        # Give the modal/iframe a moment to appear.
        try:
            await page.locator('[data-testid="ChatModal"]').wait_for(state="visible", timeout=3500)
        except Exception:
            pass

    # If reCAPTCHA appears before interacting with chat, solve it with click and visual check.
    captcha_before = await is_recaptcha_visible(page)
    if captcha_before:
        print("  - капча обнаружена перед отправкой, решаю...", flush=True)
        await solve_captcha_with_click_and_visual_check(page, timeout_s=300)

    # Optional: pause here so you can visually point the correct input/button in Playwright Inspector.
    # Set env DEBUG_PAUSE=true to enable.
    if os.getenv("DEBUG_PAUSE", "").strip().lower() in {"1", "true", "yes", "y", "on"}:
        print("  - DEBUG_PAUSE enabled: Playwright Inspector opened. Use 'Pick locator' and Resume.", flush=True)
        await page.pause()

    # Preferred exact locators provided by user (inside ChatModal frame).
    # These are more stable than heuristic searching.
    try:
        chat_frame = page.frame_locator('[data-testid="ChatModal"]')
        # Label can vary; choose the first textbox within the ChatModal frame.
        exact_textbox = chat_frame.get_by_role("textbox").first
        exact_send = chat_frame.get_by_test_id("send_button")

        await exact_textbox.wait_for(state="visible", timeout=5000)
        print("  - typing message (exact ChatModal locator)...", flush=True)
        await exact_textbox.click()
        try:
            await exact_textbox.fill("")
        except Exception:
            pass
        await exact_textbox.type(message, delay=10)
        
        # ШАГ 2: Нажатие на отправить
        print("  - sending message (exact send_button)...", flush=True)
        await exact_send.click(timeout=2500)
        await page.wait_for_timeout(2000)  # Ждем появления капчи если она появится
        
        # ШАГ 3: Если появилась капча - сначала решаем через 2captcha (вводим токен)
        captcha_visible = await is_recaptcha_visible(page)
        if captcha_visible:
            print("  - капча появилась после отправки, решаю через 2captcha...", flush=True)
            solver_service = os.getenv("CAPTCHA_SOLVER_SERVICE", "2captcha").strip().lower()
            solver_api_key = os.getenv("CAPTCHA_SOLVER_API_KEY", "").strip()
            token_injected = False
            
            if solver_api_key:
                try:
                    from cian_bot.captcha_solver import CaptchaSolver, extract_recaptcha_site_key, inject_recaptcha_token
                    solver = CaptchaSolver(service=solver_service, api_key=solver_api_key)
                    site_key = await extract_recaptcha_site_key(page)
                    if site_key:
                        print(f"  - site key найден: {site_key[:30]}...", flush=True)
                        token = await solver.solve_recaptcha_v2(page.url, site_key, timeout_s=180)
                        if token:
                            print("  - токен получен от 2captcha, ввожу в страницу...", flush=True)
                            token_injected = await inject_recaptcha_token(page, token)
                            if token_injected:
                                print("  - токен введен успешно", flush=True)
                                await page.wait_for_timeout(2000)
                except Exception as e:
                    print(f"  - ошибка решения через 2captcha: {e}", flush=True)
            
            # ШАГ 4: После ввода токена кликаем на чекбокс (чтобы активировать форму)
            if token_injected:
                print("  - кликаю на чекбокс после ввода токена...", flush=True)
                clicked = await click_recaptcha_checkbox(page)
                if clicked:
                    await page.wait_for_timeout(3000)  # Ждем реакции
            else:
                # Если токен не введен - пробуем кликнуть на чекбокс вручную
                print("  - токен не введен, кликаю на чекбокс вручную...", flush=True)
                clicked = await click_recaptcha_checkbox(page)
                if clicked:
                    await page.wait_for_timeout(3000)
            
            # ШАГ 5: Проверяем появилась ли графическая капча после клика на чекбокс
            visual_challenge = await is_visual_captcha_challenge_visible(page)
            if visual_challenge:
                print("  - появилась графическая капча после клика на чекбокс, решаю через 2captcha...", flush=True)
                if solver_api_key:
                    try:
                        solver = CaptchaSolver(service=solver_service, api_key=solver_api_key)
                        site_key = await extract_recaptcha_site_key(page)
                        if site_key:
                            token = await solver.solve_recaptcha_v2(page.url, site_key, timeout_s=180)
                            if token:
                                await inject_recaptcha_token(page, token)
                                await page.wait_for_timeout(5000)
                                # Проверяем решена ли
                                visual_still = await is_visual_captcha_challenge_visible(page)
                                if visual_still:
                                    print("  - ⚠ графическая капча не решена автоматически, делаю паузу...", flush=True)
                                    await page.pause()
                    except Exception as e:
                        print(f"  - ошибка решения графической капчи: {e}", flush=True)
                        print("  - делаю паузу для ручного решения...", flush=True)
                        await page.pause()
            
            # ШАГ 6: Проверяем что капча решена и нажимаем отправить
            captcha_visible = await is_recaptcha_visible(page)
            visual_challenge = await is_visual_captcha_challenge_visible(page)
            
            if not captcha_visible and not visual_challenge:
                print("  - капча решена, нажимаю отправить...", flush=True)
                await exact_send.click(timeout=2500)
                await page.wait_for_timeout(3000)
            elif captcha_visible or visual_challenge:
                print("  - ⚠ капча все еще видна, делаю паузу для ручного решения...", flush=True)
                await page.pause()
                # После паузы пробуем отправить еще раз
                await exact_send.click(timeout=2500)
                await page.wait_for_timeout(3000)
        
        # Проверяем что сообщение действительно отправлено
        snippet = message.strip().replace("\n", " ")[:24]
        message_sent = False
        for check_attempt in range(10):  # До 5 секунд
            captcha_visible = await is_recaptcha_visible(page)
            visual_challenge = await is_visual_captcha_challenge_visible(page)
            
            if not captcha_visible and not visual_challenge:
                if snippet:
                    try:
                        if await chat_frame.locator(f"text={snippet}").first.is_visible(timeout=2000):
                            print(f"  - ✓ Сообщение подтверждено: текст '{snippet}' найден в чате", flush=True)
                            message_sent = True
                            break
                    except Exception:
                        pass
            
            await page.wait_for_timeout(500)
        
        if not message_sent:
            # Финальная проверка капчи
            captcha_visible = await is_recaptcha_visible(page)
            visual_challenge = await is_visual_captcha_challenge_visible(page)
            if captcha_visible or visual_challenge:
                raise RuntimeError("Сообщение НЕ было отправлено! Капча не решена.")
            else:
                raise RuntimeError("Сообщение НЕ было отправлено! Текст не найден в чате.")
        
        # Prefer extracting dialog id from the chat modal itself AFTER message is confirmed sent.
        dialog_id = await _extract_dialog_id_from_chatmodal(chat_frame)
        if dialog_id:
            print(f"  - ✓ extracted dialog_id from ChatModal: {dialog_id}", flush=True)
            return dialog_id
        
        # fallback: may still be present in url in some flows
        dialog_id = await find_dialog_id_from_url(page.url)
        if dialog_id:
            print(f"  - ✓ extracted dialog_id from URL: {dialog_id}", flush=True)
            return dialog_id
        
        print("  - ✗ ВНИМАНИЕ: dialog_id не найден! Будет использован ad_id для поиска чата.", flush=True)
        return None  # Will use ad_id fallback in _open_dialog_from_list
    except Exception:
        # Fall back to heuristic locator strategy below.
        pass

    # Find message input. On Cian it can be textarea OR a contenteditable div.
    dialog_root = page.locator('[role="dialog"]').first
    placeholder_rx = re.compile(r"написать|сообщени", re.I)
    textbox_candidates: list[Locator] = [
        # Placeholder-based (works for input/textarea with placeholder)
        page.get_by_placeholder(placeholder_rx),
        dialog_root.get_by_placeholder(placeholder_rx),
        # ARIA textbox role
        dialog_root.get_by_role("textbox"),
        # Explicit textarea/input
        dialog_root.locator("textarea"),
        dialog_root.locator("input[type='text']"),
        # Custom chat editor
        dialog_root.locator('[contenteditable="true"]'),
    ]

    textbox: Locator | None = None
    for loc in textbox_candidates:
        try:
            await loc.first.wait_for(state="visible", timeout=4000)
            textbox = loc.first
            break
        except Exception:
            continue

    if textbox is None:
        await safe_screenshot(page, "cannot_find_textbox")
        raise RuntimeError("Не удалось найти поле ввода сообщения (textarea/placeholder/contenteditable).")

    # If the matched element isn't directly editable, try to find an editable child.
    if not await _is_editable(textbox):
        nested = textbox.locator('textarea, input[type="text"], [contenteditable="true"]').first
        try:
            await nested.wait_for(state="visible", timeout=1500)
            if await _is_editable(nested):
                textbox = nested
        except Exception:
            pass

    # Robust typing: click + clear + type. Some contenteditable editors ignore .fill().
    print("  - typing message...", flush=True)
    await textbox.click()
    try:
        await textbox.fill("")
    except Exception:
        pass
    try:
        await textbox.press("Control+A")
        await textbox.press("Backspace")
    except Exception:
        pass
    await textbox.type(message, delay=10)

    # Try to send: click button if present; otherwise press Enter/Ctrl+Enter.
    print("  - sending message...", flush=True)
    sent = await click_first_visible(
        [
            dialog_root.get_by_role("button", name=re.compile(r"отправить", re.I)),
            page.get_by_role("button", name=re.compile(r"отправить", re.I)),
            page.locator('[aria-label*="Отправить" i]'),
            page.locator('[data-testid*="send" i]'),
            page.locator('button[type="submit"]'),
        ],
        timeout_ms=2500,
    )
    if not sent:
        # Common in chat UIs: Enter sends.
        try:
            await textbox.press("Enter")
            sent = True
        except Exception:
            sent = False
    if not sent:
        try:
            await textbox.press("Control+Enter")
            sent = True
        except Exception:
            sent = False
    if not sent:
        await safe_screenshot(page, "cannot_send_message")
        raise RuntimeError("Не удалось отправить сообщение (кнопка/Enter/Ctrl+Enter).")

    # Give time to send and potentially navigate to dialogs
    await page.wait_for_timeout(1500)

    dialog_id = await find_dialog_id_from_url(page.url)
    if dialog_id:
        return dialog_id

    # Try to discover dialogs link on the page
    dialogs_link = page.locator('a[href*="/dialogs/"]').first
    try:
        href = await dialogs_link.get_attribute("href")
        if href:
            dialog_id = await find_dialog_id_from_url(href)
            return dialog_id
    except Exception:
        pass

    return None


async def _open_dialog_from_list(
    page: Page, dialogs_url: str, dialog_id: str | None, message: str, ad_url: str
) -> None:
    print("  - navigating to /dialogs...", flush=True)
    await page.goto(dialogs_url, wait_until="domcontentloaded")
    await page.wait_for_timeout(2000)  # Increased wait time for dialogs to load
    await _dismiss_common_popups(page)
    
    # CRITICAL: Wait for dialogs list to fully load, especially if we just sent a message
    print("  - ожидание загрузки списка диалогов...", flush=True)
    try:
        # Wait for at least one dialog item to appear
        await page.locator(".x61f99309--ba1666--item-container").first.wait_for(state="visible", timeout=5000)
        await page.wait_for_timeout(1000)  # Additional wait for list to stabilize
    except Exception:
        print("  - предупреждение: список диалогов не загрузился быстро", flush=True)

    if dialog_id:
        print(f"  - trying dialog_id link: {dialog_id}", flush=True)
        # First try click the dialog in the list, otherwise navigate directly.
        target = page.locator(f'a[href*="/dialogs/{dialog_id}"]').first
        try:
            await target.wait_for(state="visible", timeout=5000)
            await target.click()
            await page.wait_for_timeout(800)
            if await _has_attachment_ui(page):
                return
            # dialog opened but composer not visible yet; continue with fallbacks
        except Exception:
            # fall back to heuristics below
            pass

        # Direct navigation fallback
        try:
            direct = dialogs_url.rstrip("/") + f"/{dialog_id}/"
            print(f"  - direct goto dialog: {direct}", flush=True)
            await page.goto(direct, wait_until="domcontentloaded")
            await page.wait_for_timeout(1200)
            if await _has_attachment_ui(page):
                return
        except Exception:
            pass

    # CRITICAL: Try to locate the dialog by ad id on the dialogs page (MOST RELIABLE METHOD).
    # This is preferred over dialog_id when dialog_id is None or unreliable.
    ad_id = _extract_ad_id(ad_url)
    if ad_id:
        print(f"  - trying to find dialog by ad_id: {ad_id} (PRIORITY METHOD)", flush=True)
        try:
            # Method 1: Find listing link with ad_id, then find parent dialog container
            listing_links = page.locator(f'a[href*="/{ad_id}/"]')
            count = await listing_links.count()
            print(f"  - найдено ссылок с ad_id: {count}", flush=True)
            
            for i in range(count):
                try:
                    listing_link = listing_links.nth(i)
                    href = await listing_link.get_attribute("href")
                    if href and ad_id in href:
                        print(f"  - проверяю ссылку {i}: {href[:80]}...", flush=True)
                        # Find the dialog container that contains this listing link
                        # Strategy 1: Try to find item-container that contains this listing link
                        try:
                            # Find item-container elements and check which one contains our listing link
                            item_containers = page.locator(".x61f99309--ba1666--item-container")
                            container_count = await item_containers.count()
                            print(f"  - найдено item-container элементов: {container_count}", flush=True)
                            
                            for j in range(container_count):
                                try:
                                    container = item_containers.nth(j)
                                    # Check if this container contains our listing link
                                    container_listing = container.locator(f'a[href*="/{ad_id}/"]')
                                    if await container_listing.count() > 0:
                                        print(f"  - ✓ найден item-container {j} с ad_id {ad_id}", flush=True)
                                        # Click on the container (it should open the dialog)
                                        await container.click(timeout=3000)
                                        await page.wait_for_timeout(1000)
                                        if await _has_attachment_ui(page):
                                            print(f"  - ✓ диалог открыт через item-container, UI вложения доступен", flush=True)
                                            return
                                        else:
                                            print(f"  - ✗ диалог открыт, но UI вложения не найден", flush=True)
                                            break
                                except Exception as e:
                                    continue
                        except Exception as e:
                            print(f"  - ошибка поиска через item-container: {e}", flush=True)
                        
                        # Strategy 2: Find ancestor with dialog link
                        try:
                            parent = listing_link.locator('xpath=ancestor::*[.//a[contains(@href,"/dialogs/")]][1]')
                            if await parent.count() > 0:
                                dialog_link = parent.locator('a[href*="/dialogs/"]').first
                                if await dialog_link.count() > 0:
                                    dialog_href = await dialog_link.get_attribute("href")
                                    print(f"  - ✓ найден диалог через ancestor: {dialog_href}", flush=True)
                                    await dialog_link.click(timeout=3000)
                                    await page.wait_for_timeout(1000)
                                    if await _has_attachment_ui(page):
                                        print(f"  - ✓ диалог открыт, UI вложения доступен", flush=True)
                                        return
                        except Exception as e:
                            print(f"  - ошибка поиска через ancestor: {e}", flush=True)
                except Exception as e:
                    print(f"  - ошибка при проверке ссылки {i}: {e}", flush=True)
                    continue
        except Exception as e:
            print(f"  - ошибка поиска диалога по ad_id: {e}", flush=True)

    snippet = message.strip().replace("\n", " ")
    snippet = snippet[:24] if len(snippet) > 24 else snippet
    if snippet:
        print(f"  - trying to find dialog by snippet: {snippet!r}", flush=True)
        preview = page.locator(f"text={snippet}").first
        try:
            await preview.wait_for(state="visible", timeout=2500)
            await preview.click()
            await page.wait_for_timeout(800)
            if await _has_attachment_ui(page):
                return
            # snippet matched, but composer not visible; continue scanning
        except Exception:
            pass

    # LAST RESORT: Only if ad_id method failed, try to find dialog by message snippet
    # But ONLY if we have a snippet and ad_id search failed
    snippet = message.strip().replace("\n", " ")
    snippet = snippet[:24] if len(snippet) > 24 else snippet
    if snippet and ad_id:
        print(f"  - fallback: trying to find dialog by snippet: {snippet!r}", flush=True)
        preview = page.locator(f"text={snippet}").first
        try:
            await preview.wait_for(state="visible", timeout=2500)
            # Find the dialog link near this snippet
            dialog_link = preview.locator('xpath=ancestor::*[.//a[contains(@href,"/dialogs/")]][1]//a[contains(@href,"/dialogs/")][1]')
            if await dialog_link.count() == 0:
                dialog_link = page.locator(f'a[href*="/dialogs/"]').filter(has=preview).first
            if await dialog_link.count() > 0:
                await dialog_link.click(timeout=3000)
                await page.wait_for_timeout(800)
                if await _has_attachment_ui(page):
                    print(f"  - ✓ диалог найден по snippet", flush=True)
                    return
        except Exception as e:
            print(f"  - ошибка поиска по snippet: {e}", flush=True)

    # FINAL FALLBACK: Only if everything else failed, iterate dialogs
    # But ONLY check first 3 dialogs and verify they match ad_id
    print("  - FINAL FALLBACK: scanning first 3 dialogs (with ad_id verification)...", flush=True)
    dialogs = page.locator('a[href*="/dialogs/"]')
    try:
        count = await dialogs.count()
        for i in range(min(count, 3)):  # Only check first 3, not 12!
            try:
                dialog_href = await dialogs.nth(i).get_attribute("href")
                print(f"  - проверяю диалог {i}: {dialog_href}", flush=True)
                
                # Click and check if it has our ad_id or message snippet
                await dialogs.nth(i).click(timeout=3000)
                await page.wait_for_timeout(1000)
                
                # Verify this is the right dialog by checking for ad_id
                is_correct = False
                if ad_id:
                    try:
                        # Check if current page/dialog contains link to our ad
                        ad_link = page.locator(f'a[href*="/{ad_id}/"]').first
                        if await ad_link.is_visible(timeout=2000):
                            is_correct = True
                            print(f"  - ✓ диалог {i} содержит ссылку на ad_id {ad_id}", flush=True)
                    except Exception:
                        pass
                
                if is_correct and await _has_attachment_ui(page):
                    print(f"  - ✓ выбран правильный диалог index={i}", flush=True)
                    return
                else:
                    print(f"  - ✗ диалог {i} не подходит, продолжаю поиск...", flush=True)
            except Exception as e:
                print(f"  - ошибка при проверке диалога {i}: {e}", flush=True)
                continue
    except Exception as e:
        print(f"  - ошибка при сканировании диалогов: {e}", flush=True)

    if os.getenv("DEBUG_PAUSE", "").strip().lower() in {"1", "true", "yes", "y", "on"}:
        print("  - DEBUG_PAUSE: couldn't auto-open dialog; pause so you can click the correct chat.", flush=True)
        await page.pause()

    await safe_screenshot(page, "cannot_open_dialog_with_composer")
    raise RuntimeError("Не удалось открыть нужный диалог в /dialogs (не найден чат с кнопкой прикрепления).")


async def _attach_image_and_send(page: Page, image_path: Path) -> None:
    image_path = image_path.resolve()
    if not image_path.exists():
        raise RuntimeError(f"Файл картинки не найден: {image_path}")

    print("  - attaching image...", flush=True)
    await wait_for_recaptcha_to_be_solved(
        page,
        note="Капча может появиться перед отправкой вложения.",
    )

    async def _try_attach_using_user_locators(context) -> bool:
        """
        User-provided locators:
        1) .x61f99309--_86555--attach_button  (attach)
        2) label has_text "Фото"              (choose photo type)
        Then OS file chooser appears.
        """
        attach_btn = context.locator(".x61f99309--_86555--attach_button").first
        photo_choice = context.locator("label").filter(has_text="Фото").first

        try:
            print("    - waiting for attach button...", flush=True)
            await attach_btn.wait_for(state="visible", timeout=5000)
            await attach_btn.click()
            
            print("    - waiting for 'Photo' choice...", flush=True)
            # Иногда нужно подождать, пока анимация меню закончится
            await page.wait_for_timeout(500)
            await photo_choice.wait_for(state="visible", timeout=5000)

            print("    - opening file chooser...", flush=True)
            async with page.expect_file_chooser(timeout=10000) as fc_info:
                await photo_choice.click()
            fc = await fc_info.value
            await fc.set_files(str(image_path))
            print("    - file selected.", flush=True)
            await page.wait_for_timeout(2000) # Даем время на превью
            return True
        except Exception as e:
            error_msg = str(e)
            # Check if browser/page was closed
            if "closed" in error_msg.lower() or "Target page" in error_msg:
                print(f"    - attach attempt failed: браузер или страница закрыты: {e}", flush=True)
                raise  # Re-raise to handle at higher level
            print(f"    - attach attempt failed: {e}", flush=True)
            return False

    async def _try_send_after_attach(context) -> bool:
        """
        Отправка фото с обработкой капчи по порядку:
        1. Нажатие на отправить
        2. Если появилась капча - клик на чекбокс
        3. Если графическая капча - решаем её
        4. Если галочка - нажимаем отправить еще раз
        5. Проверяем что фото отправлено
        """
        # ШАГ 2: Нажатие на отправить
        print("    - clicking send after attachment...", flush=True)
        send_btn = None
        try:
            send_btn = context.get_by_test_id("send_button").first
            await send_btn.wait_for(state="visible", timeout=5000)
            await send_btn.click(timeout=2500)
        except Exception:
            # Fallback
            sent = await click_first_visible(
                [
                    context.get_by_role("button", name=re.compile(r"отправить", re.I)),
                    context.locator("text=Отправить"),
                ],
                timeout_ms=3000,
            )
            if not sent:
                return False
        
        await page.wait_for_timeout(2000)  # Ждем появления капчи если она появится
        
        # ШАГ 3: Если появилась капча - сначала решаем через 2captcha (вводим токен)
        captcha_visible = await is_recaptcha_visible(page)
        if captcha_visible:
            print("    - капча появилась после отправки фото, решаю через 2captcha...", flush=True)
            solver_service = os.getenv("CAPTCHA_SOLVER_SERVICE", "2captcha").strip().lower()
            solver_api_key = os.getenv("CAPTCHA_SOLVER_API_KEY", "").strip()
            token_injected = False
            
            if solver_api_key:
                try:
                    from cian_bot.captcha_solver import CaptchaSolver, extract_recaptcha_site_key, inject_recaptcha_token
                    solver = CaptchaSolver(service=solver_service, api_key=solver_api_key)
                    site_key = await extract_recaptcha_site_key(page)
                    if site_key:
                        print(f"    - site key найден: {site_key[:30]}...", flush=True)
                        token = await solver.solve_recaptcha_v2(page.url, site_key, timeout_s=180)
                        if token:
                            print("    - токен получен от 2captcha, ввожу в страницу...", flush=True)
                            token_injected = await inject_recaptcha_token(page, token)
                            if token_injected:
                                print("    - токен введен успешно", flush=True)
                                await page.wait_for_timeout(2000)
                except Exception as e:
                    print(f"    - ошибка решения через 2captcha: {e}", flush=True)
            
            # ШАГ 4: После ввода токена кликаем на чекбокс (чтобы активировать форму)
            if token_injected:
                print("    - кликаю на чекбокс после ввода токена...", flush=True)
                clicked = await click_recaptcha_checkbox(page)
                if clicked:
                    await page.wait_for_timeout(3000)  # Ждем реакции
            else:
                # Если токен не введен - пробуем кликнуть на чекбокс вручную
                print("    - токен не введен, кликаю на чекбокс вручную...", flush=True)
                clicked = await click_recaptcha_checkbox(page)
                if clicked:
                    await page.wait_for_timeout(3000)
            
            # ШАГ 5: Проверяем появилась ли графическая капча после клика на чекбокс
            visual_challenge = await is_visual_captcha_challenge_visible(page)
            if visual_challenge:
                print("    - появилась графическая капча после клика на чекбокс, решаю через 2captcha...", flush=True)
                if solver_api_key:
                    try:
                        solver = CaptchaSolver(service=solver_service, api_key=solver_api_key)
                        site_key = await extract_recaptcha_site_key(page)
                        if site_key:
                            token = await solver.solve_recaptcha_v2(page.url, site_key, timeout_s=180)
                            if token:
                                await inject_recaptcha_token(page, token)
                                await page.wait_for_timeout(5000)
                                # Проверяем решена ли
                                visual_still = await is_visual_captcha_challenge_visible(page)
                                if visual_still:
                                    print("    - ⚠ графическая капча не решена автоматически, делаю паузу...", flush=True)
                                    await page.pause()
                    except Exception as e:
                        print(f"    - ошибка решения графической капчи: {e}", flush=True)
                        print("    - делаю паузу для ручного решения...", flush=True)
                        await page.pause()
            
            # ШАГ 6: Проверяем что капча решена и нажимаем отправить
            captcha_visible = await is_recaptcha_visible(page)
            visual_challenge = await is_visual_captcha_challenge_visible(page)
            
            if not captcha_visible and not visual_challenge:
                print("    - капча решена, нажимаю отправить...", flush=True)
                if send_btn:
                    await send_btn.click(timeout=2500)
                else:
                    await click_first_visible(
                        [
                            context.get_by_test_id("send_button"),
                            context.get_by_role("button", name=re.compile(r"отправить", re.I)),
                            context.locator("text=Отправить"),
                        ],
                        timeout_ms=3000,
                    )
                await page.wait_for_timeout(3000)
            elif captcha_visible or visual_challenge:
                print("    - ⚠ капча все еще видна, делаю паузу для ручного решения...", flush=True)
                await page.pause()
                # После паузы пробуем отправить еще раз
                if send_btn:
                    await send_btn.click(timeout=2500)
                else:
                    await click_first_visible(
                        [
                            context.get_by_test_id("send_button"),
                            context.get_by_role("button", name=re.compile(r"отправить", re.I)),
                            context.locator("text=Отправить"),
                        ],
                        timeout_ms=3000,
                    )
                await page.wait_for_timeout(3000)
        
        # Проверяем что фото действительно отправлено (капчи нет)
        for check_attempt in range(10):  # До 5 секунд
            captcha_visible = await is_recaptcha_visible(page)
            visual_challenge = await is_visual_captcha_challenge_visible(page)
            
            if not captcha_visible and not visual_challenge:
                print("    - ✓ Фото отправлено (капчи нет)", flush=True)
                await page.wait_for_timeout(2000)
                return True
            
            await page.wait_for_timeout(500)
        
        # Финальная проверка
        captcha_visible = await is_recaptcha_visible(page)
        visual_challenge = await is_visual_captcha_challenge_visible(page)
        if captcha_visible or visual_challenge:
            print("    - ⚠ Капча все еще видна после отправки фото", flush=True)
            return False
        
        await page.wait_for_timeout(2000)
        return True

    # Prefer ChatModal iframe if present; else operate on the main page.
    chat_ctx = None
    try:
        # Проверяем наличие iframe
        if await page.locator('[data-testid="ChatModal"]').count() > 0:
            chat_ctx = page.frame_locator('[data-testid="ChatModal"]')
    except Exception:
        chat_ctx = None

    if chat_ctx is not None:
        print("  - trying attachment inside ChatModal iframe...", flush=True)
        ok = await _try_attach_using_user_locators(chat_ctx)
        if ok:
            success = await _try_send_after_attach(chat_ctx)
            if success:
                return
        else:
            print("  - iframe attachment failed, trying fallback...", flush=True)

    print("  - trying attachment on main page...", flush=True)
    ok = await _try_attach_using_user_locators(page)
    if ok:
        success = await _try_send_after_attach(page)
        if success:
            return

    attach_clicked = False
    file_chosen = False

    # Prefer file chooser flow (common when input[type=file] hidden).
    attach_button_candidates = [
        page.get_by_role("button", name=re.compile(r"прикреп", re.I)),
        page.locator('[aria-label*="Прикреп"]'),
        page.locator('[aria-label*="прикреп"]'),
        page.locator('button:has-text("Скрепка")'),
    ]

    try:
        async with page.expect_file_chooser(timeout=5000) as fc_info:
            attach_clicked = await click_first_visible(attach_button_candidates, timeout_ms=1500)
        if attach_clicked:
            fc = await fc_info.value
            await fc.set_files(str(image_path))
            file_chosen = True
    except Exception:
        # fallback to direct input[type=file]
        pass

    if not file_chosen:
        file_input = page.locator('input[type="file"]').first
        try:
            await file_input.set_input_files(str(image_path))
            file_chosen = True
        except Exception:
            await safe_screenshot(page, "cannot_attach_file")
            raise RuntimeError("Не удалось прикрепить файл (не найден file chooser / input[type=file]).")

    # Wait a bit for upload/preview
    await page.wait_for_timeout(1500)

    print("  - sending attachment...", flush=True)
    sent = await click_first_visible(
        [
            page.get_by_role("button", name=re.compile(r"отправить", re.I)),
            page.locator("text=Отправить"),
        ],
        timeout_ms=4000,
    )
    if not sent:
        await safe_screenshot(page, "cannot_send_after_attach")
        raise RuntimeError("Файл прикрепился, но не удалось нажать 'Отправить' для отправки вложения.")

    await page.wait_for_timeout(1200)


async def main() -> None:
    s = load_settings()
    await _ensure_storage_state_exists(s.storage_state_path)

    downloads_dir = Path("downloads")
    print("Step 1/4: downloading image from Google Drive...", flush=True)
    image_path = download_public_file(s.google_drive_file_id, downloads_dir)
    # Windows PowerShell console encoding can crash on some unicode filenames.
    safe_path = str(image_path).encode("ascii", "backslashreplace").decode("ascii")
    print(f"Downloaded: {safe_path}", flush=True)

    async with async_playwright() as p:
        print("Step 2/4: launching browser...", flush=True)
        browser = await p.chromium.launch(headless=s.headless, slow_mo=s.slow_mo_ms or 0)
        context = await browser.new_context(storage_state=str(s.storage_state_path))
        page = await context.new_page()
        store = StateStore(s.state_db_path)

        try:
            print("Step 3/4: opening listing and sending message...", flush=True)
            dialog_id = await _open_listing_and_send_message(page, s.cian_ad_url, s.cian_message)
            print(f"Message sent. dialog_id={dialog_id}", flush=True)
            try:
                offer_id = _extract_ad_id(s.cian_ad_url)
                if offer_id and offer_id.isdigit():
                    store.set_status(int(offer_id), "messaged")
            except Exception:
                pass
            print("Step 4/4: opening dialogs and attaching image...", flush=True)
            await _open_dialog_from_list(page, s.dialogs_url, dialog_id, s.cian_message, s.cian_ad_url)
            await _attach_image_and_send(page, image_path)
            print("Attachment sent.", flush=True)
            try:
                offer_id = _extract_ad_id(s.cian_ad_url)
                if offer_id and offer_id.isdigit():
                    store.set_status(int(offer_id), "photo_sent")
            except Exception:
                pass
        except Exception:
            await safe_screenshot(page, "run_failed")
            try:
                offer_id = _extract_ad_id(s.cian_ad_url)
                if offer_id and offer_id.isdigit():
                    store.set_status(int(offer_id), "error", last_error="run_failed")
            except Exception:
                pass
            raise
        finally:
            try:
                store.close()
            except Exception:
                pass
            await context.close()
            await browser.close()

    print("Готово: сообщение отправлено и картинка прикреплена.")


if __name__ == "__main__":
    asyncio.run(main())

