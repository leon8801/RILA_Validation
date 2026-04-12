from __future__ import annotations
from dataclasses import dataclass, field, asdict
from typing import Any, Dict, Optional
from datetime import datetime, date
from decimal import Decimal
from collections import OrderedDict
import pandas as pd

from .Role import Role        # WB flow
from .Funds import Funds      # DB flow

# Local lightweight helpers
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

def to_decimal(val: Any) -> Optional[Decimal]:
    if pd.isna(val):
        return None
    try:
        return Decimal(str(val))
    except Exception:
        return None


@dataclass
class Transaction:
    # Common
    date: Optional[date] = None
    pol_yr: Optional[int] = None
    transaction_code: Optional[int] = None
    transaction_num: Optional[str] = None
    transaction_type: Optional[str] = None

    # WB-specific system/program fields (kept for backward compatibility)
    # sysRemEvalPeriod: Optional[int] = None
    # sysBB: Optional[Decimal] = None
    # sysBBB: Optional[Decimal] = None
    # sysBBC: Optional[Decimal] = None
    # sysMAWA: Optional[Decimal] = None
    # sysNextFeeDate: Optional[date] = None
    # sysGMWBFee: Optional[Decimal] = None
    # sysPIPV: Optional[Decimal] = None
    # sysGMBB: Optional[Decimal] = None
    # sysGMBBRate: Optional[Decimal] = None

    # progRemEvalPeriod: Optional[int] = None
    # progBB: Optional[Decimal] = None
    # progBBB: Optional[Decimal] = None
    # progBBC: Optional[Decimal] = None
    # progMAWA: Optional[Decimal] = None
    # progMSGMAWA: Optional[Decimal] = None
    # progNextFeeDate: Optional[date] = None
    # progGMWBFee: Optional[Decimal] = None
    # progPIPV: Optional[Decimal] = None
    # progGMBB: Optional[Decimal] = None
    # progGMBBRate: Optional[Decimal] = None

    # Other (WB)
    DOB1: Optional[date] = None
    DOB2: Optional[date] = None
    ownerType: Optional[str] = None
    comments: Optional[str] = None

    # DB-specific fields
    txn_amount: Optional[str] = None
    interest_credited: Optional[str] = '0'
    sys_av: Optional[str] = None
    sys_mwv: Optional[str] = None
    sys_csv: Optional[str] = None
    csv: Optional[str] = None
    sys_death_benefit: Optional[str] = None
    death_benefit: Optional[str] = None
    # Nested (WB)
    roles: "OrderedDict[int, Dict[str, Any]]" = field(default_factory=OrderedDict)
    # Nested (DB)
    funds: "OrderedDict[int, Dict[str, Any]]" = field(default_factory=OrderedDict)

    # ====== WB (legacy) ======
    @classmethod
    def from_row(cls, row: pd.Series) -> "Transaction":
        return cls(
            date=to_date(row.get("EFFECTIVE_DATE")),
            pol_yr=row.get("pol_yr"),
            transaction_code=to_str(row.get("TXN_TYPE")),
            transaction_num=to_str(row.get("TRANSACTION_NUM")),
            transaction_type=to_str(row.get("TRANSACTION_DESC")),
            txn_amount=to_str(row.get("TXN_AMT")),
            sys_av=to_decimal(row.get("ACCOUNT_VAL")),
            sys_mwv=to_decimal(row.get("SYS_MWV_Pol")),
            sys_csv=to_decimal(row.get("SYS_CSV")),
            csv = 0,
            sys_death_benefit=to_decimal(row.get("TC_DB_PROCEEDS")),
            death_benefit = 0,

            DOB1=to_date(row.get("DoBirthi")),
            DOB2=to_date(row.get("DoBirtha")),
            ownerType=to_str(row.get("OWNER_TYPE")),
            comments=to_str(row.get("COMMENTS")),
        )

    def update_from_row(self, row: pd.Series) -> None:
        self.date = self.date or to_date(row.get("EFFECTIVE_DATE"))
        self.pol_yr = self.pol_yr or row.get("pol_yr")
        self.transaction_code = self.transaction_code or to_int(row.get("TXN_TYPE"))
        self.transaction_type = self.transaction_type or to_str(row.get("TRANSACTION_DESC"))
        self.txn_amount = self.txn_amount or to_str(row.get("TXN_AMT"))

        for attr, col in [
            ("sys_av", "ACCOUNT_VAL"),
            ("sys_mwv", "MWV"),
            ("sys_csv", "CSV"),
            ("sys_death_benefit", "TC_DB_PROCEEDS")
        ]:
            if getattr(self, attr) is None:
                val = row.get(col)
                if attr.endswith("Date"):
                    setattr(self, attr, to_date(val))
                elif attr.endswith("Period") or attr in {"transaction_code"}:
                    setattr(self, attr, to_int(val))
                elif attr in {"transaction_type", "ownerType", "comments"}:
                    setattr(self, attr, to_str(val))
                else:
                    setattr(self, attr, to_decimal(val))

        self.DOB1 = self.DOB1 or to_date(row.get("DOB1"))
        self.DOB2 = self.DOB2 or to_date(row.get("DOB2"))
        self.ownerType = self.ownerType or to_str(row.get("OWNER_TYPE"))
        self.comments = self.comments or to_str(row.get("COMMENTS"))

    # ====== DB (gmwb_long) ======
    @classmethod
    def from_row_db(cls, row: pd.Series) -> "Transaction":
        return cls(
            date=to_date(row.get("EFFECTIVE_DATE")),
            transaction_code=to_int(row.get("TXN_TYPE")),
            transaction_num=to_str(row.get("TRANSACTION_NUM")),
            transaction_type=to_str(row.get("TRANSACTION_DESC")),  # if you have a desc col, swap here
            txn_amount=to_str(row.get("TXN_AMT"))
        )

    def update_from_row_db(self, row: pd.Series) -> None:
        self.date = self.date or to_date(row.get("EFFECTIVE_DATE"))
        self.transaction_code = self.transaction_code or to_int(row.get("TXN_TYPE"))
        self.transaction_num = self.transaction_num or to_str(row.get("TRANSACTION_NUM"))
        self.transaction_type = self.transaction_type or to_str(row.get("TRANSACTION_DESC"))
        self.txn_amount = self.txn_amount or to_str(row.get("TXN_AMT"))

    # ====== Upserts ======
    def upsert_role(self, row: pd.Series, role_idx_provider) -> None:
        """WB path: unique PR_ROLE_ID per txn."""
        pr_role_id = to_int(row.get("PR_ROLE_ID"))
        if pr_role_id is None:
            return

        existing_idx = None
        for idx, role_dict in self.roles.items():
            if role_dict.get("roleId") == pr_role_id:
                existing_idx = idx
                break

        if existing_idx is None:
            idx = role_idx_provider()
            role = Role.from_row(row)
            self.roles[idx] = role.to_dict()
        else:
            role = Role(**self.roles[existing_idx])
            role.update_from_row(row)
            self.roles[existing_idx] = role.to_dict()

    def upsert_fund(self, row: pd.Series, fund_idx_provider) -> None:
        """DB path: unique raw_fund_code per txn."""
        code = to_str(row.get("raw_fund_code"))
        if code is None:
            return

        existing_idx = None
        for idx, fund_dict in self.funds.items():
            if fund_dict.get("raw_fund_code") == code:
                existing_idx = idx
                break

        if existing_idx is None:
            idx = fund_idx_provider()
            fund = Funds.from_row(row)
            self.funds[idx] = fund.to_dict()
        else:
            fund = Funds(**self.funds[existing_idx])
            fund.update_from_row(row)
            self.funds[existing_idx] = fund.to_dict()

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        # Convert dates to ISO format when needed
        for k in list(d.keys()):
            if isinstance(d[k], (date, datetime)):
                d[k] = d[k].isoformat()
        # Decimal → str for JSON friendliness
        for k, v in d.items():
            if isinstance(v, Decimal):
                d[k] = str(v)
        # Ensure date fields are ISO strings
        for k in ["date", "DOB1", "DOB2"]:
            if d.get(k) and isinstance(d[k], (date, datetime)):
                d[k] = d[k].isoformat()
        return d

    @classmethod
    def aggregate_fund_values_from_dict(cls, txn_dict: Dict[str, Any]) -> Dict[str, Decimal]:
        """
        Aggregates account values and mwvs from all funds within a single transaction dict
        (as loaded from pols_structure.json).

        Returns a dict with:
            - total_av: sum of all fund av values
            - total_mwv: sum of all fund mwv values
            - total_sys_av: sum of all fund sys_av values
            - total_sys_mwv: sum of all fund sys_mwv values
        """
        total_av = Decimal("0")
        total_mwv = Decimal("0")

        if txn_dict.get("transaction_type", {}) == "Initial Premium":
            return {
                    "total_av": Decimal("0"),
                    "total_mwv": Decimal("0")
            }

        funds = txn_dict.get("funds", {}) or {}
        for fund_dict in funds.values():
            for key, target in [
                ("av", "total_av"),
                ("mwv", "total_mwv")
            ]:
                val = fund_dict.get(key)
                if val is not None:
                    try:
                        decimal_val = Decimal(str(val))
                        if target == "total_av":
                            total_av += decimal_val
                        elif target == "total_mwv":
                            total_mwv += decimal_val
                    except Exception:
                        continue

        return {
            "total_av": total_av,
            "total_mwv": total_mwv
        }