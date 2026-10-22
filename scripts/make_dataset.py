"""Build the synthetic test dataset `synthetic-ca-2026jan.v1` (deterministic).

Eight trading days of 1-minute quotes for AAA and BBB (2026-01-12 .. 01-22; 01-19 is a market
holiday and has no data). Built to exercise every backtest requirement:
  * AAA: dip and slow decline on 01-13 (entry, a thin-size bar -> partial fill), 2:1 split ex 01-14,
           recovery on 01-14 (exit)
  * BBB: dip on 01-15 (entry), cash dividend 0.25 ex 01-16, slow decline, delisted 01-21 at 46.80
           while held (position must be closed at the delisting price, not dropped)
Refuses to overwrite an existing dataset whose content differs.
"""
import sys
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "src"), str(ROOT / ".deps")]
from trading_agent.backtest.dataset import DATASETS_DIR, FILES, file_sha256  # noqa: E402

VERSION = "synthetic-ca-2026jan.v1"
NY = ZoneInfo("America/New_York")
DAYS = [date(2026, 1, d) for d in (12, 13, 14, 15, 16, 20, 21, 22)]
C = Decimal("0.01")


def minutes(day):
    t = datetime.combine(day, time(9, 30), NY)
    while t.time() < time(16, 0):
        yield t
        t += timedelta(minutes=1)


def wiggle(i: int) -> Decimal:
    return Decimal((i * 7) % 5 - 2) / Decimal(10000)  # deterministic +-2bp noise


def aaa_mid(day, t, i) -> Decimal:
    base = Decimal(100)
    if day == DAYS[1] and t.time() >= time(14, 0):  # dip then slow decline into the close
        k = (t.hour * 60 + t.minute) - 14 * 60
        return base * (Decimal("0.9940") - Decimal("0.00005") * k)
    if day == DAYS[2]:                               # split day: half price; decline then recover from 11:00
        k = (t.hour * 60 + t.minute) - (9 * 60 + 30)
        low = Decimal("0.9940") - Decimal("0.00005") * 120
        if t.time() < time(11, 0):
            return base / 2 * (low - Decimal("0.00005") * k)
        return base / 2 * min(Decimal("1.0"), low + Decimal("0.0002") * (k - 90))
    if day > DAYS[2]:
        return base / 2 * (1 + wiggle(i))
    return base * (1 + wiggle(i))


def bbb_mid(day, t, i) -> Decimal:
    k_day = DAYS.index(day)
    m = (t.hour * 60 + t.minute) - (9 * 60 + 30)
    if (k_day, m) < (3, 210):                       # before 01-15 13:00
        return Decimal(50) * (1 + wiggle(i))
    # drop at 01-15 13:00, then one cent down every 5 minutes (keeps price below its 10-bar SMA)
    elapsed = (k_day - 3) * 390 + m - 210
    price = Decimal("49.70") - Decimal("0.01") * (elapsed // 5)
    if k_day >= 4:
        price -= Decimal("0.25")                    # ex-dividend gap on 01-16
    return price


def quote_row(t, sym, mid, size=500):
    half = max(C, (mid * Decimal("0.0001")).quantize(C))  # 2bp spread, at least 1 cent each side
    m = mid.quantize(C)
    return f"{t.astimezone(timezone.utc).isoformat()},{sym},{m - half},{m + half},{size},{size},{m}"


def build():
    rows = ["ts,symbol,bid,ask,bid_size,ask_size,last"]
    for day in DAYS:
        for i, t in enumerate(minutes(day)):
            thin = day == DAYS[1] and t.time() == time(14, 1)
            rows.append(quote_row(t, "AAA", aaa_mid(day, t, i), 30 if thin else 500))
            if day < date(2026, 1, 21):
                rows.append(quote_row(t, "BBB", bbb_mid(day, t, i)))
    actions = ["ex_date,symbol,type,value", "2026-01-14,AAA,SPLIT,2", "2026-01-16,BBB,CASH_DIVIDEND,0.25"]
    listings = ["symbol,listed_from,delisted_on,delist_price", "AAA,2020-01-02,,", "BBB,2020-01-02,2026-01-21,46.80"]
    return {"quotes.csv": rows, "corporate_actions.csv": actions, "listings.csv": listings}


def main() -> int:
    out = DATASETS_DIR / VERSION
    content = {n: ("\n".join(lines) + "\n").encode("utf-8") for n, lines in build().items()}
    if out.exists():
        same = all((out / n).read_bytes().replace(b"\r\n", b"\n") == b for n, b in content.items())
        print("unchanged" if same else f"refusing: {VERSION} exists with different content; bump the version")
        return 0 if same else 1
    out.mkdir(parents=True)
    for n, b in content.items():
        (out / n).write_bytes(b)
    manifest = {"dataset_version": VERSION, "synthetic": True, "source": "scripts/make_dataset.py",
                "description": "Synthetic 8-day 1-minute AAA/BBB quotes with a 2:1 split, a cash dividend, a delisting while held, a thin-size bar and a market holiday (for testing the backtester only)", "files": {n: file_sha256(out / n) for n in FILES}}
    (out / "manifest.yaml").write_bytes(yaml.safe_dump(manifest, sort_keys=True).encode("utf-8"))
    print(f"wrote {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
