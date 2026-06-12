"""Validation helpers for ReconciliationGroup invariants.

The core invariant: members' allocated_amount_base should net to zero
(within tolerance) for a balanced group.  When allocated_amount_base is
missing on a member, the system converts via FX as of the group's
as_of_date and flags staleness explicitly.
"""
from dataclasses import dataclass, field
from datetime import datetime
from app.services.clock import naive_utc_now
from decimal import Decimal

from sqlalchemy.orm import Session

from app.models.enums import FeeTreatment, FxTreatmentMode
from app.models.reconciliation import ReconciliationGroup, ReconciliationMember
from app.services.fx_service import convert_amount


@dataclass
class InvariantResult:
    balanced: bool = False
    net_base: Decimal = Decimal("0.00")
    tolerance: Decimal = Decimal("0.01")
    members_converted: int = 0
    members_missing_base: int = 0
    fx_stale_members: list[int] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


def validate_group(
    db: Session,
    group: ReconciliationGroup,
) -> InvariantResult:
    """Check whether the group's members net to zero in base currency."""
    members: list[ReconciliationMember] = group.members
    tolerance = group.tolerance_base or Decimal("0.01")
    base_ccy = group.base_currency or "USD"
    fx_treatment = group.fx_treatment or FxTreatmentMode.NONE.value
    fee_treatment = group.fee_treatment or FeeTreatment.EXCLUDE_FROM_NET.value
    as_of = group.as_of_date or naive_utc_now()

    result = InvariantResult(tolerance=tolerance)
    net = Decimal("0.00")

    for member in members:
        if fee_treatment == FeeTreatment.EXCLUDE_FROM_NET.value and member.is_fee_leg:
            continue

        if member.allocated_amount_base is not None:
            net += member.allocated_amount_base
            result.members_converted += 1
        elif member.allocated_currency == base_ccy:
            net += member.allocated_amount_native
            result.members_converted += 1
        else:
            if fx_treatment in (
                FxTreatmentMode.SPOT_ON_GROUP_DATE.value,
                FxTreatmentMode.MEMBER_RATES.value,
            ):
                converted, _ = convert_amount(
                    db,
                    member.allocated_amount_native,
                    member.allocated_currency,
                    base_ccy,
                    as_of,
                )
                if converted is not None:
                    net += converted
                    result.members_converted += 1
                else:
                    result.members_missing_base += 1
                    result.fx_stale_members.append(member.id)
                    result.warnings.append(
                        f"Member {member.id}: no FX rate for "
                        f"{member.allocated_currency}/{base_ccy} "
                        f"as of {as_of.date()}"
                    )
            else:
                result.members_missing_base += 1
                result.warnings.append(
                    f"Member {member.id}: allocated_amount_base is NULL "
                    f"and FX treatment is {fx_treatment}"
                )

    result.net_base = net.quantize(Decimal("0.000001"))
    result.balanced = abs(result.net_base) <= tolerance

    if not result.balanced:
        result.warnings.append(
            f"Group net {result.net_base} exceeds tolerance {tolerance}"
        )

    return result
