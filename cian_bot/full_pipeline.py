from __future__ import annotations

import argparse
import asyncio
import base64
import json
import os
from pathlib import Path
import time

import requests

from cian_bot.cian_api import CianApi, with_paging
from cian_bot.config import load_core_settings
from cian_bot.console import safe_print
from cian_bot.n8n_create_photo import N8NCreatePhotoClient
from cian_bot.n8n_scoring import N8NPhotoScoringClient
from cian_bot.photo_score_validation import validate_photo_score
from cian_bot.photo_type import is_target_room_type
from cian_bot.run import main as run_send_main
from cian_bot.state_store import StateStore


async def main_async() -> None:
    ap = argparse.ArgumentParser(description="End-to-end: parse -> score -> create_photo -> send message+photo")
    ap.add_argument("--payload", required=True, help="Path to payload JSON for Cian search")
    ap.add_argument(
        "--threshold",
        "--bad-threshold",
        dest="bad_threshold",
        type=int,
        default=60,
        help="Immediate processing threshold: if interior score < this value, process immediately. Default=60.",
    )
    ap.add_argument(
        "--process-max",
        dest="process_max",
        type=int,
        default=90,
        help="If no interior < bad-threshold, process the minimum interior in [bad-threshold..process-max] after scoring all. Default=90.",
    )
    ap.add_argument("--max-offers", type=int, default=50)
    ap.add_argument("--max-pages", type=int, default=10)
    ap.add_argument("--delay-ms", type=int, default=300, help="Delay between webhook calls")
    ap.add_argument("--message-file", default="generated_message.txt", help="Where to write generated message text")
    ap.add_argument(
        "--max-sends",
        type=int,
        default=1,
        help="How many offers to fully send (message+photo) in this run. 0 = no limit. Default=1.",
    )
    ap.add_argument(
        "--skip-status",
        default="scored_good,scored_bad,messaged,photo_sent,error",
        help="Comma-separated offer statuses to skip (default: scored_good,scored_bad,messaged,photo_sent,error)",
    )
    args = ap.parse_args()

    core = load_core_settings()
    store = StateStore(core.state_db_path)
    api = CianApi(storage_state_path=core.storage_state_path)
    scorer = N8NPhotoScoringClient()
    creator = N8NCreatePhotoClient()

    payload = json.loads(Path(args.payload).read_text(encoding="utf-8"))
    skip_statuses = {s.strip() for s in (args.skip_status or "").split(",") if s.strip()}

    inspected = 0
    sends_done = 0
    skipped_count = 0
    for page in range(1, args.max_pages + 1):
        paged = with_paging(payload, page=page, page_size=min(50, args.max_offers))
        offers = api.search_offers_parsed(paged)
        if not offers:
            safe_print(f"\nNo more offers on page {page}, stopping.")
            break

        safe_print(f"\n=== Page {page}: {len(offers)} offers ===")
        for offer in offers:
            # CRITICAL: Check status BEFORE upsert_discovered to avoid resetting status
            existing = store.get(offer.offer_id)
            if existing and existing.status in skip_statuses:
                skipped_count += 1
                safe_print(f"Offer {offer.offer_id} status={existing.status!r} -> SKIP (skipped {skipped_count} total)")
                # Continue to next offer, don't stop
                continue
            
            # Only update if not skipping
            store.upsert_discovered(
                offer.offer_id,
                offer.cian_url,
                offer.photos,
                seller_first_name=offer.seller_first_name,
                offer_description=offer.offer_description,
                raw=offer.raw,
            )
            
            # Found an offer that needs processing
            inspected += 1
            skipped_count = 0  # Reset skipped counter when we find something to process
            safe_print(f"\n>>> Processing Offer {offer.offer_id} photos={len(offer.photos)} url={offer.cian_url}")

            # Selection logic for target room types (room_living_*, kitchen, bathroom, corridor):
            # - If any target room score < bad_threshold: process immediately, stop scoring remaining photos.
            # - Else, if any target room score in [bad_threshold..process_max]: after scoring all photos, pick minimal and process.
            # - Else (all target rooms > process_max or no target rooms): mark scored_good and do not process.
            chosen: tuple[int, int, str, str] | None = None  # (score, idx, photo_url, photo_type)
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
                safe_print(f"  [{idx}] {ptype=} score={score} {photo_url}")

                if is_target_room_type(ptype):
                    # Case 1: strictly below bad_threshold => process immediately (stop scoring others).
                    if score < args.bad_threshold:
                        chosen = (score, idx, photo_url, ptype)
                        break
                    # Case 2: no < bad_threshold, but in [bad_threshold..process_max] => remember minimal
                    if args.bad_threshold <= score <= args.process_max:
                        if chosen is None or score < chosen[0] or (score == chosen[0] and idx < chosen[1]):
                            chosen = (score, idx, photo_url, ptype)

            # Decide what to do with this offer after scoring loop
            if chosen is None:
                store.set_status(offer.offer_id, "scored_good")
                continue

            score, idx, photo_url, ptype = chosen
            safe_print("\n=== SELECTED TARGET ROOM PHOTO ===")
            safe_print(f"offer_id={offer.offer_id}")
            safe_print(f"offer_url={offer.cian_url}")
            safe_print(f"photo_url={photo_url}")
            safe_print(f"score={score}")
            safe_print(f"photo_type={ptype}")

            store.set_status(offer.offer_id, "scored_bad")
            try:
                store.set_selected_photo(offer_id=offer.offer_id, photo_url=photo_url, score=score, category=ptype)
            except Exception:
                pass

            # create_photo payload requires base64
            img = requests.get(photo_url, headers={"user-agent": "Mozilla/5.0"}, timeout=60)
            img.raise_for_status()
            mime = (img.headers.get("content-type") or "image/jpeg").split(";")[0].strip()

            create_payload = {
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

            try:
                create_resp = creator.create(create_payload)
            except Exception as e:
                store.set_status(offer.offer_id, "error", last_error=f"create_photo failed: {e}")
                safe_print(f"\n!!! CREATE_PHOTO FAILED offer_id={offer.offer_id}: {e}")
                # If testing with single offer (max_offers=1), stop on error instead of continuing
                if args.max_offers == 1:
                    safe_print(f"\n!!! Stopping due to error (--max-offers=1). Fix the issue and retry.")
                    store.close()
                    return
                continue

            drive_id = create_resp.get("google_drive_file_id")
            message_text = create_resp.get("message_text")

            safe_print("\n=== CREATE_PHOTO RESPONSE ===")
            safe_print(f"google_drive_file_id={drive_id}")
            safe_print(f"google_drive_view_url={create_resp.get('google_drive_view_url')}")
            safe_print(f"message_text={message_text}")

            if not drive_id or not isinstance(drive_id, str):
                raise RuntimeError("create_photo did not return google_drive_file_id")
            if not message_text or not isinstance(message_text, str):
                raise RuntimeError("create_photo did not return message_text")

            msg_path = Path(args.message_file).resolve()
            msg_path.write_text(message_text, encoding="utf-8")

            # Hand off to existing send+attach flow
            os.environ["CIAN_AD_URL"] = offer.cian_url or ""
            os.environ["CIAN_MESSAGE_FILE"] = str(msg_path)
            os.environ["GOOGLE_DRIVE_FILE_ID"] = drive_id
            os.environ["STORAGE_STATE_PATH"] = str(core.storage_state_path)
            os.environ.setdefault("HEADLESS", "false")
            os.environ.setdefault("CAPTCHA_PAUSE", "true")

            safe_print("\n=== SENDING MESSAGE + ATTACHING PHOTO ===")
            try:
                await run_send_main()
                store.set_status(offer.offer_id, "photo_sent")
                sends_done += 1
                if args.max_sends > 0 and sends_done >= args.max_sends:
                    store.close()
                    return
                # continue to next offer after successful send
                continue
            except Exception as e:
                store.set_status(offer.offer_id, "error", last_error=str(e))
                safe_print(f"\n!!! SEND FAILED offer_id={offer.offer_id}: {e}")
                # If testing with single offer (max_offers=1), stop on error instead of continuing
                if args.max_offers == 1:
                    safe_print(f"\n!!! Stopping due to error (--max-offers=1). Fix the issue and retry.")
                    store.close()
                    return
                continue

            if inspected >= args.max_offers:
                store.close()
                print("\nNo interior photo <= threshold found within max-offers.", flush=True)
                return

    store.close()
    print("\nNo interior photo <= threshold found.", flush=True)


def main() -> None:
    asyncio.run(main_async())


if __name__ == "__main__":
    main()


