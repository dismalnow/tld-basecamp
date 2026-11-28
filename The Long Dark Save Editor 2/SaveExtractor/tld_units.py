"""US volume and weight for the save listings."""

from decimal import ROUND_DOWN, Decimal, localcontext

LITERS_PER_US_GALLON = Decimal("3.785411784")
KG_PER_POUND = Decimal("0.45359237")


def us_amount(amount: Decimal, metric_per_unit: Decimal, scale: int = 1) -> Decimal:
    """Convert to US units and cut off after two decimals. Use a scale of a billion for stored amounts."""
    with localcontext() as context:
        # Keep every stored digit when scaling, and allow room for the whole part and two decimals.
        context.prec = max(len(amount.as_tuple().digits), amount.adjusted() + 4)
        context.rounding = ROUND_DOWN
        return (amount / scale / metric_per_unit).quantize(Decimal("0.01"))
