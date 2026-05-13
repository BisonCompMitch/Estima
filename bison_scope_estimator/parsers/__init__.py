"""File parser dispatchers."""

from __future__ import annotations

from pathlib import Path

from ..models import MeasurementTotals
from .dxf_parser import parse_dxf
from .ifc_parser import parse_ifc


def parse_file_measurements(
    path: Path,
    source_length_unit: str | None = None,
    strict_mode: bool = False,
) -> MeasurementTotals:
    """Parse a DXF or IFC file and return normalized measurement totals."""
    ext = path.suffix.lower()
    if ext == ".dxf":
        # Pass None so the parser can auto-detect $INSUNITS; it falls back to "ft" internally.
        return parse_dxf(path, source_length_unit or None, strict_mode=strict_mode)
    if ext == ".ifc":
        return parse_ifc(path, source_length_unit, strict_mode=strict_mode)
    raise ValueError(f"Unsupported file type '{path.suffix}'. Use a .dxf or .ifc file.")
