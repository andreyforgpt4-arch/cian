from __future__ import annotations

import asyncio
import os
import time
from typing import Optional

import aiohttp


class CaptchaSolver:
    """
    Automatic reCAPTCHA solving via external service (2captcha or CapSolver).
    """

    request_timeout_s = 30

    def __init__(self, service: str = "2captcha", api_key: Optional[str] = None):
        """
        Args:
            service: "2captcha" or "capsolver"
            api_key: API key from the service
        """
        self.service = service.lower()
        self.api_key = api_key or os.getenv("CAPTCHA_SOLVER_API_KEY", "").strip()
        if not self.api_key:
            raise RuntimeError(
                f"Missing CAPTCHA_SOLVER_API_KEY env var. "
                f"Get API key from https://2captcha.com/ or https://capsolver.com/"
            )

        if self.service == "2captcha":
            self.submit_url = "http://2captcha.com/in.php"
            self.result_url = "http://2captcha.com/res.php"
        elif self.service == "capsolver":
            self.submit_url = "https://api.capsolver.com/createTask"
            self.result_url = "https://api.capsolver.com/getTaskResult"
        else:
            raise ValueError(f"Unknown service: {service}. Use '2captcha' or 'capsolver'")

    async def solve_recaptcha_v2(
        self, page_url: str, site_key: str, timeout_s: int = 180
    ) -> Optional[str]:
        """
        Solve reCAPTCHA v2 and return the solution token.
        Handles both checkbox-only and visual challenge (image selection) types.

        Args:
            page_url: URL where captcha appears (e.g., "https://novosibirsk.cian.ru/...")
            site_key: reCAPTCHA site key (extracted from page)
            timeout_s: Max wait time for solution (increased to 180s for visual challenges)

        Returns:
            Solution token (g-recaptcha-response) or None if failed
        """
        if self.service == "2captcha":
            return await self._solve_2captcha(page_url, site_key, timeout_s)
        elif self.service == "capsolver":
            return await self._solve_capsolver(page_url, site_key, timeout_s)
        return None

    async def _solve_2captcha(
        self, page_url: str, site_key: str, timeout_s: int
    ) -> Optional[str]:
        """Solve via 2captcha.com API."""
        # Submit task
        submit_data = {
            "key": self.api_key,
            "method": "userrecaptcha",
            "googlekey": site_key,
            "pageurl": page_url,
            "json": 1,
        }
        try:
            print(f"[CAPTCHA_SOLVER] Sending request to 2captcha with API key: {self.api_key[:10]}...", flush=True)
            result = await self._request_json("POST", self.submit_url, data=submit_data)
            if not result:
                return None
            if result.get("status") != 1:
                print(f"[CAPTCHA_SOLVER] 2captcha submit failed: {result}", flush=True)
                # Check if it's an API key error
                if result.get("request") == "ERROR_KEY_DOES_NOT_EXIST":
                    print(f"[CAPTCHA_SOLVER] ERROR: API key is invalid or missing. Check CAPTCHA_SOLVER_API_KEY in .env", flush=True)
                return None
            task_id = result.get("request")
            print(f"[CAPTCHA_SOLVER] Task submitted successfully, task_id={task_id}", flush=True)
        except Exception as e:
            print(f"[CAPTCHA_SOLVER] 2captcha submit error: {e}", flush=True)
            return None

        print(f"[CAPTCHA_SOLVER] Task submitted, task_id={task_id}, waiting...", flush=True)

        # Poll for result
        deadline = time.time() + timeout_s
        while time.time() < deadline:
            await asyncio.sleep(5)  # Wait 5 seconds between checks
            try:
                result = await self._request_json(
                    "GET",
                    self.result_url,
                    params={"key": self.api_key, "action": "get", "id": task_id, "json": 1},
                )
                if not result:
                    continue
                if result.get("status") == 1:
                    token = result.get("request")
                    print(f"[CAPTCHA_SOLVER] Solution received!", flush=True)
                    return token
                elif result.get("request") == "CAPCHA_NOT_READY":
                    continue  # Still processing
                else:
                    print(f"[CAPTCHA_SOLVER] 2captcha error: {result}", flush=True)
                    return None
            except Exception as e:
                print(f"[CAPTCHA_SOLVER] Poll error: {e}", flush=True)
                continue

        print(f"[CAPTCHA_SOLVER] Timeout waiting for solution", flush=True)
        return None

    async def _solve_capsolver(
        self, page_url: str, site_key: str, timeout_s: int
    ) -> Optional[str]:
        """Solve via capsolver.com API."""
        # Submit task
        submit_data = {
            "clientKey": self.api_key,
            "task": {
                "type": "ReCaptchaV2TaskProxyLess",
                "websiteURL": page_url,
                "websiteKey": site_key,
            },
        }
        try:
            result = await self._request_json("POST", self.submit_url, json=submit_data)
            if not result:
                return None
            if result.get("errorId") != 0:
                print(f"[CAPTCHA_SOLVER] CapSolver submit failed: {result}", flush=True)
                return None
            task_id = result.get("taskId")
        except Exception as e:
            print(f"[CAPTCHA_SOLVER] CapSolver submit error: {e}", flush=True)
            return None

        print(f"[CAPTCHA_SOLVER] Task submitted, task_id={task_id}, waiting...", flush=True)

        # Poll for result
        deadline = time.time() + timeout_s
        while time.time() < deadline:
            await asyncio.sleep(2)  # CapSolver is usually faster
            try:
                result_data = {"clientKey": self.api_key, "taskId": task_id}
                result = await self._request_json("POST", self.result_url, json=result_data)
                if not result:
                    continue
                if result.get("status") == "ready":
                    token = result.get("solution", {}).get("gRecaptchaResponse")
                    if token:
                        print(f"[CAPTCHA_SOLVER] Solution received!", flush=True)
                        return token
                elif result.get("status") == "processing":
                    continue
                else:
                    print(f"[CAPTCHA_SOLVER] CapSolver error: {result}", flush=True)
                    return None
            except Exception as e:
                print(f"[CAPTCHA_SOLVER] Poll error: {e}", flush=True)
                continue

        print(f"[CAPTCHA_SOLVER] Timeout waiting for solution", flush=True)
        return None

    async def _request_json(self, method: str, url: str, **kwargs) -> Optional[dict]:
        timeout = aiohttp.ClientTimeout(total=self.request_timeout_s)
        try:
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.request(method, url, **kwargs) as response:
                    response.raise_for_status()
                    return await response.json(content_type=None)
        except asyncio.TimeoutError:
            print(
                f"[CAPTCHA_SOLVER] Request timeout after {self.request_timeout_s}s for {method} {url}",
                flush=True,
            )
        except aiohttp.ClientResponseError as e:
            print(
                f"[CAPTCHA_SOLVER] Request failed ({e.status}) for {method} {url}: {e.message}",
                flush=True,
            )
        except aiohttp.ClientError as e:
            print(f"[CAPTCHA_SOLVER] Request error for {method} {url}: {e}", flush=True)
        except Exception as e:
            print(f"[CAPTCHA_SOLVER] Unexpected request error for {method} {url}: {e}", flush=True)
        return None


async def extract_recaptcha_site_key(page) -> Optional[str]:
    """
    Extract reCAPTCHA site key from page.
    Works with both main page and iframes (including ChatModal).
    """
    import re

    # Method 1: JavaScript evaluation on main page (searches all frames recursively)
    try:
        site_key = await page.evaluate(
            """
            () => {
                // Helper to search in all frames recursively
                function searchInFrames(frame) {
                    try {
                        // Check data-sitekey attributes
                        const el = frame.document.querySelector('[data-sitekey]');
                        if (el) return el.getAttribute('data-sitekey');
                        
                        // Check div.g-recaptcha
                        const grecaptcha = frame.document.querySelector('.g-recaptcha, div[data-sitekey]');
                        if (grecaptcha) {
                            const key = grecaptcha.getAttribute('data-sitekey');
                            if (key) return key;
                        }
                        
                        // Check iframes for recaptcha URLs
                        const iframes = frame.document.querySelectorAll('iframe');
                        for (const iframe of iframes) {
                            const src = iframe.getAttribute('src') || '';
                            if (src.includes('recaptcha')) {
                                const match = src.match(/[?&]k=([^&]+)/);
                                if (match) return match[1];
                            }
                        }
                        
                        // Check window.grecaptcha
                        if (frame.window && frame.window.grecaptcha) {
                            try {
                                const widgets = frame.window.grecaptcha.getResponse || 
                                               (frame.window.grecaptcha.execute && frame.window.grecaptcha.execute());
                                // Try to get site key from grecaptcha object
                                if (frame.window.___grecaptcha_cfg) {
                                    const cfg = frame.window.___grecaptcha_cfg;
                                    if (cfg.sitekey) return cfg.sitekey;
                                    if (cfg.clients && cfg.clients[0] && cfg.clients[0].sitekey) {
                                        return cfg.clients[0].sitekey;
                                    }
                                }
                            } catch (e) {}
                        }
                        
                        // Recursively check child frames
                        for (let i = 0; i < frame.frames.length; i++) {
                            const result = searchInFrames(frame.frames[i]);
                            if (result) return result;
                        }
                    } catch (e) {
                        // Cross-origin or other error, skip
                    }
                    return null;
                }
                
                // Start search from main window
                return searchInFrames(window);
            }
            """
        )
        if site_key:
            print(f"[CAPTCHA_SOLVER] Site key found via JS (main): {site_key[:20]}...", flush=True)
            return site_key
    except Exception as e:
        print(f"[CAPTCHA_SOLVER] JS evaluation error: {e}", flush=True)

    # Method 2: Check ChatModal iframe specifically (Cian-specific)
    try:
        chat_modal_frame = page.frame_locator('[data-testid="ChatModal"]')
        site_key = await chat_modal_frame.evaluate(
            """
            () => {
                // Check data-sitekey
                const el = document.querySelector('[data-sitekey]');
                if (el) return el.getAttribute('data-sitekey');
                
                // Check .g-recaptcha
                const grecaptcha = document.querySelector('.g-recaptcha, div[data-sitekey]');
                if (grecaptcha) {
                    const key = grecaptcha.getAttribute('data-sitekey');
                    if (key) return key;
                }
                
                // Check iframes
                const iframes = document.querySelectorAll('iframe[src*="recaptcha"]');
                for (const iframe of iframes) {
                    const src = iframe.getAttribute('src') || '';
                    const match = src.match(/[?&]k=([^&]+)/);
                    if (match) return match[1];
                }
                
                // Check window.grecaptcha
                if (window.grecaptcha && window.___grecaptcha_cfg) {
                    const cfg = window.___grecaptcha_cfg;
                    if (cfg.sitekey) return cfg.sitekey;
                    if (cfg.clients && cfg.clients[0] && cfg.clients[0].sitekey) {
                        return cfg.clients[0].sitekey;
                    }
                }
                
                return null;
            }
            """
        )
        if site_key:
            print(f"[CAPTCHA_SOLVER] Site key found in ChatModal: {site_key[:20]}...", flush=True)
            return site_key
    except Exception as e:
        print(f"[CAPTCHA_SOLVER] ChatModal frame error: {e}", flush=True)

    # Method 3: Check all frames sequentially
    frames = page.frames
    for i, frame in enumerate(frames):
        try:
            frame_url = frame.url
            # Try to extract from iframe URL
            if "recaptcha" in frame_url.lower():
                match = re.search(r"[?&]k=([^&]+)", frame_url)
                if match:
                    site_key = match.group(1)
                    print(f"[CAPTCHA_SOLVER] Site key found in frame URL: {site_key[:20]}...", flush=True)
                    return site_key
            
            # Try JavaScript evaluation in this frame
            try:
                site_key = await frame.evaluate(
                    """
                    () => {
                        const el = document.querySelector('[data-sitekey]');
                        if (el) return el.getAttribute('data-sitekey');
                        
                        const iframes = document.querySelectorAll('iframe[src*="recaptcha"]');
                        for (const iframe of iframes) {
                            const src = iframe.getAttribute('src') || '';
                            const match = src.match(/[?&]k=([^&]+)/);
                            if (match) return match[1];
                        }
                        
                        if (window.___grecaptcha_cfg) {
                            const cfg = window.___grecaptcha_cfg;
                            if (cfg.sitekey) return cfg.sitekey;
                            if (cfg.clients && cfg.clients[0] && cfg.clients[0].sitekey) {
                                return cfg.clients[0].sitekey;
                            }
                        }
                        
                        return null;
                    }
                    """
                )
                if site_key:
                    print(f"[CAPTCHA_SOLVER] Site key found in frame {i}: {site_key[:20]}...", flush=True)
                    return site_key
            except Exception:
                pass  # Cross-origin or other error
        except Exception:
            pass

    print("[CAPTCHA_SOLVER] Site key not found in any frame", flush=True)
    return None


async def _recaptcha_response_details(context, token_value: Optional[str] = None) -> dict:
    return await context.evaluate(
        """
        (token) => {
            const textarea = document.querySelector('textarea[name="g-recaptcha-response"]');
            const input = document.querySelector('input[name="g-recaptcha-response"]');
            const textareaValue = textarea ? textarea.value : '';
            const inputValue = input ? input.value : '';
            const filled = Boolean(textareaValue || inputValue);
            let grecaptchaResponse = null;
            let grecaptchaAvailable = false;
            try {
                grecaptchaAvailable = Boolean(window.grecaptcha && window.grecaptcha.getResponse);
                if (grecaptchaAvailable) {
                    grecaptchaResponse = window.grecaptcha.getResponse();
                }
            } catch (e) {
                grecaptchaResponse = null;
            }
            const tokenMatch = token
                ? (textareaValue === token || inputValue === token)
                : filled;
            const grecaptchaMatch = token && grecaptchaAvailable
                ? grecaptchaResponse === token
                : null;
            return {
                has_textarea: Boolean(textarea),
                has_input: Boolean(input),
                textarea_value_length: textareaValue.length,
                input_value_length: inputValue.length,
                filled,
                token_match: tokenMatch,
                grecaptcha_available: grecaptchaAvailable,
                grecaptcha_response_length: grecaptchaResponse ? grecaptchaResponse.length : 0,
                grecaptcha_match: grecaptchaMatch,
            };
        }
        """,
        token_value,
    )


async def _log_recaptcha_search_result(context_name: str, details: dict, context_url: str | None) -> None:
    if details.get("has_textarea") or details.get("has_input"):
        print(
            "[CAPTCHA_SOLVER] g-recaptcha-response найден в "
            f"{context_name} (url={context_url}): "
            f"textarea={details.get('has_textarea')}, "
            f"input={details.get('has_input')}, "
            f"filled={details.get('filled')}",
            flush=True,
        )


async def inject_recaptcha_token(page, token: str) -> bool:
    """
    Inject solved reCAPTCHA token into page.
    This sets the g-recaptcha-response value that the form expects.
    Works with both main page and iframes (including ChatModal).
    CRITICAL: For Cian, captcha is usually in ChatModal iframe, so we MUST inject there.
    """
    # Helper function to inject token in a context
    async def inject_in_context(context, context_name: str) -> bool:
        try:
            context_url = getattr(context, "url", None)
            success = await context.evaluate(
                """
                (token) => {
                    console.log('[CAPTCHA_INJECT] Starting token injection, token length:', token.length);
                    let found = false;
                    let details = { methods: [] };
                    
                    // Method 1: Set in textarea (common for reCAPTCHA v2)
                    const textarea = document.querySelector('textarea[name="g-recaptcha-response"]');
                    if (textarea) {
                        console.log('[CAPTCHA_INJECT] Found textarea, setting value...');
                        textarea.value = token;
                        textarea.dispatchEvent(new Event('input', { bubbles: true }));
                        textarea.dispatchEvent(new Event('change', { bubbles: true }));
                        // Trigger focus/blur to ensure form validation
                        textarea.dispatchEvent(new Event('focus', { bubbles: true }));
                        textarea.dispatchEvent(new Event('blur', { bubbles: true }));
                        found = true;
                        details.methods.push('textarea');
                        details.textarea_value_length = textarea.value.length;
                        console.log('[CAPTCHA_INJECT] Textarea value set, length:', textarea.value.length);
                    } else {
                        console.log('[CAPTCHA_INJECT] No textarea found');
                    }
                    
                    // Method 2: Set in hidden input
                    const input = document.querySelector('input[name="g-recaptcha-response"]');
                    if (input) {
                        console.log('[CAPTCHA_INJECT] Found input, setting value...');
                        input.value = token;
                        input.dispatchEvent(new Event('input', { bubbles: true }));
                        input.dispatchEvent(new Event('change', { bubbles: true }));
                        found = true;
                        details.methods.push('input');
                        details.input_value_length = input.value.length;
                        console.log('[CAPTCHA_INJECT] Input value set, length:', input.value.length);
                    } else {
                        console.log('[CAPTCHA_INJECT] No input found');
                    }
                    
                    // Method 3: Try to trigger grecaptcha callback directly
                    if (window.grecaptcha) {
                        console.log('[CAPTCHA_INJECT] grecaptcha found, trying callbacks...');
                        try {
                            // Get widget ID from grecaptcha config
                            let widgetId = null;
                            if (window.___grecaptcha_cfg && window.___grecaptcha_cfg.clients) {
                                const clients = window.___grecaptcha_cfg.clients;
                                for (const clientId in clients) {
                                    const client = clients[clientId];
                                    if (client && client.widgets) {
                                        for (const widgetIdKey in client.widgets) {
                                            widgetId = parseInt(widgetIdKey);
                                            console.log('[CAPTCHA_INJECT] Found widget ID:', widgetId);
                                            break;
                                        }
                                    }
                                }
                            }
                            
                            // Try multiple callback methods
                            const callbacks = [
                                { name: '__grecaptcha_callback', fn: window.__grecaptcha_callback },
                                { name: 'grecaptcha_callback', fn: window.grecaptcha_callback }
                            ];
                            
                            for (const cb of callbacks) {
                                if (typeof cb.fn === 'function') {
                                    try {
                                        console.log('[CAPTCHA_INJECT] Calling callback:', cb.name);
                                        cb.fn(token);
                                        found = true;
                                        details.methods.push(`callback_${cb.name}`);
                                        console.log('[CAPTCHA_INJECT] Callback called successfully');
                                        break;
                                    } catch (e) {
                                        console.log('[CAPTCHA_INJECT] Callback error:', e);
                                    }
                                }
                            }
                            
                            // Also try to trigger via widget callback
                            if (widgetId !== null && window.___grecaptcha_cfg) {
                                try {
                                    const clients = window.___grecaptcha_cfg.clients;
                                    for (const clientId in clients) {
                                        const client = clients[clientId];
                                        if (client && client.widgets && client.widgets[widgetId]) {
                                            const widget = client.widgets[widgetId];
                                            if (widget && widget.callback) {
                                                console.log('[CAPTCHA_INJECT] Calling widget callback for widget', widgetId);
                                                widget.callback(token);
                                                found = true;
                                                details.methods.push(`widget_callback_${widgetId}`);
                                            }
                                        }
                                    }
                                } catch (e) {
                                    console.log('[CAPTCHA_INJECT] Widget callback error:', e);
                                }
                            }
                            
                            // Try getResponse to verify token is set
                            if (widgetId !== null && window.grecaptcha.getResponse) {
                                try {
                                    const currentResponse = window.grecaptcha.getResponse(widgetId);
                                    console.log('[CAPTCHA_INJECT] Current grecaptcha response length:', currentResponse ? currentResponse.length : 0);
                                    details.grecaptcha_response_length = currentResponse ? currentResponse.length : 0;
                                } catch (e) {
                                    console.log('[CAPTCHA_INJECT] getResponse error:', e);
                                }
                            }
                        } catch (e) {
                            console.log('[CAPTCHA_INJECT] grecaptcha callback error:', e);
                            details.grecaptcha_error = String(e);
                        }
                    } else {
                        console.log('[CAPTCHA_INJECT] No grecaptcha found');
                    }
                    
                    // Method 4: Try to find and click the recaptcha checkbox if visible
                    try {
                        const recaptchaFrame = document.querySelector('iframe[src*="recaptcha"]');
                        if (recaptchaFrame) {
                            console.log('[CAPTCHA_INJECT] Found recaptcha iframe');
                            // Try to access the iframe content (might fail due to CORS)
                            try {
                                const iframeDoc = recaptchaFrame.contentDocument || recaptchaFrame.contentWindow.document;
                                if (iframeDoc) {
                                    const checkbox = iframeDoc.querySelector('.recaptcha-checkbox');
                                    if (checkbox) {
                                        console.log('[CAPTCHA_INJECT] Found checkbox, clicking...');
                                        checkbox.click();
                                        found = true;
                                        details.methods.push('checkbox_click');
                                    }
                                }
                            } catch (e) {
                                console.log('[CAPTCHA_INJECT] Cannot access iframe (CORS):', e);
                            }
                        }
                    } catch (e) {
                        console.log('[CAPTCHA_INJECT] Checkbox click error:', e);
                    }
                    
                    const textareaValue = textarea ? textarea.value : '';
                    const inputValue = input ? input.value : '';
                    const filled = Boolean(textareaValue || inputValue);
                    let grecaptchaResponse = null;
                    let grecaptchaAvailable = false;
                    try {
                        grecaptchaAvailable = Boolean(window.grecaptcha && window.grecaptcha.getResponse);
                        if (grecaptchaAvailable) {
                            grecaptchaResponse = window.grecaptcha.getResponse();
                        }
                    } catch (e) {
                        grecaptchaResponse = null;
                    }

                    details.found = found;
                    details.has_textarea = Boolean(textarea);
                    details.has_input = Boolean(input);
                    details.filled = filled;
                    details.token_match = textareaValue === token || inputValue === token;
                    details.grecaptcha_available = grecaptchaAvailable;
                    details.grecaptcha_response_length = grecaptchaResponse ? grecaptchaResponse.length : 0;
                    details.grecaptcha_match = grecaptchaAvailable ? grecaptchaResponse === token : null;
                    console.log('[CAPTCHA_INJECT] Injection result:', details);
                    return details;
                }
                """,
                token,
            )
            
            # Parse the result - it's now a dict with details
            if isinstance(success, dict):
                found = success.get('found', False)
                methods = success.get('methods', [])
                print(f"[CAPTCHA_SOLVER] Инъекция в {context_name}: found={found}, methods={methods}", flush=True)
                if 'textarea' in methods:
                    print(f"[CAPTCHA_SOLVER]   - textarea value length: {success.get('textarea_value_length', 0)}", flush=True)
                if 'input' in methods:
                    print(f"[CAPTCHA_SOLVER]   - input value length: {success.get('input_value_length', 0)}", flush=True)
                if 'grecaptcha_response_length' in success:
                    print(f"[CAPTCHA_SOLVER]   - grecaptcha response length: {success.get('grecaptcha_response_length', 0)}", flush=True)
                await _log_recaptcha_search_result(context_name, success, context_url)
                if not success.get("token_match"):
                    print(
                        f"[CAPTCHA_SOLVER] ✗ g-recaptcha-response не заполнен в {context_name} (url={context_url})",
                        flush=True,
                    )
                    return False
                if success.get("grecaptcha_match") is False:
                    print(
                        f"[CAPTCHA_SOLVER] ✗ grecaptcha.getResponse не подтверждает токен в {context_name} (url={context_url})",
                        flush=True,
                    )
                    return False
                return True
            else:
                # Backward compat - if evaluate returns bool
                return bool(success)
        except Exception as e:
            print(f"[CAPTCHA_SOLVER] Error injecting in {context_name}: {e}", flush=True)
            return False

    # CRITICAL: Try ChatModal iframe FIRST (Cian-specific, captcha is usually here)
    print("[CAPTCHA_SOLVER] Приоритет: инъекция в ChatModal iframe...", flush=True)
    try:
        # Check if ChatModal exists
        try:
            chat_modal_count = await page.locator('[data-testid="ChatModal"]').count()
            print(f"[CAPTCHA_SOLVER] ChatModal найден: count={chat_modal_count}", flush=True)
        except Exception:
            print("[CAPTCHA_SOLVER] ChatModal не найден на главной странице", flush=True)
        
        # Find the actual Frame object for ChatModal iframe
        # We need to find it by checking frames that contain the captcha element
        chat_modal_frame_obj = None
        frames = page.frames
        print(f"[CAPTCHA_SOLVER] Проверяю {len(frames)} фреймов для поиска ChatModal...", flush=True)
        
        for i, frame in enumerate(frames):
            try:
                # Try to find frame that has the captcha element
                captcha_locator = frame.locator(".x61f99309--_919db--captcha > div")
                if await captcha_locator.count() > 0:
                    print(f"[CAPTCHA_SOLVER] ✓ ChatModal iframe найден: frame {i}", flush=True)
                    chat_modal_frame_obj = frame
                    break
            except Exception:
                pass
        
        if chat_modal_frame_obj:
            print("[CAPTCHA_SOLVER] Инъекция токена в реальный Frame объект ChatModal...", flush=True)
            success = await inject_in_context(chat_modal_frame_obj, "ChatModal iframe (Frame)")
            if success:
                print("[CAPTCHA_SOLVER] ✓ Токен успешно введен в ChatModal iframe", flush=True)
                # Also try to get detailed info about what was injected
                try:
                    injected_info = await chat_modal_frame_obj.evaluate(
                        """
                        () => {
                            const textarea = document.querySelector('textarea[name="g-recaptcha-response"]');
                            const input = document.querySelector('input[name="g-recaptcha-response"]');
                            return {
                                has_textarea: !!textarea,
                                textarea_value_length: textarea ? textarea.value.length : 0,
                                has_input: !!input,
                                input_value_length: input ? input.value.length : 0,
                                has_grecaptcha: !!window.grecaptcha,
                                grecaptcha_response: window.grecaptcha && window.grecaptcha.getResponse ? window.grecaptcha.getResponse() : null
                            };
                        }
                        """
                    )
                    print(f"[CAPTCHA_SOLVER] Детали инъекции в ChatModal: {injected_info}", flush=True)
                except Exception as e:
                    print(f"[CAPTCHA_SOLVER] Ошибка получения деталей: {e}", flush=True)
                
                # NOTE: We don't click checkbox anymore - 2captcha should handle it
                # Clicking checkbox manually can trigger visual challenge before 2captcha solves it
                print("[CAPTCHA_SOLVER] Токен введен, ожидаю активации капчи...", flush=True)
                
                # CRITICAL: Try to activate the form immediately after injection
                print("[CAPTCHA_SOLVER] Активирую форму в ChatModal iframe...", flush=True)
                try:
                    await chat_modal_frame_obj.evaluate(
                        """
                        () => {
                            console.log('[CAPTCHA_SOLVER] Activating form after token injection...');
                            
                            // Find textarea with token
                            const textarea = document.querySelector('textarea[name="g-recaptcha-response"]');
                            if (textarea && textarea.value) {
                                console.log('[CAPTCHA_SOLVER] Found textarea with token, triggering events...');
                                // Trigger all necessary events
                                textarea.dispatchEvent(new Event('input', { bubbles: true }));
                                textarea.dispatchEvent(new Event('change', { bubbles: true }));
                                textarea.dispatchEvent(new Event('focus', { bubbles: true }));
                                textarea.dispatchEvent(new Event('blur', { bubbles: true }));
                                
                                // CRITICAL: Try to trigger grecaptcha callback with the token
                                if (window.grecaptcha && window.___grecaptcha_cfg) {
                                    try {
                                        const clients = window.___grecaptcha_cfg.clients;
                                        for (const clientId in clients) {
                                            const client = clients[clientId];
                                            if (client && client.widgets) {
                                                for (const widgetId in client.widgets) {
                                                    const widget = client.widgets[widgetId];
                                                    if (widget && widget.callback) {
                                                        console.log('[CAPTCHA_SOLVER] Calling widget callback with token...');
                                                        widget.callback(textarea.value);
                                                    }
                                                }
                                            }
                                        }
                                        
                                        // Also try to execute grecaptcha if available
                                        if (window.grecaptcha.execute) {
                                            try {
                                                const widgetId = parseInt(Object.keys(client.widgets)[0]);
                                                if (!isNaN(widgetId)) {
                                                    console.log('[CAPTCHA_SOLVER] Executing grecaptcha for widget', widgetId);
                                                    window.grecaptcha.execute(widgetId, { action: 'submit' });
                                                }
                                            } catch(e) {
                                                console.log('[CAPTCHA_SOLVER] Execute error:', e);
                                            }
                                        }
                                    } catch(e) {
                                        console.log('[CAPTCHA_SOLVER] Callback error:', e);
                                    }
                                }
                            }
                        }
                        """
                    )
                    print("[CAPTCHA_SOLVER] Форма активирована", flush=True)
                except Exception as e:
                    print(f"[CAPTCHA_SOLVER] Ошибка активации формы: {e}", flush=True)
                
                return True
            else:
                print("[CAPTCHA_SOLVER] ✗ Не удалось ввести токен в ChatModal iframe", flush=True)
        else:
            print("[CAPTCHA_SOLVER] ChatModal iframe не найден среди фреймов, пробую через frame_locator...", flush=True)
            # Fallback: try to use frame_locator and find the actual frame
            chat_modal_frame_locator = page.frame_locator('[data-testid="ChatModal"]')
            # We can't use evaluate on FrameLocator, so we'll skip this and try frames directly
    except Exception as e:
        print(f"[CAPTCHA_SOLVER] ChatModal frame injection error: {e}", flush=True)
        import traceback
        print(f"[CAPTCHA_SOLVER] Traceback: {traceback.format_exc()}", flush=True)

    # Fallback: Try main page
    print("[CAPTCHA_SOLVER] Fallback: инъекция в главную страницу...", flush=True)
    success = await inject_in_context(page, "main page")
    if success:
        print("[CAPTCHA_SOLVER] ✓ Токен успешно введен в главную страницу", flush=True)
        return True

    # Try all frames
    frames = page.frames
    for i, frame in enumerate(frames):
        try:
            success = await inject_in_context(frame, f"frame {i}")
            if success:
                return True
        except Exception:
            pass  # Cross-origin or other error

    print("[CAPTCHA_SOLVER] Token injection failed in all contexts", flush=True)
    return False


async def find_recaptcha_response_context(page, token: Optional[str] = None) -> Optional[dict]:
    """
    Search for g-recaptcha-response textarea/input on main page and then in each frame.
    Logs where it was found (frame name/URL).
    Returns details for the first filled/matching context (main page has priority).
    """
    print(
        "[CAPTCHA_SOLVER] Поиск g-recaptcha-response: главная страница -> фреймы...",
        flush=True,
    )

    try:
        details = await _recaptcha_response_details(page, token)
        await _log_recaptcha_search_result("main page", details, page.url)
        if details.get("filled") and details.get("token_match"):
            return {"context": "main page", "url": page.url, "details": details}
    except Exception as e:
        print(f"[CAPTCHA_SOLVER] Ошибка поиска g-recaptcha-response на странице: {e}", flush=True)

    for index, frame in enumerate(page.frames):
        frame_name = frame.name or f"frame-{index}"
        frame_url = frame.url
        try:
            details = await _recaptcha_response_details(frame, token)
            await _log_recaptcha_search_result(f"frame {frame_name}", details, frame_url)
            if details.get("filled") and details.get("token_match"):
                return {"context": frame_name, "url": frame_url, "details": details}
        except Exception:
            continue

    print("[CAPTCHA_SOLVER] g-recaptcha-response не найден или не заполнен", flush=True)
    return None
