from __future__ import annotations

import os
import re
from typing import Optional

from dotenv import load_dotenv
from playwright.async_api import Page

# Load .env file to ensure CAPTCHA_SOLVER_* vars are available
load_dotenv(override=False)

# Optional: import captcha solver if API key is set
try:
    from cian_bot.captcha_solver import CaptchaSolver, extract_recaptcha_site_key, inject_recaptcha_token
    CAPTCHA_SOLVER_AVAILABLE = True
except Exception:
    CAPTCHA_SOLVER_AVAILABLE = False


async def is_recaptcha_visible(page: Page) -> bool:
    """
    Best-effort detection of reCAPTCHA widget/challenge on the page.
    """
    # Common iframes/containers for Google reCAPTCHA
    selectors = (
        'iframe[src*="google.com/recaptcha"]',
        'iframe[src*="recaptcha"]',
        "div.g-recaptcha",
        "[data-sitekey]",
    )

    # Cian-specific: captcha inside ChatModal iframe (user-provided locator).
    try:
        cian_captcha = page.frame_locator('[data-testid="ChatModal"]').locator(
            ".x61f99309--_919db--captcha > div"
        )
        if await cian_captcha.first.is_visible():
            return True
    except Exception:
        pass

    # Important: reCAPTCHA may appear inside an iframe (e.g., Cian ChatModal).
    frames = page.frames
    for frame in frames:
        for sel in selectors:
            try:
                if await frame.locator(sel).first.is_visible():
                    return True
            except Exception:
                pass

        # Sometimes the page shows "Я не робот" text
        try:
            if await frame.get_by_text(re.compile(r"я не робот", re.I)).first.is_visible():
                return True
        except Exception:
            pass


async def is_visual_captcha_challenge_visible(page: Page) -> bool:
    """
    Detect if visual CAPTCHA challenge (image selection) is visible.
    This happens when reCAPTCHA v2 shows image grid after checkbox click.
    """
    # Check for visual challenge indicators in all frames
    frames = page.frames
    for frame in frames:
        try:
            # Check for image grid or challenge text
            # Common indicators: "Выберите все квадраты", "Select all", image grid
            challenge_indicators = [
                frame.get_by_text(re.compile(r"выберите.*квадрат", re.I)),
                frame.get_by_text(re.compile(r"select all", re.I)),
                frame.locator('div[role="dialog"]'),  # Challenge dialog
                frame.locator('.rc-imageselect-challenge'),  # reCAPTCHA challenge container
            ]
            
            for indicator in challenge_indicators:
                try:
                    if await indicator.first.is_visible(timeout=500):
                        return True
                except Exception:
                    pass
            
            # Check ChatModal iframe specifically
            try:
                chat_modal_frame = page.frame_locator('[data-testid="ChatModal"]')
                # Look for challenge dialog or image grid
                challenge_in_chat = chat_modal_frame.locator('div[role="dialog"]')
                if await challenge_in_chat.first.is_visible(timeout=500):
                    return True
            except Exception:
                pass
        except Exception:
            pass
    
    return False

    return False


async def wait_for_recaptcha_to_be_solved(
    page: Page,
    *,
    appear_timeout_s: int = 0,
    timeout_s: int = 10 * 60,
    poll_ms: int = 1000,
    note: Optional[str] = None,
) -> bool:
    """
    If reCAPTCHA becomes visible (optionally within appear_timeout_s), wait until it disappears.
    Supports both manual solving (CAPTCHA_PAUSE=true) and automatic solving via external service.
    """
    captcha_seen = False
    if appear_timeout_s > 0:
        # Wait a short window for captcha to appear (it may pop after a click/navigation).
        waited_appear = 0.0
        while waited_appear < appear_timeout_s:
            if await is_recaptcha_visible(page):
                captcha_seen = True
                break
            await page.wait_for_timeout(min(poll_ms, 250))
            waited_appear += min(poll_ms, 250) / 1000
        else:
            return False
    else:
        if not await is_recaptcha_visible(page):
            return False
        captcha_seen = True

    print("\n[CAPTCHA] Обнаружена reCAPTCHA.", flush=True)
    if note:
        print(f"[CAPTCHA] {note}", flush=True)

    # Check if automatic solving is enabled
    solver_service = os.getenv("CAPTCHA_SOLVER_SERVICE", "").strip().lower()
    solver_api_key = os.getenv("CAPTCHA_SOLVER_API_KEY", "").strip()
    auto_solve = solver_service in {"2captcha", "capsolver"} and bool(solver_api_key)

    if auto_solve and CAPTCHA_SOLVER_AVAILABLE:
        print(f"[CAPTCHA] Автоматическое решение через {solver_service}...", flush=True)
        try:
            solver = CaptchaSolver(service=solver_service, api_key=solver_api_key)
            page_url = page.url
            print(f"[CAPTCHA] Текущий URL: {page_url}", flush=True)
            print(f"[CAPTCHA] Количество фреймов: {len(page.frames)}", flush=True)
            
            site_key = await extract_recaptcha_site_key(page)
            if not site_key:
                print("[CAPTCHA] Не удалось извлечь site key", flush=True)
                print("[CAPTCHA] Попробую найти капчу вручную для отладки...", flush=True)
                # Debug: try to find captcha elements
                try:
                    captcha_elements = await page.evaluate(
                        """
                        () => {
                            const results = [];
                            // Check main page
                            const els = document.querySelectorAll('[data-sitekey], .g-recaptcha, iframe[src*="recaptcha"]');
                            for (const el of els) {
                                results.push({
                                    tag: el.tagName,
                                    sitekey: el.getAttribute('data-sitekey'),
                                    src: el.getAttribute('src'),
                                    className: el.className
                                });
                            }
                            return results;
                        }
                        """
                    )
                    print(f"[CAPTCHA] Debug: найдено элементов на главной странице: {len(captcha_elements)}", flush=True)
                    for el in captcha_elements[:3]:  # Show first 3
                        print(f"  - {el}", flush=True)
                except Exception as e:
                    print(f"[CAPTCHA] Debug error: {e}", flush=True)
                print("[CAPTCHA] Переключаюсь на ручное решение", flush=True)
            else:
                print(f"[CAPTCHA] Site key извлечен: {site_key[:30]}...", flush=True)
                token = await solver.solve_recaptcha_v2(page_url, site_key, timeout_s=180)  # Increased for visual challenges
                if token:
                    print(f"[CAPTCHA] Токен получен (длина: {len(token)}), ввожу в страницу...", flush=True)
                    injection_success = await inject_recaptcha_token(page, token)
                    print(f"[CAPTCHA] Результат инъекции токена: {injection_success}", flush=True)
                    
                    if injection_success:
                        # CRITICAL: Check if visual challenge appeared after token injection
                        await page.wait_for_timeout(2000)  # Wait for potential challenge to appear
                        visual_challenge = await is_visual_captcha_challenge_visible(page)
                        if visual_challenge:
                            print("[CAPTCHA] ⚠ Появилась визуальная капча (image challenge)!", flush=True)
                            print("[CAPTCHA] 2captcha может решить её автоматически, но это займет больше времени...", flush=True)
                            print("[CAPTCHA] Ожидаю решение визуальной капчи (до 3 минут)...", flush=True)
                            
                            # Wait longer for visual challenge solution
                            # 2captcha workers need to solve images, which takes longer
                            for wait_attempt in range(36):  # Wait up to 3 minutes (36 * 5s)
                                await page.wait_for_timeout(5000)
                                visual_still_visible = await is_visual_captcha_challenge_visible(page)
                                captcha_visible = await is_recaptcha_visible(page)
                                
                                if not visual_still_visible and not captcha_visible:
                                    print(f"[CAPTCHA] ✓ Визуальная капча решена через {wait_attempt * 5} секунд!", flush=True)
                                    break
                                elif wait_attempt % 6 == 0:  # Log every 30 seconds
                                    print(f"[CAPTCHA] Ожидание решения визуальной капчи... ({wait_attempt * 5}s)", flush=True)
                            
                            # Check final state
                            visual_still_visible = await is_visual_captcha_challenge_visible(page)
                            captcha_visible = await is_recaptcha_visible(page)
                            
                            if visual_still_visible or captcha_visible:
                                print("[CAPTCHA] ⚠ Визуальная капча не решена автоматически.", flush=True)
                                print("[CAPTCHA] Требуется ручное решение. Делаю паузу...", flush=True)
                                print("[CAPTCHA] Решите визуальную капчу вручную и нажмите Resume в Playwright Inspector.", flush=True)
                                await page.pause()
                                # After manual solve, continue
                                print("[CAPTCHA] Продолжаю после ручного решения...", flush=True)
                                await page.wait_for_timeout(2000)
                        
                        # CRITICAL: Check captcha visibility in ChatModal iframe specifically
                        print("[CAPTCHA] Проверяю видимость капчи в ChatModal iframe...", flush=True)
                        captcha_in_chatmodal = False
                        try:
                            chat_modal_frame = page.frame_locator('[data-testid="ChatModal"]')
                            captcha_locator = chat_modal_frame.locator(".x61f99309--_919db--captcha > div")
                            captcha_in_chatmodal = await captcha_locator.first.is_visible(timeout=1000)
                            print(f"[CAPTCHA] Капча в ChatModal iframe видна: {captcha_in_chatmodal}", flush=True)
                        except Exception as e:
                            print(f"[CAPTCHA] Ошибка проверки капчи в ChatModal: {e}", flush=True)
                        
                        # Wait longer for the page to process the token and verify it's accepted
                        print("[CAPTCHA] Ожидаю обработку токена страницей (до 15 секунд)...", flush=True)
                        captcha_disappeared = False
                        for wait_attempt in range(30):  # Wait up to 15 seconds (30 * 500ms)
                            await page.wait_for_timeout(500)
                            
                            # Check both main page and ChatModal iframe
                            main_visible = await is_recaptcha_visible(page)
                            chatmodal_visible = False
                            try:
                                chat_modal_frame = page.frame_locator('[data-testid="ChatModal"]')
                                captcha_locator = chat_modal_frame.locator(".x61f99309--_919db--captcha > div")
                                chatmodal_visible = await captcha_locator.first.is_visible(timeout=500)
                            except Exception:
                                pass
                            
                            if not main_visible and not chatmodal_visible:
                                print(f"[CAPTCHA] ✓ Капча исчезла через {wait_attempt * 0.5:.1f} секунд! (main={main_visible}, chatmodal={chatmodal_visible})", flush=True)
                                captcha_disappeared = True
                                break
                            elif wait_attempt % 4 == 0:  # Log every 2 seconds
                                print(f"[CAPTCHA] Ожидание... ({wait_attempt * 0.5:.1f}s) main={main_visible}, chatmodal={chatmodal_visible}", flush=True)
                        
                        # CRITICAL: Check if token is actually set in grecaptcha, even if captcha is still visible
                        token_actually_set = False
                        try:
                            chat_modal_frame_obj = None
                            frames = page.frames
                            for i, frame in enumerate(frames):
                                try:
                                    captcha_locator = frame.locator(".x61f99309--_919db--captcha > div")
                                    if await captcha_locator.count() > 0:
                                        chat_modal_frame_obj = frame
                                        break
                                except Exception:
                                    pass
                            
                            if chat_modal_frame_obj:
                                token_check = await chat_modal_frame_obj.evaluate(
                                    """
                                    () => {
                                        if (window.grecaptcha && window.grecaptcha.getResponse) {
                                            const response = window.grecaptcha.getResponse();
                                            return response && response.length > 100; // Valid token is long
                                        }
                                        return false;
                                    }
                                    """
                                )
                                if token_check:
                                    print("[CAPTCHA] ✓ Токен установлен в grecaptcha (даже если капча видна)", flush=True)
                                    token_actually_set = True
                        except Exception as e:
                            print(f"[CAPTCHA] Ошибка проверки токена: {e}", flush=True)
                        
                        if captcha_disappeared or token_actually_set:
                            # Wait a bit more to ensure form is ready
                            await page.wait_for_timeout(1500)
                            if token_actually_set and not captcha_disappeared:
                                print("[CAPTCHA] ✓ Токен установлен, капча решена (визуально может оставаться видимой). Форма готова к отправке.", flush=True)
                            else:
                                print("[CAPTCHA] ✓ Капча решена автоматически! Форма готова к отправке.", flush=True)
                            return True
                        else:
                            # Captcha still visible - try to trigger form submission manually in ChatModal
                            print("[CAPTCHA] ✗ Токен введен, но капча все еще видна.", flush=True)
                            print("[CAPTCHA] Пытаюсь активировать форму через JavaScript в ChatModal iframe...", flush=True)
                            try:
                                # Find the actual Frame object for ChatModal iframe
                                chat_modal_frame_obj = None
                                frames = page.frames
                                for i, frame in enumerate(frames):
                                    try:
                                        captcha_locator = frame.locator(".x61f99309--_919db--captcha > div")
                                        if await captcha_locator.count() > 0:
                                            print(f"[CAPTCHA] Найден ChatModal frame: {i}", flush=True)
                                            chat_modal_frame_obj = frame
                                            break
                                    except Exception:
                                        pass
                                
                                if chat_modal_frame_obj:
                                    await chat_modal_frame_obj.evaluate(
                                        """
                                        (token) => {
                                            console.log('[CAPTCHA_ACTIVATE] Starting form activation in ChatModal...');
                                            
                                            // Try to find and trigger form submission
                                            const forms = document.querySelectorAll('form');
                                            console.log('[CAPTCHA_ACTIVATE] Found forms:', forms.length);
                                            for (const form of forms) {
                                                const recaptchaInput = form.querySelector('[name="g-recaptcha-response"]');
                                                if (recaptchaInput && recaptchaInput.value) {
                                                    console.log('[CAPTCHA_ACTIVATE] Found recaptcha input with value length:', recaptchaInput.value.length);
                                                    // Trigger form validation
                                                    form.dispatchEvent(new Event('submit', { bubbles: true, cancelable: true }));
                                                    recaptchaInput.dispatchEvent(new Event('input', { bubbles: true }));
                                                    recaptchaInput.dispatchEvent(new Event('change', { bubbles: true }));
                                                }
                                            }
                                            
                                            // Try to trigger grecaptcha callback in ChatModal
                                            if (window.grecaptcha && window.___grecaptcha_cfg) {
                                                console.log('[CAPTCHA_ACTIVATE] grecaptcha found, trying callbacks...');
                                                try {
                                                    const clients = window.___grecaptcha_cfg.clients;
                                                    for (const clientId in clients) {
                                                        const client = clients[clientId];
                                                        if (client && client.widgets) {
                                                            for (const widgetId in client.widgets) {
                                                                const widget = client.widgets[widgetId];
                                                                if (widget && widget.callback) {
                                                                    const textarea = document.querySelector('textarea[name="g-recaptcha-response"]');
                                                                    if (textarea && textarea.value) {
                                                                        console.log('[CAPTCHA_ACTIVATE] Calling widget callback for widget', widgetId);
                                                                        widget.callback(textarea.value);
                                                                        console.log('[CAPTCHA_ACTIVATE] Callback triggered in ChatModal');
                                                                    }
                                                                }
                                                            }
                                                        }
                                                    }
                                                } catch(e) {
                                                    console.log('[CAPTCHA_ACTIVATE] Error triggering callback in ChatModal:', e);
                                                }
                                            } else {
                                                console.log('[CAPTCHA_ACTIVATE] No grecaptcha found');
                                            }
                                            
                                            // Also try to find submit button and click it
                                            const submitButtons = document.querySelectorAll('button[type="submit"], button[data-testid*="send"], button[aria-label*="отправить"]');
                                            console.log('[CAPTCHA_ACTIVATE] Found submit buttons:', submitButtons.length);
                                            for (const btn of submitButtons) {
                                                try {
                                                    btn.click();
                                                    console.log('[CAPTCHA_ACTIVATE] Clicked submit button');
                                                } catch(e) {
                                                    console.log('[CAPTCHA_ACTIVATE] Error clicking button:', e);
                                                }
                                            }
                                        }
                                        """,
                                        token
                                    )
                                else:
                                    print("[CAPTCHA] ChatModal frame не найден для активации формы", flush=True)
                                # Wait a bit more after triggering
                                await page.wait_for_timeout(3000)
                                
                                # Check again
                                main_visible = await is_recaptcha_visible(page)
                                chatmodal_visible = False
                                try:
                                    chat_modal_frame = page.frame_locator('[data-testid="ChatModal"]')
                                    captcha_locator = chat_modal_frame.locator(".x61f99309--_919db--captcha > div")
                                    chatmodal_visible = await captcha_locator.first.is_visible(timeout=1000)
                                except Exception:
                                    pass
                                
                                if not main_visible and not chatmodal_visible:
                                    print("[CAPTCHA] ✓ Капча исчезла после активации формы в ChatModal!", flush=True)
                                    return True
                                else:
                                    print(f"[CAPTCHA] ✗ Капча все еще видна после активации (main={main_visible}, chatmodal={chatmodal_visible})", flush=True)
                            except Exception as e:
                                print(f"[CAPTCHA] Ошибка при активации формы в ChatModal: {e}", flush=True)
                                import traceback
                                print(f"[CAPTCHA] Traceback: {traceback.format_exc()}", flush=True)
                            
                            print("[CAPTCHA] ⚠ Капча не исчезла автоматически. Возвращаю управление для повторной отправки.", flush=True)
                            # Return True to allow caller to retry the action (e.g. click Send again)
                            return True
                    else:
                        print("[CAPTCHA] ✗ Не удалось ввести токен в страницу", flush=True)
                else:
                    print("[CAPTCHA] Не удалось получить решение от сервиса", flush=True)
        except Exception as e:
            import traceback
            print(f"[CAPTCHA] Ошибка автоматического решения: {e}", flush=True)
            print(f"[CAPTCHA] Traceback: {traceback.format_exc()}", flush=True)
            print("[CAPTCHA] Переключаюсь на ручное решение...", flush=True)

    # Fallback to manual solving
    pause = os.getenv("CAPTCHA_PAUSE", "").strip().lower() in {"1", "true", "yes", "y", "on"}
    if pause:
        print(
            "[CAPTCHA] Включена пауза (CAPTCHA_PAUSE=true).\n"
            "[CAPTCHA] Сейчас откроется Playwright Inspector.\n"
            "[CAPTCHA] Поставьте галочку 'Я не робот' в браузере, затем нажмите Resume.\n",
            flush=True,
        )
        await page.pause()
        # Important: on some sites (incl. Cian chat) the captcha container can remain visible
        # even after successful verification, until the user re-submits the action.
        # In CAPTCHA_PAUSE mode we return immediately after Resume so the caller can retry (e.g. click Send again).
        return True
    else:
        print("[CAPTCHA] Решите капчу в открытом браузере — я подожду и продолжу.\n", flush=True)

    # Wait until recaptcha is no longer visible
    waited = 0
    while waited < timeout_s:
        if not await is_recaptcha_visible(page):
            print("[CAPTCHA] Капча исчезла, продолжаю...\n", flush=True)
            return True
        await page.wait_for_timeout(poll_ms)
        waited += poll_ms / 1000

    raise TimeoutError(f"reCAPTCHA не решена за {timeout_s} секунд")


