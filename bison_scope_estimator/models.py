"""Core data structures."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class MeasurementTotals:
    """Normalized quantities extracted from a source file."""

    source_path: Path
    source_format: str
    source_unit: str
    area_sqft: float
    area_basis: str
    linear_ft: float
    linear_basis: str
    framing_element_count: int = 0
