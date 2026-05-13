"""Confidence assessment helpers for BisonScope V3."""

from __future__ import annotations

from dataclasses import dataclass

from .models import MeasurementTotals

_RANK = {"low": 0, "medium": 1, "high": 2}
_RANK_TO_LABEL = {0: "low", 1: "medium", 2: "high"}

# Minimum element count before element-count confidence boost kicks in.
_ELEMENT_COUNT_MEDIUM_THRESHOLD = 5
_ELEMENT_COUNT_HIGH_THRESHOLD = 20


@dataclass(frozen=True)
class ConfidenceReport:
    area_confidence: str
    linear_confidence: str
    overall_confidence: str
    warnings: tuple[str, ...]

    def summary(self) -> str:
        return (
            f"Area confidence: {self.area_confidence} | "
            f"Linear confidence: {self.linear_confidence} | "
            f"Overall: {self.overall_confidence}"
        )


def assess_measurement_confidence(measurements: MeasurementTotals, strict_mode: bool = False) -> ConfidenceReport:
    area_confidence, area_warnings = _classify_area(measurements)
    linear_confidence, linear_warnings = _classify_linear(measurements)

    # Element count can boost linear confidence when many members were found.
    linear_confidence = _apply_element_count_adjustment(
        linear_confidence, measurements.framing_element_count
    )

    overall_confidence = _lowest(area_confidence, linear_confidence)

    warnings = list(area_warnings)
    warnings.extend(linear_warnings)
    if strict_mode and overall_confidence != "high":
        warnings.append("Strict mode passed, but one or more measurements are still not fully authored.")

    return ConfidenceReport(
        area_confidence=area_confidence,
        linear_confidence=linear_confidence,
        overall_confidence=overall_confidence,
        warnings=tuple(dict.fromkeys(warnings)),
    )


def _lowest(*levels: str) -> str:
    lowest_rank = min(_RANK.get(level, 1) for level in levels)
    return _RANK_TO_LABEL[lowest_rank]


def _apply_element_count_adjustment(confidence: str, element_count: int) -> str:
    """Boost medium→high when many framing elements were found (corroborates the geometry source)."""
    if confidence == "low":
        return confidence  # count can't rescue a low-confidence source
    if confidence == "medium" and element_count >= _ELEMENT_COUNT_HIGH_THRESHOLD:
        return "high"
    return confidence


def _classify_area(measurements: MeasurementTotals) -> tuple[str, list[str]]:
    basis = measurements.area_basis.lower()
    warnings: list[str] = []

    if "no-floor-area" in basis:
        warnings.append("No floor area data was found; estimate is unreliable.")
        return "low", warnings

    # Authoritative authored quantity sources.
    if any(token in basis for token in (
        "ifcspace-floorarea-quantities",
        "ifcbuildingstorey-or-building-floorarea-quantities",
        "ifcslab-floorarea-quantities",
    )):
        return "high", warnings

    # Area pset/QTO fallback — still authored, slightly less specific.
    if "area-quantities-fallback" in basis:
        warnings.append("Generic IFC area quantity used as floor-area fallback; consider adding explicit GrossFloorArea.")
        return "medium", warnings

    # IfcSpace geometry projection.
    if "ifcspace-geometry-footprint" in basis:
        warnings.append("IfcSpace geometry was projected to derive floor area — no authored quantity found.")
        return "medium", warnings

    # DXF closed geometry and 3DFACE areas.
    if "dxf-closed-geometry-and-3dface-area" in basis:
        if measurements.area_sqft > 0.0:
            return "medium", warnings
        warnings.append("DXF area sources found but total is zero; check geometry.")
        return "low", warnings

    # Shell / exterior footprint fallback.
    if "ifcshell-geometry-footprint" in basis:
        warnings.append("Exterior shell geometry was projected to derive floor area — least-reliable fallback.")
        return "low", warnings

    # Framing footprint fallback.
    if "framing-geometry-footprint" in basis:
        warnings.append("Floor area derived from framing footprint — highly approximate.")
        return "low", warnings

    if "quantity" in basis:
        return "high", warnings

    if "geometry" in basis or "fallback" in basis:
        warnings.append("Floor area uses a geometry-based fallback.")
        return "medium", warnings

    return "medium", warnings


def _classify_linear(measurements: MeasurementTotals) -> tuple[str, list[str]]:
    basis = measurements.linear_basis.lower()
    warnings: list[str] = []

    if "no-cfs-framing-found" in basis:
        warnings.append("No CFS framing elements were identified; linear estimate is zero.")
        return "low", warnings

    if "all-geometry-fallback" in basis:
        warnings.append("Framing length fell back to all geometry — not CFS-specific.")
        return "low", warnings

    # Pset_MemberCommon or QTO authored quantities — highest confidence.
    if "cfs-framing-element-quantities" in basis or "framing-element-quantities" in basis:
        return "high", warnings

    # DXF layer-based framing geometry — reasonable but inferred.
    if "cfs-framing-layer-geometry" in basis or "framing-layer-geometry" in basis:
        warnings.append("DXF framing length inferred from CFS-like layer names — verify layer naming.")
        return "medium", warnings

    # IFC geometry fallback alongside quantities.
    if "cfs-framing-element-geometry-or-quantities" in basis or "framing-element-geometry-or-quantities" in basis:
        warnings.append("Some IFC framing lengths are geometry-derived (no authored quantity on those members).")
        return "medium", warnings

    if "quantity" in basis:
        return "high", warnings

    if "geometry" in basis or "fallback" in basis:
        warnings.append("Framing length uses a geometry-based fallback.")
        return "medium", warnings

    return "medium", warnings
