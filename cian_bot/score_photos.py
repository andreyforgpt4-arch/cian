from __future__ import annotations

import argparse
import json
import os
import time

from cian_bot.config import load_core_settings
from cian_bot.n8n_scoring import N8NPhotoScoringClient
from cian_bot.openai_vision import OpenAIVisionClient
from cian_bot.photo_score_validation import validate_photo_score
from cian_bot.state_store import StateStore


DEFAULT_PROMPT = """You are a strict real-estate listing photo QUALITY evaluator (not an interior designer).

TASK:
1) Determine photo type from the image:
- exterior: building/facade/yard/outdoor view
- interior: rooms/kitchen/bathroom interior
- entrance: подъезд/elevator/stairs/entrance group
- floorplan: plan/drawing/scheme
- other: everything else

2) Rate whether this photo should be improved on a 100-point scale:
- 100 = no improvement needed (already looks like a clean professional listing photo)
- 0 = must improve (photo quality severely hurts perception)
IMPORTANT: Judge ONLY the PHOTO quality (light, framing, perspective, sharpness, noise, color, clutter, UI artifacts).
Do NOT judge renovation quality, furniture style/value, “expensiveness”, or interior design taste.

SCORING METHOD (MANDATORY):
Assign severity points for each criterion (higher = worse).
Use:
0 = none/minor
1 = noticeable but acceptable
2 = strong problem
3 = critical
Criteria and ranges:
A) Lighting/exposure/dynamic range: 0..3
B) White balance / color cast: 0..2
C) Sharpness / blur / focus: 0..3
D) Noise / compression / low resolution: 0..2
E) Perspective / verticals / lens distortion: 0..3
F) Framing / composition / cropping / room context: 0..2
G) Distractions: clutter, messy items, reflections, personal data, UI/screenshot artifacts: 0..3

Compute S = A+B+C+D+E+F+G (max 18).
Compute improvement_score_0to100 = clamp to 0..100 of round(100 - (S/18)*100).

TYPE-SPECIFIC NOTES:
- floorplan: prioritize legibility (straight lines, no skew, enough contrast/resolution, no shadows/glare, text not cropped).
- exterior: prioritize straight verticals, sky/highlights handling, overall clarity, balanced exposure.
- entrance: prioritize exposure, color neutrality, distortion/verticals, clutter.
- other: still score photo quality; if it is a screenshot or document, mention that.

OUTPUT (STRICT JSON ONLY, NO MARKDOWN, NO EXTRA TEXT):
{
  "photo_type": "exterior|interior|entrance|floorplan|other",
  "improvement_score_0to100": <integer 0..100>,
  "severity_breakdown": {
    "lighting_exposure_0to3": <int>,
    "white_balance_0to2": <int>,
    "sharpness_blur_0to3": <int>,
    "noise_compression_0to2": <int>,
    "perspective_verticals_0to3": <int>,
    "framing_composition_0to2": <int>,
    "distractions_clutter_UI_privacy_0to3": <int>
  },
  "top_issues": [
    "<short issue 1>",
    "<short issue 2>",
    "<short issue 3>"
  ],
  "should_improve": "<yes|maybe|no>",
  "quick_win_fixes": [
    "<what to fix first (photo-quality only)>",
    "<what to fix second>",
    "<what to fix third>"
  ],
  "risk_notes": [
    "Do not change finishes, do not change major furniture, do not add rugs if none, avoid over-HDR/oversaturation."
  ]
}

Rules for should_improve:
- score 0-60 => "yes"
- score 61-80 => "maybe"
- score 81-100 => "no"
"""


def main() -> None:
    parser = argparse.ArgumentParser(description="Score Cian offer photos via OpenAI Vision and store results in state.db")
    parser.add_argument("--limit", type=int, default=50, help="How many offers to process from DB")
    parser.add_argument("--threshold", type=int, default=60, help="Stop early when interior score <= threshold (0..100)")
    parser.add_argument(
        "--statuses",
        default="discovered",
        help="Comma-separated offer statuses to pick from DB (default: discovered)",
    )
    parser.add_argument("--webhook-delay-ms", type=int, default=300, help="Delay between webhook requests (ms)")
    args = parser.parse_args()

    core = load_core_settings()
    store = StateStore(core.state_db_path)
    # Prefer webhook scoring (n8n) if configured, otherwise fallback to OpenAI.
    webhook_url = (os.getenv("PHOTO_SCORING_WEBHOOK_URL", "") or "").strip()
    webhook_client = N8NPhotoScoringClient(webhook_url=webhook_url) if webhook_url else None
    openai_client = None if webhook_client else OpenAIVisionClient()

    statuses = [s.strip() for s in (args.statuses or "").split(",") if s.strip()]
    offers = store.list_by_status(statuses, limit=args.limit)
    print(f"Offers to score: {len(offers)}")

    prompt = DEFAULT_PROMPT
    for offer in offers:
        if not offer.photos:
            store.set_status(offer.offer_id, "scored", last_error="no_photos")
            continue

        print(f"\nOffer {offer.offer_id} photos={len(offer.photos)} url={offer.cian_url}")
        chosen = None

        for idx, photo_url in enumerate(offer.photos):
            try:
                if webhook_client:
                    # Ensure requests are sequential and gentle for n8n.
                    if args.webhook_delay_ms > 0:
                        time.sleep(args.webhook_delay_ms / 1000.0)
                    res = webhook_client.score(offer_id=offer.offer_id, idx=idx, photo_url=photo_url)
                else:
                    res = openai_client.analyze_photo(image_url=photo_url, prompt=prompt)  # type: ignore[union-attr]
                res = validate_photo_score(res)
                category = str(res.get("photo_type") or "").strip().lower()
                score_int = int(res["improvement_score_0to100"])

                store.upsert_photo_analysis(
                    offer_id=offer.offer_id,
                    photo_url=photo_url,
                    idx=idx,
                    category=category or None,
                    score=score_int,
                    raw_json=json.dumps(res, ensure_ascii=False),
                )

                print(f"  [{idx}] {category=} {score_int=} {photo_url}")

                if category == "interior" and score_int is not None and score_int <= args.threshold:
                    chosen = (photo_url, score_int, category)
                    break
            except Exception as e:
                store.upsert_photo_analysis(
                    offer_id=offer.offer_id,
                    photo_url=photo_url,
                    idx=idx,
                    category=None,
                    score=None,
                    raw_json=json.dumps({"error": str(e)}, ensure_ascii=False),
                )
                print(f"  [{idx}] error: {e}")
                continue

        if chosen:
            photo_url, score_int, category = chosen
            store.set_selected_photo(
                offer_id=offer.offer_id,
                photo_url=photo_url,
                score=score_int,
                category=category,
            )
            store.set_status(offer.offer_id, "scored")
            print(f"Selected candidate: score={score_int} url={photo_url}")
        else:
            # Scored all photos (or stopped due to errors) but no candidate met criteria
            store.set_status(offer.offer_id, "scored")
            print("No interior photo with score <= threshold found.")

    # Summary table for debugging
    print("\n=== SUMMARY (worst INTERIOR photo per offer) ===")
    header = f"{'offer_id':>10}  {'score':>5}  {'type':>9}  {'interior_photo_url':<60}  cian_url"
    print(header)
    print("-" * len(header))
    for offer in offers:
        st = store.get(offer.offer_id)
        if not st:
            continue
        if (
            st.selected_photo_url
            and st.selected_photo_score is not None
            and (st.selected_photo_category or "").lower() == "interior"
        ):
            photo_url = st.selected_photo_url
            score = st.selected_photo_score
            ptype = st.selected_photo_category or ""
        else:
            worst = store.get_worst_interior_photo(offer.offer_id)
            if worst:
                photo_url = worst["photo_url"]
                score = worst["score"]
                ptype = worst.get("category") or ""
            else:
                photo_url = ""
                score = ""
                ptype = ""
        short_photo = (photo_url[:57] + "...") if isinstance(photo_url, str) and len(photo_url) > 60 else photo_url
        print(f"{offer.offer_id:>10}  {str(score):>5}  {ptype:>9}  {short_photo:<60}  {offer.cian_url or ''}")

    store.close()


if __name__ == "__main__":
    main()


