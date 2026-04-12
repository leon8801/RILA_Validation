from __future__ import annotations
from dataclasses import dataclass, asdict
from typing import Any, Dict, Optional
import pandas as pd

def to_str(val: Any) -> Optional[str]:
    if pd.isna(val):
        return None
    return str(val)

@dataclass
class Funds:
    raw_fund_code: Optional[str] = None
    alt_fund_code: Optional[str] = None 
    fund_name: Optional[str] = None
    interest_credited: Optional[str] = None
    strategy_return: Optional[str] = None
    txn_amt: Optional[str] = None
    prem: Optional[str] = None
    wd: Optional[str] = None
    av: Optional[str] = None
    sys_av: Optional[str] = None
    mwv: Optional[str] = None
    sys_mwv: Optional[str] = None
    strat_base: Optional[str] = None

    term_start_date: Optional[str] = None
    term_end_date: Optional[str] = None
    alloc_pct: Optional[str] = None
    buffer: Optional[str] = None
    upside_param_type: Optional[str] = None   # cap / trigger / lock-step
    upside_param_rate: Optional[str] = None
    beg_equity_index_value: Optional[str] = None
    end_equity_index_value: Optional[str] = None
    auto_locked: Optional[str] = None
    manual_locked: Optional[str] = None
    lock_credited: Optional[str] = None

    # Transaction-level fund amount (per-transaction fund attribution)
    # TODO: txn_amount: Optional[str] = None

    @classmethod
    def from_row(cls, row: pd.Series) -> "Funds":
        # Define mapping: {class_argument: dataframe_column}
        mapping = {
            "raw_fund_code": "raw_fund_code",
            "fund_name": "fund_name",
            "alt_fund_code": "alt_fund_code",
            "interest_credited": "interest_credit_amt",
            "strategy_return": "strategy_return",
            "txn_amt": "AS_txn_amt",
            "prem": "Prem",
            "wd": "wd",
            "av": "av_end_txn",
            "sys_av": "PRNL_amt",
            "mwv": "mwv_interim",
            "sys_mwv": "SYS_MWV",
            "strat_base": "strategy_base",
            "term_start_date": "bop_date",
            "alloc_pct": "fund_attribution",
            "buffer": "buffer",
            "upside_param_type": "upside_param",
            "upside_param_rate": "upside_param_rate",
            "beg_equity_index_value": "bop_index_value",
            "end_equity_index_value": "index_value",
            "auto_locked": "auto_locked",
            "manual_locked": "manual_locked",
            "lock_credited": "lock_credited"
        }

        # Extract and convert values
        data = {k: to_str(row.get(v)) for k, v in mapping.items()}
        
        # Handle the unique edge case for the date manually
        data["term_end_date"] = to_str(row.get("eot_date").date())

        return cls(**data)


    def update_from_row(self, row: pd.Series) -> None:
        mapping = {
            "raw_fund_code": "raw_fund_code",
            "fund_name": "fund_name",
            "alt_fund_code": "alt_fund_code",
            "interest_credited": "interest_credit_amt",
            "strategy_return": "strategy_return",
            "txn_amt": "AS_txn_amt",
            "prem": "Prem",
            "wd": "wd",
            "av": "av_end_txn",
            "sys_av": "PRNL_amt",
            "mwv": "mwv_interim",
            "sys_mwv": "SYS_MWV",
            "strat_base": "strategy_base",
            "term_start_date": "bop_date",
            "term_end_date": "eot_date",
            "alloc_pct": "fund_attribution",
            "buffer": "buffer",
            "upside_param_type": "upside_param",
            "upside_param_rate": "upside_param_rate",
            "beg_equity_index_value": "bop_index_value",
            "end_equity_index_value": "index_value",
            "auto_locked": "auto_locked",
            "manual_locked": "manual_locked",
            "lock_credited": "lock_credited"
            }

        for attr, column in mapping.items():
            # Only update if the current attribute is None or empty
            if not getattr(self, attr):
                setattr(self, attr, to_str(row.get(column)))


    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)