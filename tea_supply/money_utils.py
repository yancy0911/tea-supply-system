"""金额工具：统一两位小数，计算用 Decimal，落库仍兼容 FloatField。"""
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

MONEY_QUANT = Decimal("0.01")


def money_dec(value) -> Decimal:
    if value is None:
        return Decimal("0")
    if isinstance(value, Decimal):
        return value
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        return Decimal("0")


def money_q2(value) -> Decimal:
    return money_dec(value).quantize(MONEY_QUANT, rounding=ROUND_HALF_UP)


def money_float(value) -> float:
    return float(money_q2(value))
