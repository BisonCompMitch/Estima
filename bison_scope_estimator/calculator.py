"""Budget calculation logic."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class BudgetComparison:
    """Cost outputs from the three estimate methods."""

    area_cost: float
    linear_cost: float
    combined_cost: float


def build_budget_comparison(
    area_sqft: float,
    linear_ft: float,
    cost_per_sqft: float,
    cost_per_linear_ft: float,
) -> BudgetComparison:
    """Compute area-only, linear-only, and combined estimates."""
    area_cost = float(area_sqft) * float(cost_per_sqft)
    linear_cost = float(linear_ft) * 1.0125 * float(cost_per_linear_ft)
    return BudgetComparison(
        area_cost=area_cost,
        linear_cost=linear_cost,
        combined_cost=area_cost + linear_cost,
    )


def format_currency(value: float) -> str:
    """Display a number as USD-like currency."""
    return f"${value:,.2f}"
