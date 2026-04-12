from __future__ import annotations
from dataclasses import dataclass, asdict
from typing import Any, Dict, Optional
from datetime import datetime, date
from decimal import Decimal
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

def to_str(val: Any) -> Optional[str]:
    if pd.isna(val):
        return None
    return str(val)

def to_int(val: Any) -> Optional[int]:
    if pd.isna(val):
        return None
    try:
        return int(val)
    except Exception:
        return None


@dataclass
class Role:
    roleId: Optional[int] = None
    optId: Optional[int] = None
    type: Optional[str] = None
    DOB: Optional[date] = None
    relation: Optional[str] = None

    @classmethod
    def from_row(cls, row: pd.Series) -> "Role":
        return cls(
            roleId=to_int(row.get("PR_ROLE_ID")),
            optId=to_int(row.get("PR_OPT_ID")),
            type=to_str(row.get("NM_NAME_TYPE")),
            DOB=to_date(row.get("NM_DOB")),
            relation=to_str(row.get("RE_RELATIONSHIP_DESC")),
        )

    def update_from_row(self, row: pd.Series) -> None:
        self.roleId = self.roleId or to_int(row.get("PR_ROLE_ID"))
        self.optId = self.optId or to_int(row.get("PR_OPT_ID"))
        self.type = self.type or to_str(row.get("NM_NAME_TYPE"))
        self.DOB = self.DOB or to_date(row.get("NM_DOB"))
        self.relation = self.relation or to_str(row.get("RE_RELATIONSHIP_DESC"))

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        dob_val = d.get("DOB")
        if isinstance(dob_val, (date, datetime)):
            d["DOB"] = dob_val.isoformat()
        # if it's already a str or None, just leave it
        return d
