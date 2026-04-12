from __future__ import annotations
from dataclasses import dataclass, field
from typing import Any, Dict, Optional
from collections import OrderedDict
from datetime import datetime, date
import pandas as pd

# Local lightweight helpers (kept here to avoid circular imports)
def to_date(val: Any) -> Optional[date]:
    if pd.isna(val):
        return None
    if isinstance(val, pd.Timestamp):
        return val.date()
    if isinstance(val, datetime):
        return val.date()
    if isinstance(val, date):
        return val
    try:
        return pd.to_datetime(val).date()
    except Exception:
        return None


@dataclass
class Day:
    date: Optional[date] = None
    transactions: "OrderedDict[int, Dict[str, Any]]" = field(default_factory=OrderedDict)

    @classmethod
    def from_row(cls, row: pd.Series) -> "Day":
        return cls(date=to_date(row.get("EFFECTIVE_DATE")))

    def update_from_row(self, row: pd.Series) -> None:
        self.date = self.date or to_date(row.get("EFFECTIVE_DATE"))

    def to_dict(self) -> Dict[str, Any]:
        if isinstance(self.date, (datetime, date)):
            date_val = self.date.isoformat()
        else:
            date_val = self.date  # already str or None
        return {
            "date": date_val,
            "transactions": dict(self.transactions),
        }