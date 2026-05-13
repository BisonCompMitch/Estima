"""Shared estimation logic used by CLI and GUI entry points."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from .calculator import BudgetComparison, build_budget_comparison
from .models import MeasurementTotals
from .parsers import parse_file_measurements


def validate_cost_inputs(cost_per_sqft: float, cost_per_linear_ft: float) -> None:
    """Validate cost inputs before attempting estimation."""
    if cost_per_sqft < 0:
        raise ValueError("Cost per square foot must be non-negative.")
    if cost_per_linear_ft < 0:
        raise ValueError("Cost per linear foot must be non-negative.")


@lru_cache(maxsize=64)
def _parse_measurements_cached(
    resolved_path_str: str,
    mtime_ns: int,
    size_bytes: int,
    source_length_unit: str | None,
) -> MeasurementTotals:
    # mtime/size are part of the cache key to invalidate when file contents change.
    _ = (mtime_ns, size_bytes)
    return parse_file_measurements(Path(resolved_path_str), source_length_unit)


def get_measurements(file_path: Path, source_length_unit: str | None = None) -> MeasurementTotals:
    """Parse file measurements with in-process caching by file signature."""
    if not file_path.exists():
        raise FileNotFoundError(f"File not found: {file_path}")
    if not file_path.is_file():
        raise ValueError(f"Not a file: {file_path}")

    stat = file_path.stat()
    resolved = file_path.resolve()
    normalized_unit = source_length_unit.strip().lower() if source_length_unit else None
    return _parse_measurements_cached(
        str(resolved),
        int(getattr(stat, "st_mtime_ns", int(stat.st_mtime * 1_000_000_000))),
        int(stat.st_size),
        normalized_unit,
    )


def estimate_from_measurements(
    measurements: MeasurementTotals,
    cost_per_sqft: float,
    cost_per_linear_ft: float,
) -> BudgetComparison:
    """Compute budget outputs from already-parsed measurements."""
    validate_cost_inputs(cost_per_sqft, cost_per_linear_ft)
    return build_budget_comparison(
        area_sqft=measurements.area_sqft,
        linear_ft=measurements.linear_ft,
        cost_per_sqft=cost_per_sqft,
        cost_per_linear_ft=cost_per_linear_ft,
    )


def estimate_from_file(
    file_path: Path,
    cost_per_sqft: float,
    cost_per_linear_ft: float,
    source_length_unit: str | None = None,
) -> tuple[MeasurementTotals, BudgetComparison]:
    """Run parsing and budget calculation for a source model file."""
    measurements = get_measurements(file_path, source_length_unit)
    budget = estimate_from_measurements(
        measurements=measurements,
        cost_per_sqft=cost_per_sqft,
        cost_per_linear_ft=cost_per_linear_ft,
    )
    return measurements, budget
