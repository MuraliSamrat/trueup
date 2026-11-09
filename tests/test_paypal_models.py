from decimal import Decimal

import pytest

from app.paypal.models import LineItem, Money, OrderResult, cap_for, usd


def test_money_rounds_and_formats():
    assert usd("66").to_paypal() == {"currency_code": "USD", "value": "66.00"}
    assert usd("10.005").amount == Decimal("10.01")
    assert str(usd(5)) == "5.00 USD"


def test_money_rejects_float():
    with pytest.raises(Exception):
        Money(amount=0.1 + 0.2)


def test_money_arithmetic_and_currency_guard():
    assert (usd("60") + usd("6")).amount == Decimal("66.00")
    assert (usd("111") - usd("60")).amount == Decimal("51.00")
    with pytest.raises(ValueError):
        usd("1") + Money(amount="1", currency="EUR")


@pytest.mark.parametrize(
    "hold, cap",
    [
        ("60", "69.00"),       # 115% wins
        ("500", "575.00"),     # 115% = 575, +75 = 575 (tie)
        ("1000", "1075.00"),   # +75 wins
        ("33.33", "38.32"),    # rounded DOWN to the cent (38.3295)
    ],
)
def test_cap_for(hold, cap):
    assert cap_for(usd(hold)).amount == Decimal(cap)


def test_line_item_total_and_payload():
    item = LineItem(name="Faucet cartridge", unit_amount=usd("22.50"), quantity=2)
    assert item.total.amount == Decimal("45.00")
    assert item.to_paypal() == {"name": "Faucet cartridge", "unit_amount": {"currency_code": "USD", "value": "22.50"}, "quantity": "2"}


def test_order_result_picks_payer_action_link():
    o = OrderResult.from_paypal(
        {"id": "O1", "status": "PAYER_ACTION_REQUIRED",
         "links": [{"rel": "self", "href": "x"}, {"rel": "payer-action", "href": "https://approve"}]}
    )
    assert o.approve_url == "https://approve"
