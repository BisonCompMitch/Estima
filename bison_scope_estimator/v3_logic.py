"""V3-specific estimation logic with strict mode, confidence reporting, and estimate ranges."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .app_logic import validate_cost_inputs
from .calculator import BudgetComparison, build_budget_comparison
from .models import MeasurementTotals
from .parsers import parse_file_measurements
from .v3_assessment import ConfidenceReport, assess_measurement_confidence

# Uncertainty band (fraction) applied symmetrically around the overall estimate.
_CONFIDENCE_UNCERTAINTY: dict[str, float] = {
    "high": 0.08,    # ±8 %
    "medium": 0.18,  # ±18 %
    "low": 0.38,     # ±38 %
}

# Confidence → numeric score used for weighted blending.
_CONFIDENCE_SCORES: dict[str, float] = {
    "low": 0.30,
    "medium": 0.65,
    "high": 1.00,
}

# When one side has substantially higher confidence, tilt the blend toward it.
_BLEND_TILT_THRESHOLD = 0.25   # score difference that triggers tilting
_BLEND_TILT_FACTOR = 0.15      # extra weight shifted to the stronger side


@dataclass(frozen=True)
class V3EstimateResult:
    measurements: MeasurementTotals
    budget: BudgetComparison
    confidence: ConfidenceReport
    strict_mode: bool
    overall_cost: float
    overall_basis: str
    overall_area_weight: float
    overall_linear_weight: float
    estimate_low: float
    estimate_high: float


def _confidence_score(level: str) -> float:
    return float(_CONFIDENCE_SCORES.get(level, 0.65))


def _build_overall_estimate(
    confidence: ConfidenceReport,
    area_cost: float,
    linear_cost: float,
) -> tuple[float, str, float, float]:
    area_score = _confidence_score(confidence.area_confidence)
    linear_score = _confidence_score(confidence.linear_confidence)
    total = area_score + linear_score

    if total <= 0.0:
        return (area_cost + linear_cost) * 0.5, "equal-weight average (no confidence)", 0.5, 0.5

    area_weight = area_score / total
    linear_weight = linear_score / total

    # Tilt blend further toward the more reliable side when the gap is significant.
    score_gap = abs(area_score - linear_score)
    if score_gap >= _BLEND_TILT_THRESHOLD:
        if area_score > linear_score:
            area_weight = min(area_weight + _BLEND_TILT_FACTOR, 0.90)
            linear_weight = 1.0 - area_weight
        else:
            linear_weight = min(linear_weight + _BLEND_TILT_FACTOR, 0.90)
            area_weight = 1.0 - linear_weight

    overall_cost = (area_cost * area_weight) + (linear_cost * linear_weight)
    basis = (
        f"confidence-weighted blend "
        f"({confidence.area_confidence}/{confidence.linear_confidence}; "
        f"area {area_weight:.0%}, linear {linear_weight:.0%})"
    )
    return overall_cost, basis, area_weight, linear_weight


def _compute_estimate_range(overall_cost: float, overall_confidence: str) -> tuple[float, float]:
    """Return (low, high) uncertainty bounds around the overall estimate."""
    band = _CONFIDENCE_UNCERTAINTY.get(overall_confidence, 0.20)
    return overall_cost * (1.0 - band), overall_cost * (1.0 + band)


def estimate_from_file_v3(
    file_path: Path,
    cost_per_sqft: float,
    cost_per_linear_ft: float,
    source_length_unit: str | None = None,
    strict_mode: bool = False,
) -> V3EstimateResult:
    validate_cost_inputs(cost_per_sqft, cost_per_linear_ft)
    measurements = parse_file_measurements(
        file_path,
        source_length_unit=source_length_unit,
        strict_mode=strict_mode,
    )
    budget = build_budget_comparison(
        area_sqft=measurements.area_sqft,
        linear_ft=measurements.linear_ft,
        cost_per_sqft=cost_per_sqft,
        cost_per_linear_ft=cost_per_linear_ft,
    )
    confidence = assess_measurement_confidence(measurements, strict_mode=strict_mode)
    overall_cost, overall_basis, area_weight, linear_weight = _build_overall_estimate(
        confidence,
        budget.area_cost,
        budget.linear_cost,
    )
    estimate_low, estimate_high = _compute_estimate_range(overall_cost, confidence.overall_confidence)

    return V3EstimateResult(
        measurements=measurements,
        budget=budget,
        confidence=confidence,
        strict_mode=strict_mode,
        overall_cost=overall_cost,
        overall_basis=overall_basis,
        overall_area_weight=area_weight,
        overall_linear_weight=linear_weight,
        estimate_low=estimate_low,
        estimate_high=estimate_high,
    )
