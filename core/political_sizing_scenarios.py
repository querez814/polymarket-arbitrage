"""Immutable read-only sizing policies for political-paper evidence.

These definitions deliberately contain no scorer, adapter, or ledger calls.
One sealed decision may later be fanned out to these policies, but a larger
scenario can never create a different signal or change control accounting.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from decimal import Decimal


@dataclass(frozen=True)
class PoliticalSizingScenario:
    """One full-policy, read-only sizing interpretation of common evidence."""

    name: str
    position_cap: Decimal | None
    total_reserved_cap: Decimal | None
    max_open_positions: int | None
    risk_label: str
    risk_group_reserved_cap: Decimal | None = None
    max_open_positions_per_risk_group: int | None = None
    deployable: bool = False

    def __post_init__(self) -> None:
        if not self.name or not self.risk_label:
            raise ValueError("political sizing scenario identity is required")
        for value in (
            self.position_cap,
            self.total_reserved_cap,
            self.risk_group_reserved_cap,
        ):
            if value is not None and (not value.is_finite() or value <= 0):
                raise ValueError("political sizing caps must be finite and positive")
        if self.max_open_positions is not None and self.max_open_positions <= 0:
            raise ValueError("political sizing max_open_positions must be positive")
        if (
            self.max_open_positions_per_risk_group is not None
            and self.max_open_positions_per_risk_group <= 0
        ):
            raise ValueError(
                "political sizing max_open_positions_per_risk_group must be positive"
            )
        if self.deployable:
            raise ValueError("political sizing scenarios must remain read-only")

    def canonical_policy(self) -> dict[str, str | int | None]:
        """Return a stable complete policy payload suitable for scenario identity."""
        return {
            "name": self.name,
            "position_cap": (
                None if self.position_cap is None else str(self.position_cap)
            ),
            "total_reserved_cap": (
                None
                if self.total_reserved_cap is None
                else str(self.total_reserved_cap)
            ),
            "max_open_positions": self.max_open_positions,
            "risk_group_reserved_cap": (
                None
                if self.risk_group_reserved_cap is None
                else str(self.risk_group_reserved_cap)
            ),
            "max_open_positions_per_risk_group": self.max_open_positions_per_risk_group,
            "risk_label": self.risk_label,
            "read_only_not_realized": True,
        }

    def scenario_id(self, *, evidence_cohort_id: str) -> str:
        """Derive a full-policy scenario identity without changing evidence identity."""
        if not evidence_cohort_id.strip():
            raise ValueError("evidence_cohort_id must be non-empty")
        encoded = json.dumps(
            {
                "evidence_cohort_id": evidence_cohort_id,
                "policy": self.canonical_policy(),
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return f"scenario:{self.name}:{hashlib.sha256(encoded).hexdigest()}"


def required_political_sizing_scenarios() -> tuple[PoliticalSizingScenario, ...]:
    """Return exactly the seven approved non-mutating sizing scenarios."""
    return (
        PoliticalSizingScenario(
            "control_p25_t100",
            Decimal("25"),
            Decimal("100"),
            4,
            "control",
            Decimal("50"),
            2,
        ),
        PoliticalSizingScenario(
            "cf_p50_t100",
            Decimal("50"),
            Decimal("100"),
            4,
            "counterfactual",
            Decimal("50"),
            2,
        ),
        PoliticalSizingScenario(
            "cf_p50_t200",
            Decimal("50"),
            Decimal("200"),
            4,
            "counterfactual",
            Decimal("100"),
            2,
        ),
        PoliticalSizingScenario(
            "cf_p100_t400",
            Decimal("100"),
            Decimal("400"),
            4,
            "counterfactual",
            Decimal("200"),
            2,
        ),
        PoliticalSizingScenario(
            "cf_p150_t600",
            Decimal("150"),
            Decimal("600"),
            4,
            "high_risk_counterfactual",
            Decimal("300"),
            2,
        ),
        PoliticalSizingScenario(
            "cf_p200_t800",
            Decimal("200"),
            Decimal("800"),
            4,
            "high_risk_counterfactual",
            Decimal("400"),
            2,
        ),
        PoliticalSizingScenario(
            "cf_liquidity_ceiling", None, None, None, "capacity_only", None, 2
        ),
    )
