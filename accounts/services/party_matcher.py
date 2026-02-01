from re import sub


def normalize_phone(phone: str | None) -> str | None:
    """
    Normalize phone numbers for safe comparison.

    Examples:
    +92-300-1234567 → 923001234567
    0300 123 4567   → 03001234567
    """
    if not phone:
        return None

    # keep digits only
    return sub(r"\D", "", phone)