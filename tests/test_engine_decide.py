from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from app.engine import Action, Mandate, decide
from app.paypal.models import LineItem, Money, usd

T0 = datetime(2026, 11, 9, 10, 0, tzinfo=timezone.utc)
HOLD = usd("60")


def mandate(**kw) -> Mandate:
    return Mandate(mandate_id="m1", customer_ref="agent-abc", created_at=T0, **kw)


def visit(price="60"):
    return LineItem(name="Plumber visit", unit_amount=usd(price), category="visit")


def extra(name, price, category="parts"):
    return LineItem(name=name, unit_amount=usd(price), category=category, added=True)


def run(items, m=None, **kw):
    return decide(HOLD, items, m or mandate(), now=T0, **kw)


def test_cancelled_voids_whole_hold():
    d = run([visit(), extra("Washer", "6")], cancelled=True)
    assert d.action == Action.VOID
    assert d.capture_on_hold.amount == 0 and d.total.amount == 0
    assert not d.needs_owner and not d.needs_customer


def test_zero_bill_voids():
    assert run([visit("0")]).action == Action.VOID


@pytest.mark.parametrize("price", ["40", "59.99", "60"])
def test_at_or_below_hold_captures_real_total(price):
    d = run([visit(price)])
    assert d.action == Action.CAPTURE_WITHIN
    assert d.capture_on_hold.amount == Decimal(price)
    assert d.supplemental.amount == 0


@pytest.mark.parametrize("extra_price, total", [("6", "66.00"), ("9", "69.00")])
def test_small_increase_up_to_cap_is_auto(extra_price, total):
    d = run([visit(), extra("Washer", extra_price)])
    assert d.action == Action.AUTO_CAPTURE
    assert d.capture_on_hold.amount == Decimal(total)
    assert not d.needs_owner and not d.needs_customer


def test_one_cent_over_cap_needs_approval():
    d = run([visit(), extra("Washer", "9.01")])
    assert d.action == Action.NEEDS_APPROVAL
    assert d.capture_on_hold.amount == Decimal("60.00")
    assert d.supplemental.amount == Decimal("9.01")
    assert d.needs_owner and d.needs_customer
    assert any("above PayPal's over-capture cap" in r for r in d.reasons)


def test_big_change_demo_case():
    d = run([visit(), extra("Washer", "6"), extra("Faucet cartridge", "45")])
    assert d.action == Action.NEEDS_APPROVAL
    assert (d.total.amount, d.capture_on_hold.amount, d.supplemental.amount) == (
        Decimal("111.00"), Decimal("60.00"), Decimal("51.00"))
    assert d.increase.amount == Decimal("51.00")


def test_customer_rule_stricter_than_paypal():
    m = mandate(max_auto_increase_amount="5")
    d = run([visit(), extra("Washer", "6")], m)
    assert d.action == Action.NEEDS_APPROVAL
    assert any("customer's auto-approve limit" in r for r in d.reasons)
    assert run([visit(), extra("Washer", "5")], m).action == Action.AUTO_CAPTURE


def test_always_ask_category_overrides_small_increase():
    m = mandate(always_ask_categories=["emergency"])
    d = run([visit(), extra("After-hours surcharge", "3", category="Emergency")], m)
    assert d.action == Action.NEEDS_APPROVAL
    assert any("always-ask" in r for r in d.reasons)


def test_always_ask_category_ignored_when_bill_not_higher():
    m = mandate(always_ask_categories=["parts"])
    d = run([visit("50"), extra("Washer", "6")], m)  # total 56 <= hold
    assert d.action == Action.CAPTURE_WITHIN


def test_expired_rule_blocks_auto_but_not_lower_bill():
    m = mandate(expires_at=T0 - timedelta(minutes=1))
    assert run([visit(), extra("Washer", "6")], m).action == Action.NEEDS_APPROVAL
    assert run([visit("45")], m).action == Action.CAPTURE_WITHIN


def test_mandate_hash_is_carried_on_every_decision():
    m = mandate()
    assert run([visit()], m).mandate_sha256 == m.sha256


def test_bad_items_rejected():
    with pytest.raises(ValueError):
        run([LineItem(name="x", unit_amount=Money(amount="5", currency="EUR"))])
    with pytest.raises(ValueError):
        run([LineItem(name="x", unit_amount=usd("-5"))])
    with pytest.raises(ValueError):
        run([LineItem(name="x", unit_amount=usd("5"), quantity=0)])


def test_quantity_counts():
    d = run([visit(), extra("Filter", "4.50"), extra("Filter", "4.50")])
    assert d.total.amount == Decimal("69.00") and d.action == Action.AUTO_CAPTURE
