"""Tolerant parsing of numbers that LLMs return as text ("Rs. 30,000", "70%", "50k")."""
import re

_UNITS = {"k": 1_000, "thousand": 1_000, "lakh": 100_000, "lac": 100_000, "lakhs": 100_000,
          "cr": 10_000_000, "crore": 10_000_000, "crores": 10_000_000}


def to_number(value, default=None):
    if isinstance(value, bool):
        return default
    if isinstance(value, (int, float)):
        return value
    if not isinstance(value, str):
        return default
    m = re.search(r"(-?\d[\d,]*(?:\.\d+)?)\s*(k|thousand|lakhs?|lac|cr|crores?)?\b", value, re.IGNORECASE)
    if not m:
        return default
    number = float(m.group(1).replace(",", ""))
    unit = (m.group(2) or "").lower()
    return number * _UNITS.get(unit, 1)
