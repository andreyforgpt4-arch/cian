from __future__ import annotations

import argparse
import json
from pathlib import Path
import time

from cian_bot.cian_api import CianApi, with_paging
from cian_bot.config import load_core_settings
from cian_bot.console import safe_print
from cian_bot.n8n_create_photo import N8NCreatePhotoClient
from cian_bot.n8n_scoring import N8NPhotoScoringClient
from cian_bot.photo_type import is_target_room_type
from cian_bot.photo_score_validation import validate_photo_score
from cian_bot.state_store import StateStore

from cian_bot.score_photos import DEFAULT_PROMPT  # reuse current 0..100 prompt text


def main() -> None:
    ap = argparse.ArgumentParser(description="Parse Cian -> score photos -> stop on first offer with interior <= threshold")
    ap.add_argument("--payload", required=True, help="Path to Cian search payload JSON")
    ap.add_argument("--threshold", type=int, default=60, help="Stop when interior score <= threshold (0..100)")
    ap.add_argument("--max-offers", type=int, default=50, help="How many offers to inspect at most")
    ap.add_argument("--max-pages", type=int, default=10)
    ap.add_argument("--delay-ms", type=int, default=300, help="Delay between n8n calls (ms)")
    ap.add_argument("--create-photo", action="store_true", help="Call create_photo webhook when first bad interior is found")
    args = ap.parse_args()

    core = load_core_settings()
    store = StateStore(core.state_db_path)
    api = CianApi(storage_state_path=core.storage_state_path)
    scorer = N8NPhotoScoringClient()
    creator = N8NCreatePhotoClient() if args.create_photo else None

    payload = json.loads(Path(args.payload).read_text(encoding="utf-8"))

    inspected = 0
    for page in range(1, args.max_pages + 1):
        paged = with_paging(payload, page=page, page_size=min(50, args.max_offers))
        offers = api.search_offers_parsed(paged)
        if not offers:
            break

        for offer in offers:
            store.upsert_discovered(
                offer.offer_id,
                offer.cian_url,
                offer.photos,
                seller_first_name=offer.seller_first_name,
                offer_description=offer.offer_description,
                raw=offer.raw,
            )

            inspected += 1
            safe_print(f"\nOffer {offer.offer_id} photos={len(offer.photos)} url={offer.cian_url}")

            # Score photos in order, stop on first interior <= threshold
            for idx, photo_url in enumerate(offer.photos):
                if args.delay_ms > 0:
                    time.sleep(args.delay_ms / 1000.0)
                res = scorer.score(offer_id=offer.offer_id, idx=idx, photo_url=photo_url)
                res = validate_photo_score(res)
                ptype = str(res.get("photo_type") or "").strip().lower()
                score = int(res["improvement_score_0to100"])

                store.upsert_photo_analysis(
                    offer_id=offer.offer_id,
                    photo_url=photo_url,
                    idx=idx,
                    category=ptype or None,
                    score=score,
                    raw_json=json.dumps(res, ensure_ascii=False),
                )

                safe_print(f"  [{idx}] {ptype=} {score=} {photo_url}")

                if is_target_room_type(ptype) and score <= args.threshold:
                    store.set_selected_photo(
                        offer_id=offer.offer_id,
                        photo_url=photo_url,
                        score=score,
                        category=ptype,
                    )
                    store.set_status(offer.offer_id, "scored")
                    safe_print("\n=== FOUND FIRST BAD INTERIOR ===")
                    safe_print(f"offer_id={offer.offer_id}")
                    safe_print(f"offer_url={offer.cian_url}")
                    safe_print(f"photo_url={photo_url}")
                    safe_print(f"score={score}")

                    # Optional: call create_photo webhook and print google drive id + message
                    if creator:
                        import base64, requests

                        img = requests.get(photo_url, headers={"user-agent": "Mozilla/5.0"}, timeout=60)
                        img.raise_for_status()
                        mime = (img.headers.get("content-type") or "image/jpeg").split(";")[0].strip()
                        payload = {
                            "offer_id": offer.offer_id,
                            "offer_url": offer.cian_url,
                            "seller_first_name": offer.seller_first_name,
                            "offer_description": offer.offer_description,
                            "selected_photo": {
                                "photo_url": photo_url,
                                "photo_type": ptype,
                                "improvement_score_0to100": score,
                                "image_mime": mime,
                                "image_base64": base64.b64encode(img.content).decode("ascii"),
                            },
                        }
                        resp = creator.create(payload)
                        safe_print("\n=== CREATE_PHOTO RESPONSE ===")
                        safe_print(f"google_drive_file_id={resp.get('google_drive_file_id')}")
                        safe_print(f"google_drive_view_url={resp.get('google_drive_view_url')}")
                        safe_print(f"message_text={resp.get('message_text')}")

                    store.close()
                    return

            store.set_status(offer.offer_id, "scored")

            if inspected >= args.max_offers:
                store.close()
                safe_print("\nNo interior photo <= threshold found within max-offers.")
                return

    store.close()
    safe_print("\nNo interior photo <= threshold found.")


if __name__ == "__main__":
    main()


