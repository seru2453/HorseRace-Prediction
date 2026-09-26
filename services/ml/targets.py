from __future__ import annotations


def relative_finish(finish_position: int | None, field_size: int | None) -> float | None:
    """Return a field-size-adjusted finish score without inventing missing outcomes."""
    if finish_position is None or field_size is None:
        return None
    finish = int(finish_position)
    size = int(field_size)
    if size <= 1 or finish < 1 or finish > size:
        return None
    return 1.0 - ((finish - 1) / (size - 1))


def relative_finish_category(value: float | None) -> str | None:
    if value is None:
        return None
    if value >= 0.75:
        return "GOOD"
    if value >= 0.50:
        return "FAIR"
    return "POOR"
