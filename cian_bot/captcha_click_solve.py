"""
Новая логика обработки капчи:
1. Кликаем на checkbox когда капча появляется
2. Если появляется визуальная капча - используем 2captcha для автоматического решения
3. Если нет визуальной капчи - капча решена, можно отправлять
"""
from __future__ import annotations

import os
import re
from playwright.async_api import Page

from dotenv import load_dotenv

load_dotenv(override=False)

from cian_bot.captcha import is_recaptcha_visible, is_visual_captcha_challenge_visible

# Import captcha solver if available
try:
    from cian_bot.captcha_solver import CaptchaSolver, extract_recaptcha_site_key, inject_recaptcha_token
    CAPTCHA_SOLVER_AVAILABLE = True
except Exception:
    CAPTCHA_SOLVER_AVAILABLE = False


async def click_recaptcha_checkbox(page: Page) -> bool:
    """
    Кликает на checkbox капчи в ChatModal iframe.
    Возвращает True если клик выполнен успешно.
    """
    try:
        checkbox_selectors = [
            "#recaptcha-anchor",
            ".recaptcha-checkbox-border",
            ".recaptcha-checkbox[role=\"checkbox\"]",
            ".recaptcha-checkbox",
        ]

        contexts = []
        try:
            if await page.locator('[data-testid="ChatModal"]').count() > 0:
                contexts.append(page.frame_locator('[data-testid="ChatModal"]'))
        except Exception:
            pass
        contexts.append(page)

        for ctx in contexts:
            recaptcha_frame = ctx.frame_locator('iframe[src*="recaptcha"]')
            for selector in checkbox_selectors:
                try:
                    checkbox = recaptcha_frame.locator(selector).first
                    if await checkbox.count() > 0:
                        print(
                            f"[CAPTCHA_CLICK] Найден checkbox через '{selector}', кликаю...",
                            flush=True,
                        )
                        await checkbox.scroll_into_view_if_needed()
                        await checkbox.click(timeout=3000)
                        print(f"[CAPTCHA_CLICK] ✓ Checkbox кликнут через '{selector}'!", flush=True)
                        await page.wait_for_timeout(2000)
                        return True
                except Exception as e:
                    continue

        print("[CAPTCHA_CLICK] ✗ Не удалось кликнуть на checkbox", flush=True)
        return False
    except Exception as e:
        print(f"[CAPTCHA_CLICK] Общая ошибка: {e}", flush=True)
        return False


async def solve_captcha_with_click_and_visual_check(page: Page, timeout_s: int = 300) -> bool:
    """
    Полная логика решения капчи:
    1. Ждем появления капчи
    2. Пытаемся решить через 2captcha (автоматически решает и checkbox и визуальную капчу)
    3. Если 2captcha не доступна - кликаем на checkbox вручную
    4. Если появляется визуальная капча - используем 2captcha для решения
    5. Возвращает True если капча решена
    """
    import asyncio
    
    # Шаг 1: Ждем появления капчи
    print("[CAPTCHA_SOLVE] Ожидаю появления капчи...", flush=True)
    for attempt in range(30):  # До 15 секунд
        if await is_recaptcha_visible(page):
            print("[CAPTCHA_SOLVE] ✓ Капча появилась!", flush=True)
            break
        await page.wait_for_timeout(500)
        if attempt == 29:
            print("[CAPTCHA_SOLVE] Капча не появилась", flush=True)
            return True  # Нет капчи = решена
    
    # Шаг 2: Пытаемся решить через 2captcha (автоматически решает все типы капчи)
    if CAPTCHA_SOLVER_AVAILABLE:
        solver_service = os.getenv("CAPTCHA_SOLVER_SERVICE", "2captcha").strip().lower()
        solver_api_key = os.getenv("CAPTCHA_SOLVER_API_KEY", "").strip()
        
        if solver_api_key:
            print(f"[CAPTCHA_SOLVE] Использую {solver_service} для автоматического решения капчи...", flush=True)
            try:
                solver = CaptchaSolver(service=solver_service, api_key=solver_api_key)
                page_url = page.url
                site_key = await extract_recaptcha_site_key(page)
                
                if site_key:
                    print(f"[CAPTCHA_SOLVE] Site key: {site_key[:30]}...", flush=True)
                    # 2captcha автоматически решает и checkbox и визуальную капчу
                    token = await solver.solve_recaptcha_v2(page_url, site_key, timeout_s=180)
                    
                    if token:
                        print(f"[CAPTCHA_SOLVE] Токен получен от {solver_service}, ввожу в страницу...", flush=True)
                        injection_success = await inject_recaptcha_token(page, token)
                        
                        if injection_success:
                            # Ждем обработки токена
                            print("[CAPTCHA_SOLVE] Ожидаю обработку токена...", flush=True)
                            for wait_attempt in range(30):  # До 15 секунд
                                await page.wait_for_timeout(500)
                                captcha_visible = await is_recaptcha_visible(page)
                                visual_challenge = await is_visual_captcha_challenge_visible(page)
                                
                                if not captcha_visible and not visual_challenge:
                                    print(f"[CAPTCHA_SOLVE] ✓ Капча решена автоматически через {solver_service}!", flush=True)
                                    return True
                                
                                if wait_attempt % 4 == 0:  # Логируем каждые 2 секунды
                                    print(f"[CAPTCHA_SOLVE] Ожидание... ({wait_attempt * 0.5:.1f}s) captcha={captcha_visible}, visual={visual_challenge}", flush=True)
                            
                            # Финальная проверка
                            captcha_visible = await is_recaptcha_visible(page)
                            visual_challenge = await is_visual_captcha_challenge_visible(page)
                            if not captcha_visible and not visual_challenge:
                                print(f"[CAPTCHA_SOLVE] ✓ Капча решена через {solver_service}!", flush=True)
                                return True
                        else:
                            print("[CAPTCHA_SOLVE] ✗ Не удалось ввести токен", flush=True)
                    else:
                        print("[CAPTCHA_SOLVE] ✗ Не удалось получить токен от сервиса", flush=True)
                else:
                    print("[CAPTCHA_SOLVE] ✗ Не удалось извлечь site key", flush=True)
            except Exception as e:
                print(f"[CAPTCHA_SOLVE] Ошибка автоматического решения: {e}", flush=True)
                import traceback
                print(f"[CAPTCHA_SOLVE] Traceback: {traceback.format_exc()}", flush=True)
    
    # Шаг 3: Fallback - кликаем на checkbox вручную
    print("[CAPTCHA_SOLVE] Кликаю на checkbox капчи вручную...", flush=True)
    clicked = await click_recaptcha_checkbox(page)
    await page.wait_for_timeout(3000)  # Даем время на появление визуальной капчи
    
    # Шаг 4: Проверяем визуальную капчу
    visual_challenge = await is_visual_captcha_challenge_visible(page)
    if visual_challenge:
        print("[CAPTCHA_SOLVE] ⚠ Появилась визуальная капча!", flush=True)
        # Пытаемся решить через 2captcha еще раз (для визуальной капчи)
        if CAPTCHA_SOLVER_AVAILABLE and solver_api_key:
            print("[CAPTCHA_SOLVE] Решаю визуальную капчу через 2captcha...", flush=True)
            try:
                solver = CaptchaSolver(service=solver_service, api_key=solver_api_key)
                site_key = await extract_recaptcha_site_key(page)
                if site_key:
                    token = await solver.solve_recaptcha_v2(page.url, site_key, timeout_s=180)
                    if token:
                        await inject_recaptcha_token(page, token)
                        await page.wait_for_timeout(5000)
            except Exception as e:
                print(f"[CAPTCHA_SOLVE] Ошибка решения визуальной капчи: {e}", flush=True)
        
        # Если автоматическое решение не сработало - пауза для ручного решения
        visual_still = await is_visual_captcha_challenge_visible(page)
        if visual_still:
            print("[CAPTCHA_SOLVE] ⚠ Визуальная капча не решена автоматически, делаю паузу...", flush=True)
            print("[CAPTCHA_SOLVE] Решите визуальную капчу вручную и нажмите Resume.", flush=True)
            await page.pause()
    
    # Финальная проверка
    await page.wait_for_timeout(2000)
    captcha_visible = await is_recaptcha_visible(page)
    visual_challenge = await is_visual_captcha_challenge_visible(page)
    
    if not captcha_visible and not visual_challenge:
        print("[CAPTCHA_SOLVE] ✓ Капча решена!", flush=True)
        return True
    else:
        print("[CAPTCHA_SOLVE] ⚠ Капча все еще видна", flush=True)
        return False
