from decimal import Decimal, InvalidOperation, ROUND_HALF_UP


ACCESSORY_TYPES = ("tie", "shirt", "belt", "watch", "shoes")
ALWAYS_CHARGED = ("shoes",)
CENT = Decimal("0.01")


def _money(value):
    try:
        amount = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ValueError("Enter a valid monetary amount.") from exc
    if not amount.is_finite() or amount < 0:
        raise ValueError("Amounts must be finite and non-negative.")
    return amount.quantize(CENT, rounding=ROUND_HALF_UP)


def calculate_booking(
    suit_price,
    suit_qty,
    days,
    has_suit,
    prices,
    guarantees,
    discount_type="percent",
    discount_value=0,
):
    days = int(days)
    suit_qty = int(suit_qty)
    if not 1 <= days <= 365:
        raise ValueError("Rental duration must be between 1 and 365 days.")
    if suit_qty < 1:
        raise ValueError("Suit quantity must be at least one.")
    if discount_type not in {"percent", "amount"}:
        raise ValueError("Select a valid discount type.")

    quantity_multiplier = suit_qty if has_suit else 1
    suit_total = (
        _money(suit_price) * suit_qty * days
        if has_suit
        else Decimal("0.00")
    )

    accessory_total = Decimal("0.00")
    for item, price in prices.items():
        if has_suit and item not in ALWAYS_CHARGED:
            continue
        accessory_total += _money(price) * quantity_multiplier * days

    subtotal = (suit_total + accessory_total).quantize(CENT, rounding=ROUND_HALF_UP)
    value = _money(discount_value)
    if discount_type == "percent":
        if value > 100:
            raise ValueError("Percentage discount cannot exceed 100%.")
        discount = subtotal * value / Decimal("100")
    else:
        discount = min(value, subtotal)
    discount = discount.quantize(CENT, rounding=ROUND_HALF_UP)

    guarantee_total = sum(
        (_money(value) for value in guarantees.values()),
        Decimal("0.00"),
    )
    rental_total = subtotal - discount
    return {
        "days": days,
        "subtotal": subtotal,
        "discount": discount,
        "rental_total": rental_total,
        "guarantee_total": guarantee_total,
        "total_due": rental_total + guarantee_total,
    }


def settle_guarantee(guarantee_held, damage_fee):
    held = _money(guarantee_held)
    damage = _money(damage_fee)
    kept = min(held, damage)
    return {
        "refund": held - kept,
        "kept": kept,
        "uncovered_damage": damage - kept,
    }
