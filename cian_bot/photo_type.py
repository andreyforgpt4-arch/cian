from __future__ import annotations


# Target room types that should be considered for processing
TARGET_ROOM_TYPES = {
    "room_living_with_renovation",
    "kitchen",
    "bathroom",
    "corridor",
}


def is_target_room_type(photo_type: str | None) -> bool:
    """
    Check if photo_type is one of the target room types that should be processed.
    Returns True for: room_living_with_renovation, kitchen, bathroom, corridor.
    """
    if not photo_type:
        return False
    t = photo_type.strip().lower()
    return t in TARGET_ROOM_TYPES


def is_interior(photo_type: str | None) -> bool:
    """
    Legacy function for backward compatibility.
    Now delegates to is_target_room_type.
    """
    if not photo_type:
        return False
    t = photo_type.strip().lower()
    # Backward compat: old interior types
    if t == "interior" or t.startswith("interior_"):
        return True
    return is_target_room_type(photo_type)


