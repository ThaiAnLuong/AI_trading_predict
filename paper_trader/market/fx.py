"""USD/CAD from the Bank of Canada Valet API (no key required).

This is a daily average published for the previous business day, not a live rate.
https://www.bankofcanada.ca/valet/docs
"""
from __future__ import annotations

from decimal import InvalidOperation

from ..http import fetch_json
from ..money import D
from .base import DataUnavailable, FxProvider, FxQuote

URL = "https://www.bankofcanada.ca/valet/observations/FXUSDCAD/json"
SOURCE = "Bank of Canada daily average"


class BankOfCanadaFx(FxProvider):
    def get_usd_cad(self) -> FxQuote:
        return parse_fx(fetch_json(URL, {"recent": 1}))


def parse_fx(data: dict) -> FxQuote:
    try:
        row = data["observations"][-1]
        rate = D(row["FXUSDCAD"]["v"])
        if rate <= 0:
            raise ValueError
        return FxQuote(rate, row["d"], SOURCE)
    except (KeyError, IndexError, TypeError, ValueError, InvalidOperation):
        raise DataUnavailable("The Bank of Canada response had no usable rate.") from None
