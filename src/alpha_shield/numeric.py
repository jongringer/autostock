"""Decimal 정밀도 강제.

이 프로그램의 수량·금액·가격은 전 구간 Decimal 이다.
float 은 설정 파일(YAML) 파싱 지점에서만 나타나며 `dec()` 가 str 경유로 흡수한다.
`int()`/`float()` 캐스팅으로 소수점 잔고를 잃지 않도록 주의한다.
"""

from __future__ import annotations

from decimal import Decimal, InvalidOperation, ROUND_DOWN, ROUND_HALF_UP, getcontext

# 소수점 6자리 수량 x 가격 곱셈에서도 정밀도가 남도록 넉넉히 둔다.
getcontext().prec = 34

ZERO = Decimal("0")
ONE = Decimal("1")
HUNDRED = Decimal("100")

_Q6 = Decimal("0.000001")
_Q4 = Decimal("0.0001")
_Q2 = Decimal("0.01")


class DecimalConversionError(ValueError):
    """숫자로 해석할 수 없는 값."""


def dec(value: object) -> Decimal:
    """object -> Decimal. float 은 str 을 거쳐 2진 오차를 흡수한다."""
    if isinstance(value, Decimal):
        return value
    if isinstance(value, bool):
        raise DecimalConversionError("bool is not a number")
    if isinstance(value, (int, float, str)):
        try:
            return Decimal(str(value))
        except InvalidOperation as exc:  # pragma: no cover - 방어
            raise DecimalConversionError(f"cannot convert {value!r}") from exc
    raise DecimalConversionError(f"cannot convert {type(value).__name__}")


def opt_dec(value: object | None) -> Decimal | None:
    return None if value is None or value == "" else dec(value)


def round_to(value: Decimal, places: int) -> Decimal:
    quantum = ONE.scaleb(-places)
    return value.quantize(quantum, rounding=ROUND_HALF_UP)


def money(value: Decimal) -> Decimal:
    """표시·원장용 금액 (소수 2자리, 반올림)."""
    return value.quantize(_Q2, rounding=ROUND_HALF_UP)


def money_down(value: Decimal) -> Decimal:
    """주문 금액 상한 (소수 2자리, 내림 — 초과 주문 방지)."""
    return value.quantize(_Q2, rounding=ROUND_DOWN)


def qty6(value: Decimal) -> Decimal:
    """수량 소수 6자리 내림 (토스 소수점 수량 한도)."""
    return value.quantize(_Q6, rounding=ROUND_DOWN)


def pct4(value: Decimal) -> Decimal:
    return value.quantize(_Q4, rounding=ROUND_HALF_UP)


def pct_change(new: Decimal, old: Decimal) -> Decimal | None:
    """(new-old)/old*100. old 가 0 이면 None."""
    if old == ZERO:
        return None
    return (new - old) / old * HUNDRED


def safe_div(numerator: Decimal, denominator: Decimal) -> Decimal | None:
    if denominator == ZERO:
        return None
    return numerator / denominator


def clamp(value: Decimal, low: Decimal, high: Decimal) -> Decimal:
    return max(low, min(high, value))


def is_positive(value: Decimal | None) -> bool:
    return value is not None and value > ZERO
