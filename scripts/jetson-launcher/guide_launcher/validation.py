"""Public command validation and explicit wire defaults."""

import math


def object_fields(body, allowed):
    """Reject nonobjects and fields outside the published contract."""
    if not isinstance(body, dict) or set(body) - set(allowed):
        raise ValueError("Unknown fields or expected object")


def text(value, name, maximum=128):
    """Require bounded nonblank text."""
    if not isinstance(value, str) or not value.strip() or len(value) > maximum:
        raise ValueError(f"Invalid {name}")
    return value


def number(value, name, low, high):
    """Require a finite bounded number, excluding booleans."""
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(value)
        or not low <= value <= high
    ):
        raise ValueError(f"Invalid {name}")
    return value


def mission(body):
    """Normalize mutually exclusive route selectors and explicit goal defaults."""
    defaults = {
        "start_index": 0,
        "time_budget_min": 0,
        "greet": True,
        "narrate": True,
        "confirm_between_stops": False,
        "return_home": True,
    }
    object_fields(body, [*defaults, "tour_id", "location_ids"])
    if ("tour_id" in body) == ("location_ids" in body):
        raise ValueError("Specify exactly one of tour_id or location_ids")
    result = {**defaults, **body}
    if "tour_id" in body:
        text(body["tour_id"], "tour_id")
    else:
        ids = body["location_ids"]
        if not isinstance(ids, list) or not 1 <= len(ids) <= 100:
            raise ValueError("Invalid location_ids")
        for value in ids:
            text(value, "location_id")
    for key in ("start_index", "time_budget_min"):
        number(result[key], key, 0, 65535 if key == "start_index" else 1440)
        if not isinstance(result[key], int):
            raise ValueError(f"Invalid {key}")
    for key in ("greet", "narrate", "confirm_between_stops", "return_home"):
        if not isinstance(result[key], bool):
            raise ValueError(f"Invalid {key}")
    return result


def speech(body):
    """Normalize an interruptible dialog-scoped speech goal."""
    object_fields(body, ("text", "voice", "max_duration"))
    text(body.get("text"), "text", 10000)
    if not isinstance(body.get("voice", ""), str):
        raise ValueError("Invalid voice")
    duration = number(body.get("max_duration", 120), "max_duration", 1, 300)
    return {
        "text": body["text"],
        "voice": body.get("voice", ""),
        "max_duration": float(duration),
        "priority": 100,
        "scope": 2,
        "interruptible": True,
    }


def pose(body):
    """Validate an explicit pose tied to the map selected by the client."""
    object_fields(body, ("map_version", "pose"))
    text(body.get("map_version"), "map_version")
    value = body.get("pose")
    object_fields(value, ("x", "y", "yaw"))
    for key in ("x", "y", "yaw"):
        number(value.get(key), key, -1e6, 1e6)
    return body
