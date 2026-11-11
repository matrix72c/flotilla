"""Small, deterministic resource normalization shared by task parsing and configuration."""

import re
from decimal import Decimal, InvalidOperation


def cpu(value: object) -> str:
    try:
        number = Decimal(str(value))
    except InvalidOperation as exc:
        raise ValueError("CPU must be a positive number") from exc
    if not number.is_finite() or number <= 0:
        raise ValueError("CPU must be a positive finite number")
    return str(number.normalize())


def memory(value: object) -> str:
    match = re.fullmatch(r"([0-9]+(?:\.[0-9]+)?)([kmgtKMGT](?:[iI]?[bB]?)?)?", str(value))
    if match is None or Decimal(match[1]) <= 0:
        raise ValueError("memory must be a positive byte count or K/M/G/T quantity")
    suffix = (match[2] or "").upper()
    # Compose k/m/g are binary units; emit an unambiguous backend quantity.
    return match[1] + (suffix[0] + "i" if suffix else "")
