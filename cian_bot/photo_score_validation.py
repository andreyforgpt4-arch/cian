from __future__ import annotations

from typing import Any, Dict


def _require(cond: bool, msg: str) -> None:
    if not cond:
        raise ValueError(msg)


def _require_int_range(v: Any, lo: int, hi: int, name: str) -> int:
    _require(isinstance(v, int), f"{name} must be int")
    _require(lo <= v <= hi, f"{name} must be in range {lo}..{hi}")
    return v


def validate_photo_score(obj: Dict[str, Any]) -> Dict[str, Any]:
    """
    Validate the strict schema we expect from OpenAI. Returns the same dict if valid.
    Raises ValueError on mismatch.
    """
    _require(isinstance(obj, dict), "root must be object")

    photo_type = obj.get("photo_type")
    _require(
        photo_type
        in {
            # Room types (target for processing):
            "room_living_with_renovation",
            "room_living_without_renovation",
            "kitchen",
            "bathroom",
            "corridor",
            # Other types:
            "entrance",
            "entrance_outside",
            "building",
            "street",
            "floorplan",
            "other",
            # Backward compat (old schema):
            "interior",
            "interior_with_renovation",
            "interior_without_renovation",
            "exterior",
        },
        "photo_type invalid",
    )

    # shot_scope is optional but if present must be valid enum
    shot_scope = obj.get("shot_scope")
    if shot_scope is not None:
        _require(shot_scope in {"wide_overall", "detail_element"}, "shot_scope invalid")

    score = _require_int_range(obj.get("improvement_score_0to100"), 0, 100, "improvement_score_0to100")

    sb = obj.get("severity_breakdown")
    _require(isinstance(sb, dict), "severity_breakdown must be object")

    _require_int_range(sb.get("lighting_exposure_0to3"), 0, 3, "lighting_exposure_0to3")
    _require_int_range(sb.get("white_balance_0to2"), 0, 2, "white_balance_0to2")
    _require_int_range(sb.get("sharpness_blur_0to3"), 0, 3, "sharpness_blur_0to3")
    _require_int_range(sb.get("noise_compression_0to2"), 0, 2, "noise_compression_0to2")
    _require_int_range(sb.get("perspective_verticals_0to3"), 0, 3, "perspective_verticals_0to3")
    _require_int_range(sb.get("framing_composition_0to2"), 0, 2, "framing_composition_0to2")
    _require_int_range(
        sb.get("distractions_clutter_UI_privacy_0to3"),
        0,
        3,
        "distractions_clutter_UI_privacy_0to3",
    )

    _require(isinstance(obj.get("top_issues"), list), "top_issues must be array")
    _require(all(isinstance(x, str) for x in obj["top_issues"]), "top_issues items must be strings")

    _require(obj.get("should_improve") in {"yes", "maybe", "no"}, "should_improve invalid")

    _require(isinstance(obj.get("quick_win_fixes"), list), "quick_win_fixes must be array")
    _require(all(isinstance(x, str) for x in obj["quick_win_fixes"]), "quick_win_fixes items must be strings")

    _require(isinstance(obj.get("risk_notes"), list), "risk_notes must be array")
    _require(all(isinstance(x, str) for x in obj["risk_notes"]), "risk_notes items must be strings")

    # Note: do NOT enforce should_improve mapping strictly here.
    # Different backends (n8n/LLM) can be inconsistent; we only require enum validity above.

    return obj


