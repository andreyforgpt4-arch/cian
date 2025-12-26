from __future__ import annotations

import argparse
import re
import time

from cian_bot.config import load_core_settings
from cian_bot.n8n_scoring import N8NPhotoScoringClient
from cian_bot.photo_score_validation import validate_photo_score
from cian_bot.state_store import StateStore


PROMPT_0_100 = """You are a strict real-estate listing photo QUALITY evaluator (not an interior designer).

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
  "top_issues": ["<short issue 1>", "<short issue 2>", "<short issue 3>"],
  "should_improve": "<yes|maybe|no>",
  "quick_win_fixes": ["<fix 1>", "<fix 2>", "<fix 3>"],
  "risk_notes": ["Do not change finishes, do not change major furniture, do not add rugs if none, avoid over-HDR/oversaturation."]
}
"""


def _extract_offer_id(url: str) -> int:
    m = re.search(r"/sale/flat/(\d+)/?", url)
    if not m:
        raise ValueError("Cannot extract offer_id from URL")
    return int(m.group(1))


def main() -> None:
    parser = argparse.ArgumentParser(description="Score ALL photos for a single Cian offer via n8n webhook")
    parser.add_argument("--offer-url", required=True, help="Cian listing URL (flat sale)")
    parser.add_argument("--delay-ms", type=int, default=300, help="Delay between webhook requests (ms)")
    args = parser.parse_args()

    offer_id = _extract_offer_id(args.offer_url)
    core = load_core_settings()
    store = StateStore(core.state_db_path)
    st = store.get(offer_id)
    if not st or not st.photos:
        raise RuntimeError(f"Offer {offer_id} not found in state.db (run search first) or has no photos.")

    webhook = N8NPhotoScoringClient()

    print(f"Offer {offer_id} photos={len(st.photos)} url={st.cian_url}")
    rows = []
    worst_interior = None  # (score, url)

    for idx, photo_url in enumerate(st.photos):
        if args.delay_ms > 0:
            time.sleep(args.delay_ms / 1000.0)
        try:
            res = webhook.score(offer_id=offer_id, idx=idx, photo_url=photo_url)
            res = validate_photo_score(res)
            ptype = str(res.get("photo_type") or "").strip().lower()
            score = int(res["improvement_score_0to100"])
            store.upsert_photo_analysis(
                offer_id=offer_id,
                photo_url=photo_url,
                idx=idx,
                category=ptype or None,
                score=score,
                raw_json=str(res),
            )
            rows.append((idx, ptype, score, photo_url))
            if ptype == "interior":
                if worst_interior is None or score < worst_interior[0]:
                    worst_interior = (score, photo_url)
            print(f"[{idx}] {ptype} score={score} {photo_url}")
        except Exception as e:
            print(f"[{idx}] error: {e}")
            continue

    print("\n=== RESULT (all photos) ===")
    print(f"{'idx':>3}  {'type':>9}  {'score':>5}  photo_url")
    print("-" * 90)
    for idx, ptype, score, photo_url in rows:
        print(f"{idx:>3}  {ptype:>9}  {score:>5}  {photo_url}")

    if worst_interior:
        print("\n=== WORST INTERIOR ===")
        print(f"score={worst_interior[0]} url={worst_interior[1]}")
    else:
        print("\nNo interior photos detected.")

    store.close()


if __name__ == "__main__":
    main()


