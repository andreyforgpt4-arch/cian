from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Optional

from playwright.async_api import Page


@dataclass(frozen=True)
class ListingInfo:
    seller_first_name: Optional[str]
    description: Optional[str]


def _first_name_only(full: str) -> str:
    full = (full or "").strip()
    if not full:
        return ""
    # keep first token (letters + dash)
    parts = re.split(r"\s+", full)
    return parts[0].strip()


async def scrape_listing_info(page: Page, url: str) -> ListingInfo:
    await page.goto(url, wait_until="domcontentloaded")
    await page.wait_for_timeout(1200)

    # Description: try common selectors
    desc = None
    for loc in [
        page.locator('[data-name="Description"]').first,
        page.locator('[data-testid="Description"]').first,
        page.locator('[itemprop="description"]').first,
        page.locator('section:has-text("Описание")').locator("text=/./").first,
    ]:
        try:
            if await loc.is_visible():
                txt = (await loc.inner_text()).strip()
                if txt and len(txt) > 20:
                    desc = txt
                    break
        except Exception:
            continue

    # Seller name: best-effort (varies by Cian layout)
    seller = None
    candidates = [
        page.locator('[data-name="OfferContacts"]').first,
        page.locator('[data-testid="OfferContacts"]').first,
        page.locator('section:has-text("Контакты")').first,
    ]
    for c in candidates:
        try:
            if await c.is_visible():
                txt = (await c.inner_text()).strip()
                # try to find a name-looking token near top
                lines = [x.strip() for x in txt.splitlines() if x.strip()]
                if lines:
                    seller = lines[0]
                    break
        except Exception:
            continue

    # fallback: any element that looks like a person name near "Написать" button
    if not seller:
        try:
            btn = page.get_by_role("button", name=re.compile(r"написать", re.I)).first
            box = btn.locator("xpath=ancestor::*[self::section or self::div][1]")
            txt = (await box.inner_text()).strip()
            lines = [x.strip() for x in txt.splitlines() if x.strip()]
            if lines:
                seller = lines[0]
        except Exception:
            pass

    seller_first = _first_name_only(seller or "") or None
    return ListingInfo(seller_first_name=seller_first, description=desc)


