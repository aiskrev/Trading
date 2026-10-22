"""Regenerate the synthetic quote fixtures in data/fixtures (deterministic, no randomness)."""
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

OUT = Path(__file__).resolve().parents[1] / "data" / "fixtures"
START = datetime(2026, 1, 5, 14, 30, tzinfo=timezone.utc)
BARS = 60
HALF_SPREAD = Decimal("0.01")


def aaa_mid(i: int) -> Decimal:
    if i < 20:
        return Decimal("100.00")
    if i == 20:
        return Decimal("99.50")  # dip -> demo strategy BUYs
    if i < 30:
        return Decimal("99.50") + Decimal("0.08") * (i - 20)  # recovery -> SELL
    return Decimal("100.30")


def bbb_mid(i: int) -> Decimal:
    return Decimal("50.00") + (Decimal("0.01") if i % 2 else Decimal(0))


def write(name: str, offset: Decimal) -> None:
    lines = ["ts,symbol,bid,ask,bid_size,ask_size,last"]
    for i in range(BARS):
        ts = (START + timedelta(seconds=i)).isoformat()
        for sym, mid in (("AAA", aaa_mid(i)), ("BBB", bbb_mid(i))):
            m = mid + offset
            lines.append(f"{ts},{sym},{m - HALF_SPREAD},{m + HALF_SPREAD},500,500,{m}")
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / name).write_text("\n".join(lines) + "\n", encoding="utf-8")


if __name__ == "__main__":
    write("quotes_primary.csv", Decimal("0"))
    write("quotes_secondary.csv", Decimal("0.002"))
