from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from app.engine.mandate import Mandate, custom_id_for, parse_custom_id
from app.paypal.models import usd

T0 = datetime(2026, 11, 9, 10, 0, tzinfo=timezone.utc)


def make(**kw) -> Mandate:
    base = dict(mandate_id="m1", customer_ref="agent-abc", created_at=T0)
    base.update(kw)
    return Mandate(**base)


def test_hash_is_stable_and_order_independent():
    a = make(always_ask_categories=["Emergency", "parts "])
    b = make(always_ask_categories=["parts", "emergency"])
    assert a.always_ask_categories == ["emergency", "parts"]
    assert a.sha256 == b.sha256 and len(a.sha256) == 64
    assert a.verify(b.sha256)


def test_any_change_changes_hash():
    a = make()
    assert a.sha256 != make(max_auto_increase_amount="20").sha256
    assert a.sha256 != make(customer_ref="agent-xyz").sha256


def test_custom_id_roundtrip_and_length():
    m = make()
    cid = custom_id_for("job-42", m)
    assert cid == f"job-42|m:{m.short_hash}"
    assert parse_custom_id(cid) == ("job-42", m.short_hash)
    assert len(custom_id_for("j" * 200, m)) <= 127


def test_expiry():
    m = make(expires_at=T0 + timedelta(days=1))
    assert not m.is_expired(T0)
    assert m.is_expired(T0 + timedelta(days=1))
    assert not make().is_expired(T0 + timedelta(days=999))


@pytest.mark.parametrize(
    "kw, hold, limit",
    [
        ({}, "60", "69.00"),                                    # PayPal cap only
        ({"max_auto_increase_pct": "10"}, "60", "66.00"),       # customer stricter (10%)
        ({"max_auto_increase_amount": "5"}, "60", "65.00"),     # customer stricter ($5)
        ({"max_auto_increase_pct": "50"}, "60", "69.00"),       # customer looser -> PayPal cap wins
        ({"max_auto_increase_amount": "0"}, "60", "60.00"),     # "always ask me"
        ({}, "1000", "1075.00"),                                # +$75 side of the cap
    ],
)
def test_auto_limit(kw, hold, limit):
    assert make(**kw).auto_limit(usd(hold)).amount == Decimal(limit)


def test_rejects_float_and_negative():
    with pytest.raises(Exception):
        make(max_auto_increase_pct=10.5)
    with pytest.raises(Exception):
        make(max_auto_increase_amount="-1")


def test_currency_mismatch():
    with pytest.raises(ValueError):
        make(currency="EUR").auto_limit(usd("60"))
