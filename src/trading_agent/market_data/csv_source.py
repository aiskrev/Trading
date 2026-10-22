import csv
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Iterator, Union

from trading_agent.market_data.ports import RawTick

COLUMNS = ("ts", "symbol", "bid", "ask", "bid_size", "ask_size", "last")


class CsvMarketDataSource:
    """Historical quotes from a CSV file with columns: ts,symbol,bid,ask,bid_size,ask_size,last."""

    def __init__(self, path: Union[str, Path], source_id: str) -> None:
        self.path = Path(path)
        self.source_id = source_id

    def ticks(self) -> Iterator[RawTick]:
        with self.path.open(newline="", encoding="utf-8") as fh:
            reader = csv.DictReader(fh)
            if tuple(reader.fieldnames or ()) != COLUMNS:
                raise ValueError(f"{self.path}: expected columns {COLUMNS}, got {reader.fieldnames}")
            for row in reader:
                yield RawTick(
                    source_id=self.source_id,
                    symbol=row["symbol"],
                    ts=datetime.fromisoformat(row["ts"]),
                    bid=Decimal(row["bid"]),
                    ask=Decimal(row["ask"]),
                    bid_size=Decimal(row["bid_size"]),
                    ask_size=Decimal(row["ask_size"]),
                    last=Decimal(row["last"]),
                )
