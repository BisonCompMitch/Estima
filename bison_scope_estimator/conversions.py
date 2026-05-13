"""Unit conversion helpers."""

from __future__ import annotations

from typing import Final

_UNIT_ALIASES: Final[dict[str, str]] = {
    "ft": "ft",
    "foot": "ft",
    "feet": "ft",
    "m": "m",
    "meter": "m",
    "metre": "m",
    "meters": "m",
    "metres": "m",
    "in": "in",
    "inch": "in",
    "inches": "in",
    "mm": "mm",
    "millimeter": "mm",
    "millimetre": "mm",
    "millimeters": "mm",
    "millimetres": "mm",
    "cm": "cm",
    "centimeter": "cm",
    "centimetre": "cm",
    "centimeters": "cm",
    "centimetres": "cm",
    "yd": "yd",
    "yard": "yd",
    "yards": "yd",
}

_LENGTH_TO_FEET: Final[dict[str, float]] = {
    "ft": 1.0,
    "m": 3.28083989501312,
    "in": 1.0 / 12.0,
    "mm": 0.00328083989501312,
    "cm": 0.0328083989501312,
    "yd": 3.0,
}


def normalize_length_unit(unit: str) -> str:
    """Normalize user input to supported short unit names."""
    normalized = unit.strip().lower()
    try:
        return _UNIT_ALIASES[normalized]
    except KeyError as exc:
        supported = ", ".join(sorted(_LENGTH_TO_FEET))
        raise ValueError(f"Unsupported length unit '{unit}'. Supported: {supported}") from exc


def length_to_feet(value: float, unit: str) -> float:
    """Convert a length value from source unit to feet."""
    canonical = normalize_length_unit(unit)
    return float(value) * _LENGTH_TO_FEET[canonical]


def area_to_square_feet(value: float, length_unit: str) -> float:
    """Convert area from (length_unit^2) to square feet."""
    factor = _LENGTH_TO_FEET[normalize_length_unit(length_unit)]
    return float(value) * (factor ** 2)
