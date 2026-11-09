"""The business's price list and a small retriever over it.

The AI never invents a price: it can only *find* items here and refer to them by SKU.
Prices are attached by code from this file. Retrieval is lexical (token + alias overlap),
which is fast, deterministic, offline and plenty for a price list of a few hundred items.
"""
from __future__ import annotations

import json
import re
from functools import lru_cache
from pathlib import Path

from pydantic import BaseModel

from app.paypal.models import LineItem, Money

DEFAULT_PATH = Path(__file__).resolve().parents[2] / "data" / "price_list.json"
_WORD = re.compile(r"[a-z0-9&]+")
_STOP = {"a", "an", "the", "and", "or", "of", "for", "to", "in", "on", "with", "new", "replace", "replaced", "fix", "fixed"}


def _tokens(text: str) -> set[str]:
    toks = {t for t in _WORD.findall(text.lower()) if t not in _STOP}
    # light stemming so "washers" matches "washer", "leaking" matches "leak"
    return toks | {t[:-1] for t in toks if t.endswith("s") and len(t) > 3} | {t[:-3] for t in toks if t.endswith("ing") and len(t) > 5}


class PriceItem(BaseModel):
    sku: str
    name: str
    category: str
    price: str
    aliases: list[str] = []

    @property
    def money(self) -> Money:
        return Money(amount=self.price)

    def search_text(self) -> str:
        return " ".join([self.name, self.category, *self.aliases])

    def brief(self) -> str:
        return f"{self.sku} | {self.name} | {self.category} | ${self.price}"


class PriceList(BaseModel):
    business: str
    currency: str = "USD"
    items: list[PriceItem]

    @classmethod
    def load(cls, path: Path | str = DEFAULT_PATH) -> PriceList:
        return cls.model_validate(json.loads(Path(path).read_text(encoding="utf-8")))

    def get(self, sku: str) -> PriceItem | None:
        sku = sku.strip().upper()
        return next((i for i in self.items if i.sku == sku), None)

    def search(self, query: str, k: int = 5) -> list[PriceItem]:
        q = _tokens(query)
        if not q:
            return []
        scored = []
        for item in self.items:
            name_toks = _tokens(item.name)
            all_toks = _tokens(item.search_text())
            score = 2 * len(q & name_toks) + len(q & all_toks)
            # whole-alias phrase match is a strong signal
            score += sum(3 for a in item.aliases if a.lower() in query.lower())
            if score:
                scored.append((score, item.sku, item))
        scored.sort(key=lambda s: (-s[0], s[1]))
        return [s[2] for s in scored[:k]]

    def line_item(self, sku: str, quantity: int = 1, added: bool = True, reason: str | None = None) -> LineItem:
        item = self.get(sku)
        if item is None:
            raise KeyError(f"Unknown SKU {sku}")
        return LineItem(name=item.name, unit_amount=Money(amount=item.price, currency=self.currency),
                        quantity=quantity, category=item.category, added=added,
                        description=(reason or None) and reason[:127])


@lru_cache
def default_price_list() -> PriceList:
    return PriceList.load()
